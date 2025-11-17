import { create } from 'zustand';  
import type { SystemStatus } from '@/types/backend-wss';  
  
type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';  
  
export interface SystemStoreState {  
  backendConnectionState: ConnectionState;      // Brain ↔ Bridge WS  
  extensionConnectionState: ConnectionState;    // Bridge ↔ Agent messaging  
  systemStatus: SystemStatus['status'];          // e.g. 'PROCESSING_SNAPSHOT' | 'REALTIME'  
  statusDetail: string | null;                   // from SystemStatus.detail  
  onlineUsers: number[];  
  lastPresenceUpdate: string | null;  
  _statusTuple: readonly [SystemStatus['status'], string | null];  
  actions: {  
    setBackendConnectionState: (state: ConnectionState) => void;  
    setExtensionConnectionState: (state: ConnectionState) => void;  
    handleSystemStatus: (payload: SystemStatus) => void;  
    handleOnlineUsersUpdate: (  
      payload: { user_ids: number[]; timestamp: string }  
    ) => void;  
  };  
}  
  
export const useSystemStore = create<SystemStoreState>((set, get) => ({  
  backendConnectionState: 'connecting',  
  extensionConnectionState: 'disconnected',  
  systemStatus: 'PROCESSING_SNAPSHOT',  
  statusDetail: 'Initializing...',  
  onlineUsers: [],  
  lastPresenceUpdate: null,  
  _statusTuple: ['PROCESSING_SNAPSHOT', 'Initializing...'] as const,  
  
  actions: {  
    setBackendConnectionState: (state) => {  
      if (get().backendConnectionState !== state) {  
        set({ backendConnectionState: state });  
      }  
    },  
    setExtensionConnectionState: (state) => {  
      if (get().extensionConnectionState !== state) {  
        set({ extensionConnectionState: state });  
      }  
    },  
    handleSystemStatus: (payload) => {  
      const current = get();  
      const newDetail = payload.detail ?? null;  
      if (  
        current.systemStatus !== payload.status ||  
        current.statusDetail !== newDetail  
      ) {  
        set({  
          systemStatus: payload.status,  
          statusDetail: newDetail,  
          _statusTuple: [payload.status, newDetail] as const,  
        });  
      }  
    },  
    handleOnlineUsersUpdate: (payload) => {  
      set({  
        onlineUsers: payload.user_ids,  
        lastPresenceUpdate: payload.timestamp,  
      });  
    },  
  },  
}));  
  
export const useBackendConnectionState = () =>  
  useSystemStore((s) => s.backendConnectionState);  
  
export const useExtensionConnectionState = () =>  
  useSystemStore((s) => s.extensionConnectionState);  
  
export const useSystemStatus = () =>  
  useSystemStore((s) => s._statusTuple);  
  
export const systemStoreActions = useSystemStore.getState().actions;  