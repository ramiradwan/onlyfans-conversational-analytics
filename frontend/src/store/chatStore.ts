import { create } from 'zustand';  
import { ExtendedConversationNode, Message, FullSyncResponse } from '@/types/backend-wss';  
  
const extractMessagesByConversation = (  
  conversations: ExtendedConversationNode[],  
): Record<string, Message[]> => {  
  return conversations.reduce((acc, convo) => {  
    const convoId = convo.conversationId;  
    if (!acc[convoId]) {  
      acc[convoId] = [];  
    }  
    const sortedMessages = (convo.messages || []).slice().sort(  
      (a, b) =>  
        new Date(a.createdAt ?? 0).getTime() -  
        new Date(b.createdAt ?? 0).getTime(),  
    );  
    acc[convoId] = sortedMessages;  
    return acc;  
  }, {} as Record<string, Message[]>);  
};  
  
const mapConversationsById = (  
  conversations: ExtendedConversationNode[],  
): Record<string, ExtendedConversationNode> => {  
  return conversations.reduce((acc, convo) => {  
    acc[convo.conversationId] = convo;  
    return acc;  
  }, {} as Record<string, ExtendedConversationNode>);  
};  
  
type LoadingState = 'idle' | 'loading' | 'success' | 'error';  
  
export interface ChatStoreState {  
  conversations: Record<string, ExtendedConversationNode>;  
  messagesByConversation: Record<string, Message[]>;  
  loadingState: LoadingState;  
  actions: {  
    replaceStateFromSnapshot: (payload: FullSyncResponse) => void;  
    handleFullSync: (payload: FullSyncResponse) => void;  
    handleAppendConversation: (payload: ExtendedConversationNode) => void;  
    setLoadingState: (state: LoadingState) => void;  
  };  
}  
  
export const useChatStore = create<ChatStoreState>((set, get) => ({  
  conversations: {},  
  messagesByConversation: {},  
  loadingState: 'idle',  
  actions: {  
    replaceStateFromSnapshot: (payload) => {  
      get().actions.handleFullSync(payload);  
    },  
  
    handleFullSync: (payload) => {  
      const { conversations = [] } = payload;  
      const convosById = mapConversationsById(conversations);  
      const messagesById = extractMessagesByConversation(conversations);  
  
      const current = get();  
      // ✅ Reference equality check to prevent no-op updates  
      if (  
        current.conversations !== convosById ||  
        current.messagesByConversation !== messagesById ||  
        current.loadingState !== 'success'  
      ) {  
        set({  
          conversations: convosById,  
          messagesByConversation: messagesById,  
          loadingState: 'success',  
        });  
      }  
    },  
  
    handleAppendConversation: (conversation) => {  
      const convoId = conversation.conversationId;  
      if (!convoId) return;  
  
      const sortedMessages = (conversation.messages || []).slice().sort(  
        (a, b) =>  
          new Date(a.createdAt ?? 0).getTime() -  
          new Date(b.createdAt ?? 0).getTime(),  
      );  
  
      const current = get();  
      const currentConvo = current.conversations[convoId];  
      const currentMessages = current.messagesByConversation[convoId];  
  
      // ✅ Only update if references differ  
      if (  
        currentConvo !== conversation ||  
        currentMessages !== sortedMessages  
      ) {  
        set((state) => ({  
          conversations: {  
            ...state.conversations,  
            [convoId]: conversation,  
          },  
          messagesByConversation: {  
            ...state.messagesByConversation,  
            [convoId]: sortedMessages,  
          },  
        }));  
      }  
    },  
  
    setLoadingState: (state) => {  
      if (get().loadingState !== state) {  
        set({ loadingState: state });  
      }  
    },  
  },  
}));  
  
export const chatStoreActions = useChatStore.getState().actions;  