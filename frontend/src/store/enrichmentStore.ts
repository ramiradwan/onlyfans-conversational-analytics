import { create } from 'zustand';  
import { EnrichmentResultPayload } from '@/types/backend-wss';  
  
export interface EnrichmentStoreState {  
  enrichmentsByConversation: Record<string, EnrichmentResultPayload>;  
  actions: {  
    handleEnrichmentResult: (payload: EnrichmentResultPayload) => void;  
  };  
}  
  
export const useEnrichmentStore = create<EnrichmentStoreState>((set, get) => ({  
  enrichmentsByConversation: {},  
  actions: {  
    handleEnrichmentResult: (payload) => {  
      const convoId = payload.conversationId;  
      if (!convoId) return;  
  
      const current = get().enrichmentsByConversation[convoId];  
      if (JSON.stringify(current) !== JSON.stringify(payload)) {  
        set((state) => ({  
          enrichmentsByConversation: {  
            ...state.enrichmentsByConversation,  
            [convoId]: payload,  
          },  
        }));  
      }  
    },  
  },  
}));  
  
export const enrichmentStoreActions = useEnrichmentStore.getState().actions;  