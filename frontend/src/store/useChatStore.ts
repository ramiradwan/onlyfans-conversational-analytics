import { create } from "zustand";  
import { ReadyState } from "react-use-websocket";  
import {  
  OutgoingWssMessage,  
  ExtendedConversationNode,  
  AnalyticsUpdate,  
  SendMessageCommand,  
  EnrichmentResultPayload,  
  Message,  
  FullSyncResponse, // ✅ Import to type snapshot from backend  
} from "../types/backend-wss";  
import { sendMessageToAgent } from "../services/extension";  
import { useDebugStore } from "./useDebugStore";  
  
export interface ProfileInfo {  
  displayName?: string;  
  avatarUrl?: string;  
  online?: boolean; // Presence flag  
}  
  
export type ChatForUI = ExtendedConversationNode & {  
  displayName: string;  
  avatarUrl?: string;  
  sentimentScore?: number | null;  
  priorityScore?: number | null;  
  unreadCount: number;  
  online?: boolean;  
};  
  
interface ChatStore {  
  // State  
  readyState: ReadyState;  
  conversations: ExtendedConversationNode[];  
  analytics: Partial<AnalyticsUpdate>;  
  enrichmentResults: Record<string, EnrichmentResultPayload>;  
  profiles: Record<string, ProfileInfo>;  
  systemStatus: string | null;  
  snackbar: {  
    open: boolean;  
    message: string;  
    severity?: "error" | "info" | "success";  
    details?: string | null;  
  };  
  lastMessage: OutgoingWssMessage | null;  
  activeChatId: string | null;  
  messagesByChat: Record<string, Message[]>;  
  
  /** Cached arrays for stable selectors */  
  _chatsForUICache: ChatForUI[];  
  _sortedChatsCache: ChatForUI[];  
  _sortedChatsLimitCache: Record<number, ChatForUI[]>;  
  
  /** Stable empty defaults */  
  _emptyMessagesArray: Message[];  
  _emptyChatsArray: ChatForUI[];  
  
  // Actions  
  setError: (message: string, details?: string | null) => void;  
  setConnectionStatus: (status: ReadyState) => void;  
  setActiveChatId: (id: string | null) => void;  
  closeSnackbar: () => void;  
  
  /** WS message handler */  
  handleWssMessage: (raw: string) => void;  
  
  /** New: replace entire state from snapshot (FullSyncResponse) */  
  replaceStateFromSnapshot: (snapshot: FullSyncResponse) => void;  
  
  // PURE getters  
  getReadyState: () => ReadyState;  
  getChatsForUI: () => ChatForUI[];  
  getSortedChatsForUI: (limit?: number) => ChatForUI[];  
  getMessagesForChat: (chatId: string | null) => Message[];  
  getAnalytics: () => Partial<AnalyticsUpdate>;  
  getActiveChatId: () => string | null;  
  getSystemStatus: () => string | null;  
  getSnackbar: () => ChatStore["snackbar"];  
  getLastMessage: () => OutgoingWssMessage | null;  
  getEnrichmentResults: () => Record<string, EnrichmentResultPayload>;  
}  
  
export const useChatStore = create<ChatStore>((set, get) => ({  
  // Initial state  
  readyState: ReadyState.UNINSTANTIATED,  
  conversations: [],  
  analytics: {},  
  enrichmentResults: {},  
  profiles: {},  
  systemStatus: null,  
  snackbar: { open: false, message: "", details: null },  
  lastMessage: null,  
  activeChatId: null,  
  messagesByChat: {},  
  
  _chatsForUICache: [],  
  _sortedChatsCache: [],  
  _sortedChatsLimitCache: {},  
  
  _emptyMessagesArray: [],  
  _emptyChatsArray: [],  
  
  // Actions  
  setError: (message, details = null) =>  
    set({  
      snackbar: {  
        open: true,  
        message,  
        severity: "error",  
        details,  
      },  
    }),  
  
  setConnectionStatus: (status) => set({ readyState: status }),  
  
  setActiveChatId: (id) => set({ activeChatId: id }),  
  
  closeSnackbar: () =>  
    set((state) => ({  
      snackbar: { ...state.snackbar, open: false },  
    })),  
  
  replaceStateFromSnapshot: (snapshot) => {  
    const { conversations, analytics } = snapshot;  
    const profiles: Record<string, ProfileInfo> = {};  
    const messagesByChat: Record<string, Message[]> = {};  
  
    for (const conv of conversations) {  
      const fromUser = conv.messages?.[0]?.fromUser;  
      if (fromUser) {  
        profiles[conv.conversationId] = {  
          displayName: fromUser.displayName || fromUser.username || undefined,  
          avatarUrl: fromUser.avatarThumbs?.c144 || fromUser.avatar || undefined,  
          online: false,  
        };  
      }  
      messagesByChat[conv.conversationId] =  
        conv.messages || get()._emptyMessagesArray;  
    }  
  
    const chatsForUI = computeChatsForUI(  
      conversations,  
      profiles,  
      analytics,  
      get().enrichmentResults  
    );  
    const sortedChats = sortChats(chatsForUI);  
  
    set({  
      conversations,  
      analytics,  
      profiles: { ...get().profiles, ...profiles },  
      messagesByChat,  
      _chatsForUICache: chatsForUI,  
      _sortedChatsCache: sortedChats,  
      _sortedChatsLimitCache: {},  
    });  
  },  
  
  handleWssMessage: (raw) => {  
    try {  
      const msg = JSON.parse(raw) as OutgoingWssMessage;  
      set({ lastMessage: msg });  
  
      // Debug logging  
      const preview = JSON.stringify(msg.payload).slice(0, 150);  
      useDebugStore  
        .getState()  
        .addLog(  
          msg.type === "system_error" ? "error" : "info",  
          `WS [${msg.type}]: ${preview}${preview.length >= 150 ? "..." : ""}`  
        );  
  
      switch (msg.type) {  
        case "connection_ack": {  
          const { version, statusMessage } = msg.payload;  
          set({  
            snackbar: {  
              open: true,  
              message: `Connected: ${version}${  
                statusMessage ? ` — ${statusMessage}` : ""  
              }`,  
              severity: "success",  
              details: null,  
            },  
          });  
          break;  
        }  
  
        case "system_status": {  
          set({ systemStatus: msg.payload.status });  
          break;  
        }  
  
        case "system_error": {  
          const { errorMessage, detail } = msg.payload;  
          set({  
            snackbar: {  
              open: true,  
              message: errorMessage,  
              severity: "error",  
              details: detail || null,  
            },  
          });  
          break;  
        }  
  
        case "full_sync_response": {  
          get().replaceStateFromSnapshot(msg.payload);  
          break;  
        }  
  
        case "append_message": {  
          const newNode = msg.payload;  
          const profilesUpdate: Record<string, ProfileInfo> = {};  
          const fromUser = newNode.messages?.[0]?.fromUser;  
          if (fromUser) {  
            profilesUpdate[newNode.conversationId] = {  
              displayName: fromUser.displayName || fromUser.username || undefined,  
              avatarUrl: fromUser.avatarThumbs?.c144 || fromUser.avatar || undefined,  
              online:  
                get().profiles[newNode.conversationId]?.online ?? false,  
            };  
          }  
  
          set((state) => {  
            const idx = state.conversations.findIndex(  
              (c) => c.conversationId === newNode.conversationId  
            );  
            const conversations =  
              idx !== -1  
                ? state.conversations.map((c, i) =>  
                    i === idx ? { ...c, ...newNode } : c  
                  )  
                : [...state.conversations, newNode];  
  
            const existingMessages =  
              state.messagesByChat[newNode.conversationId] ||  
              state._emptyMessagesArray;  
            const updatedMessages = [  
              ...existingMessages,  
              ...(newNode.messages || []),  
            ];  
  
            const chatsForUI = computeChatsForUI(  
              conversations,  
              { ...state.profiles, ...profilesUpdate },  
              state.analytics,  
              state.enrichmentResults  
            );  
            const sortedChats = sortChats(chatsForUI);  
  
            return {  
              conversations,  
              profiles: { ...state.profiles, ...profilesUpdate },  
              messagesByChat: {  
                ...state.messagesByChat,  
                [newNode.conversationId]: updatedMessages,  
              },  
              _chatsForUICache: chatsForUI,  
              _sortedChatsCache: sortedChats,  
              _sortedChatsLimitCache: {},  
            };  
          });  
          break;  
        }  
  
        case "analytics_update": {  
          const update = msg.payload;  
          set((state) => {  
            const analytics = { ...state.analytics, ...update };  
            const chatsForUI = computeChatsForUI(  
              state.conversations,  
              state.profiles,  
              analytics,  
              state.enrichmentResults  
            );  
            const sortedChats = sortChats(chatsForUI);  
            return {  
              analytics,  
              _chatsForUICache: chatsForUI,  
              _sortedChatsCache: sortedChats,  
              _sortedChatsLimitCache: {},  
            };  
          });  
          break;  
        }  
  
        case "enrichment_result": {  
          const payload = msg.payload;  
          set((state) => {  
            // ✅ Cast conversation_id to string to satisfy TS computed key type  
            const enrichmentResults = {  
              ...state.enrichmentResults,  
              [payload.conversation_id as string]: payload,  
            };  
            const chatsForUI = computeChatsForUI(  
              state.conversations,  
              state.profiles,  
              state.analytics,  
              enrichmentResults  
            );  
            const sortedChats = sortChats(chatsForUI);  
            return {  
              enrichmentResults,  
              _chatsForUICache: chatsForUI,  
              _sortedChatsCache: sortedChats,  
              _sortedChatsLimitCache: {},  
            };  
          });  
          break;  
        }  
  
        case "command_to_execute": {  
          sendMessageToAgent<SendMessageCommand>(  
            "execute_agent_command",  
            msg.payload  
          ).catch((err: unknown) =>  
            set({  
              snackbar: {  
                open: true,  
                message: err instanceof Error ? err.message : String(err),  
                severity: "error",  
                details: null,  
              },  
            })  
          );  
          break;  
        }  
  
        case "online_users_update": {  
          const { user_ids } = msg.payload;  
          set((state) => {  
            const updatedProfiles = { ...state.profiles };  
            for (const convId of Object.keys(updatedProfiles)) {  
              const numericId = parseInt(convId, 10);  
              if (!isNaN(numericId)) {  
                updatedProfiles[convId] = {  
                  ...updatedProfiles[convId],  
                  online: user_ids.includes(numericId),  
                };  
              }  
            }  
            const chatsForUI = computeChatsForUI(  
              state.conversations,  
              updatedProfiles,  
              state.analytics,  
              state.enrichmentResults  
            );  
            const sortedChats = sortChats(chatsForUI);  
            return {  
              profiles: updatedProfiles,  
              _chatsForUICache: chatsForUI,  
              _sortedChatsCache: sortedChats,  
              _sortedChatsLimitCache: {},  
            };  
          });  
          break;  
        }  
  
        default:  
          console.warn("Unhandled WS message type:", msg);  
      }  
    } catch (err) {  
      console.error("[WS] Failed to parse message:", err, raw);  
    }  
  },  
  
  // PURE getters  
  getReadyState: () => get().readyState,  
  getChatsForUI: () => get()._chatsForUICache,  
  getSortedChatsForUI: (limit) => {  
    const { _sortedChatsCache, _sortedChatsLimitCache } = get();  
    if (!limit) return _sortedChatsCache;  
    if (!_sortedChatsLimitCache[limit]) {  
      _sortedChatsLimitCache[limit] = _sortedChatsCache.slice(0, limit);  
    }  
    return _sortedChatsLimitCache[limit];  
  },  
  getMessagesForChat: (chatId) => {  
    if (!chatId) return get()._emptyMessagesArray;  
    return get().messagesByChat[chatId] ?? get()._emptyMessagesArray;  
  },  
  getAnalytics: () => get().analytics,  
  getActiveChatId: () => get().activeChatId,  
  getSystemStatus: () => get().systemStatus,  
  getSnackbar: () => get().snackbar,  
  getLastMessage: () => get().lastMessage,  
  getEnrichmentResults: () => get().enrichmentResults,  
}));  
  
// --- Helper functions ---  
function computeChatsForUI(  
  conversations: ExtendedConversationNode[],  
  profiles: Record<string, ProfileInfo>,  
  analytics: Partial<AnalyticsUpdate>,  
  enrichmentResults: Record<string, EnrichmentResultPayload>  
): ChatForUI[] {  
  return conversations.map((chat) => {  
    const profile = profiles[chat.conversationId] || {};  
    const lateEnrich = enrichmentResults[chat.conversationId];  
  
    const sentimentScore =  
      typeof chat.sentiment === "number"  
        ? chat.sentiment  
        : lateEnrich?.sentiment ??  
          chat.messages?.[chat.messages.length - 1]?.sentimentScore ??  
          null;  
  
    const priorityScore =  
      typeof chat.priorityScore === "number"  
        ? chat.priorityScore  
        : analytics.priorityScores?.[chat.conversationId] ?? null;  
  
    const unreadCount = analytics.unreadCounts?.[chat.conversationId] ?? 0;  
  
    return {  
      ...chat,  
      withUser: chat.withUser ?? {  
        name: profile.displayName,  
        username: undefined,  
        avatar: profile.avatarUrl,  
      },  
      displayName:  
        profile.displayName ||  
        chat.topics?.[0]?.description ||  
        `Conversation ${chat.conversationId}`,  
      avatarUrl: profile.avatarUrl,  
      sentimentScore,  
      priorityScore,  
      unreadCount,  
      online: profile.online ?? false,  
    };  
  });  
}  
  
function sortChats(chats: ChatForUI[]): ChatForUI[] {  
  return [...chats].sort(  
    (a, b) => (b.priorityScore ?? 0) - (a.priorityScore ?? 0)  
  );  
}  