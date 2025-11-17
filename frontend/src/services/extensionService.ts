// src/services/extensionService.ts  
import { SendMessageCommand } from '@/types/backend-wss';  
import { systemStoreActions } from '@store/systemStore';  
  
class ExtensionService {  
  /** Checks whether the Agent (Chrome extension) is available. */  
  public isAgentAvailable(): boolean {  
    return typeof chrome !== 'undefined' &&  
      !!chrome.runtime &&  
      !!chrome.runtime.id;  
  }  
  
  /** Actively ping the extension to update store state. */  
  public checkConnection(): void {  
    if (!this.isAgentAvailable()) {  
      systemStoreActions.setExtensionConnectionState('disconnected');  
      return;  
    }  
    try {  
      chrome.runtime.sendMessage({ type: 'ping' }, () => {  
        if (chrome.runtime.lastError) {  
          systemStoreActions.setExtensionConnectionState('error');  
        } else {  
          systemStoreActions.setExtensionConnectionState('connected');  
        }  
      });  
    } catch {  
      systemStoreActions.setExtensionConnectionState('error');  
    }  
  }  
  
  /** Internal helper for sending a message to the Agent. */  
  private sendMessage<T>(type: string, payload: unknown): Promise<T> {  
    return new Promise((resolve, reject) => {  
      if (!this.isAgentAvailable()) {  
        return reject(new Error('[ExtensionService] Agent not available'));  
      }  
  
      try {  
        chrome.runtime.sendMessage({ type, payload }, (response: T) => {  
          if (chrome.runtime.lastError) {  
            return reject(  
              new Error(  
                `[ExtensionService] Messaging error: ${chrome.runtime.lastError.message}`  
              )  
            );  
          }  
          resolve(response);  
        });  
      } catch (error) {  
        reject(error);  
      }  
    });  
  }  
  
  public getAllChatsFromDB(): Promise<unknown> {  
    return this.sendMessage('get_all_chats_from_db', null);  
  }  
  
  public getAllMessagesFromDB(chatId: string): Promise<unknown> {  
    return this.sendMessage('get_all_messages_from_db', { chatId });  
  }  
  
  public executeAgentCommand(command: SendMessageCommand): Promise<unknown> {  
    return this.sendMessage('execute_agent_command', command);  
  }  
}  
  
export const extensionService = new ExtensionService();  