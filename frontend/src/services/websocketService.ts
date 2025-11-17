import { getConfig } from '@/config/fastapiConfig';  
import {  
  OutgoingWssMessage,  
  ConnectionInfo,  
  SystemStatus,  
  WssError,  
  FullSyncResponse,  
  ExtendedConversationNode,  
  AnalyticsUpdate,  
  EnrichmentResultPayload,  
  SendMessageCommand,  
} from '@/types/backend-wss';  
import { extensionService } from '@services/extensionService';  
import { analyticsStoreActions } from '@store/analyticsStore';  
import { useChatStore, chatStoreActions } from '@store/chatStore';  
import { enrichmentStoreActions } from '@store/enrichmentStore';  
import { systemStoreActions } from '@store/systemStore';  
  
class WebSocketService {  
  private ws: WebSocket | null = null;  
  private reconnectTimer: NodeJS.Timeout | null = null;  
  private userId: string | null = null;  
  private wsUrl: string | null = null;  
  
  public connect(wsUrl?: string, userId?: string) {  
    if (  
      this.ws &&  
      (this.ws.readyState === WebSocket.OPEN ||  
        this.ws.readyState === WebSocket.CONNECTING)  
    ) {  
      console.log('[WebSocketService] Already connected or connecting.');  
      return;  
    }  
  
    if (!wsUrl || !userId) {  
      const config = getConfig();  
      wsUrl = wsUrl ?? config.FASTAPI_WS_URL;  
      userId = userId ?? config.USER_ID ?? wsUrl?.split('/').pop();  
    }  
  
    if (!wsUrl || !userId) {  
      console.error('[WebSocketService] Missing WS URL or userId — cannot connect.');  
      return;  
    }  
  
    this.userId = userId;  
    this.wsUrl = wsUrl;  
  
    systemStoreActions.setBackendConnectionState('connecting');  
    console.log(`[WebSocketService] Connecting to WebSocket: ${wsUrl}`);  
  
    this.ws = new WebSocket(wsUrl);  
  
    this.ws.onopen = () => {  
      console.log('✅ [WebSocketService] Connected');  
      systemStoreActions.setBackendConnectionState('connected');  
      chatStoreActions.setLoadingState('loading');  
  
      // Spec: If Agent is present, send cache_update snapshot  
      if (extensionService.isAgentAvailable()) {  
        systemStoreActions.setExtensionConnectionState('connected');  
        extensionService  
          .getAllChatsFromDB()  
          .then((snapshot) => {  
            this.sendMessage('cache_update', snapshot);  
          })  
          .catch((err) => {  
            console.error('[WebSocketService] Failed to send cache_update:', err);  
            systemStoreActions.setExtensionConnectionState('error');  
          });  
      } else {  
        systemStoreActions.setExtensionConnectionState('disconnected');  
      }  
    };  
  
    this.ws.onmessage = (event) => {  
      try {  
        const message = JSON.parse(event.data) as OutgoingWssMessage;  
        this.handleMessage(message);  
      } catch (error) {  
        console.error('[WebSocketService] Failed to parse WS message:', error);  
      }  
    };  
  
    this.ws.onerror = (error) => {  
      console.error('[WebSocketService] WebSocket error:', error);  
      systemStoreActions.setBackendConnectionState('error');  
      chatStoreActions.setLoadingState('error');  
    };  
  
    this.ws.onclose = () => {  
      console.log('⚠️ [WebSocketService] Disconnected.');  
      systemStoreActions.setBackendConnectionState('disconnected');  
      chatStoreActions.setLoadingState('error');  
  
      if (!this.reconnectTimer) {  
        console.log('[WebSocketService] Attempting to reconnect in 5s...');  
        this.reconnectTimer = setTimeout(() => {  
          this.reconnectTimer = null;  
          if (this.wsUrl && this.userId) {  
            this.connect(this.wsUrl, this.userId);  
          }  
        }, 5000);  
      }  
    };  
  }  
  
  public disconnect() {  
    if (this.ws) {  
      console.log('[WebSocketService] Closing connection...');  
      this.ws.close();  
      this.ws = null;  
    }  
    if (this.reconnectTimer) {  
      clearTimeout(this.reconnectTimer);  
      this.reconnectTimer = null;  
    }  
    systemStoreActions.setBackendConnectionState('disconnected');  
  }  
  
  /** Spec: Unified sendMessage helper */  
  public sendMessage<T>(type: string, payload: T) {  
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {  
      this.ws.send(JSON.stringify({ type, payload }));  
    } else {  
      console.warn('[WebSocketService] Cannot send message — WS not open.');  
    }  
  }  
  
  /** WS message router */  
  private handleMessage(message: OutgoingWssMessage) {  
    switch (message.type) {  
      case 'connection_ack':  
        this.onConnectionAck(message.payload);  
        break;  
      case 'system_status':  
        this.onSystemStatus(message.payload);  
        break;  
      case 'full_sync_response':  
        this.onFullSync(message.payload);  
        break;  
      case 'append_message':  
        this.onAppendMessage(message.payload);  
        break;  
      case 'analytics_update':  
        this.onAnalyticsUpdate(message.payload);  
        break;  
      case 'enrichment_result':  
        this.onEnrichmentResult(message.payload);  
        break;  
      case 'command_to_execute':  
        this.onCommandToExecute(message.payload as SendMessageCommand);  
        break;  
      case 'system_error':  
        this.onSystemError(message.payload);  
        break;  
      case 'online_users_update': // ✅ presence handling  
        this.onOnlineUsersUpdate(  
          message.payload as { user_ids: number[]; timestamp: string }  
        );  
        break;  
      default: {  
        const unknownMessage = message as Record<string, unknown>;  
        console.warn(  
          '[WebSocketService] Unhandled WS message type:',  
          unknownMessage.type  
        );  
      }  
    }  
  }  
  
  private onConnectionAck(payload: ConnectionInfo) {  
    console.log(`[WebSocketService] Connection ACK: v${payload.version}`);  
  
    // In no-Agent mode, backend won't send system_status  
    if (!extensionService.isAgentAvailable()) {  
      systemStoreActions.handleSystemStatus({  
        status: 'REALTIME',  
        detail: payload.statusMessage ?? 'Connected to backend',  
      });  
    }  
  }  
  
  private onSystemStatus(payload: SystemStatus) {  
    console.log(`[WebSocketService] System Status: ${payload.status}`);  
    systemStoreActions.handleSystemStatus(payload);  
  
    if (payload.status === 'PROCESSING_SNAPSHOT') {  
      chatStoreActions.setLoadingState('loading');  
    }  
  }  
  
  private onFullSync(payload: FullSyncResponse) {  
    console.log('[WebSocketService] Received full_sync_response, hydrating stores...');  
    if (payload.analytics) {  
      analyticsStoreActions.handleAnalyticsUpdate(payload.analytics);  
    }  
    chatStoreActions.replaceStateFromSnapshot(payload);  
  }  
  
  private onAppendMessage(payload: ExtendedConversationNode) {  
    chatStoreActions.handleAppendConversation(payload);  
  }  
  
  private onAnalyticsUpdate(payload: AnalyticsUpdate) {  
    analyticsStoreActions.handleAnalyticsUpdate(payload);  
  }  
  
  /**   
   * Spec-compliant enrichment handling:  
   * - Update enrichment store  
   * - Merge enrichment fields into chat conversation if changed  
   */  
  private onEnrichmentResult(payload: EnrichmentResultPayload) {  
    // Always update enrichment store  
    enrichmentStoreActions.handleEnrichmentResult(payload);  
  
    // Merge into chat conversation  
    const convo = useChatStore.getState().conversations[payload.conversationId];  
    if (convo) {  
      const enrichedConvo = {  
        ...convo,  
        topics: payload.topics,  
        actions: payload.actions,  
        sentiment: payload.sentiment,  
        outcomes: payload.outcomes,  
      };  
  
      // Deep compare enrichment fields to avoid redundant updates  
      const fieldsChanged =  
        JSON.stringify({  
          topics: convo.topics,  
          actions: convo.actions,  
          sentiment: convo.sentiment,  
          outcomes: convo.outcomes,  
        }) !==  
        JSON.stringify({  
          topics: enrichedConvo.topics,  
          actions: enrichedConvo.actions,  
          sentiment: enrichedConvo.sentiment,  
          outcomes: enrichedConvo.outcomes,  
        });  
  
      if (fieldsChanged) {  
        console.log(  
          `[WebSocketService] Merging enrichment into chatStore for convo ${payload.conversationId}`  
        );  
        chatStoreActions.handleAppendConversation(enrichedConvo);  
      }  
    }  
  }  
  
  private onCommandToExecute(payload: SendMessageCommand) {  
    extensionService.executeAgentCommand(payload);  
  }  
  
  private onOnlineUsersUpdate(payload: { user_ids: number[]; timestamp: string }) {  
    systemStoreActions.handleOnlineUsersUpdate(payload);  
  }  
  
  private onSystemError(payload: WssError) {  
    console.error(  
      `[WebSocketService] System Error: ${payload.errorMessage}`,  
      payload.detail  
    );  
    systemStoreActions.setBackendConnectionState('error');  
  }  
}  
  
export const websocketService = new WebSocketService();  