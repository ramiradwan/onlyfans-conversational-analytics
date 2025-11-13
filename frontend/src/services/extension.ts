// src/services/extension.ts  
/**  
 * ExtensionService  
 * ----------------  
 * Sole interface for sending messages from the Bridge (frontend React app)  
 * to the Agent (Chrome extension background service worker).  
 *  
 * Complies with Communication Spec §2.3.2:  
 *  - Wraps chrome.runtime.sendMessage in a Promise.  
 *  - Checks chrome.runtime.lastError inside callback before resolving.  
 *  - Supports generic payload typing for type safety.  
 */  
  
export function sendMessageToAgent<T>(  
  type: string,  
  payload: T  
): Promise<void> {  
  return new Promise((resolve, reject) => {  
    try {  
      // This works in MV3 service worker contexts  
      chrome.runtime.sendMessage({ type, payload }, () => {  
        if (chrome.runtime.lastError) {  
          // lastError is set if the background page/service worker is inactive or the message fails  
          reject(new Error(chrome.runtime.lastError.message));  
          return;  
        }  
        resolve();  
      });  
    } catch (err) {  
      reject(err instanceof Error ? err : new Error(String(err)));  
    }  
  });  
}  
  
/**  
 * Example usage:  
 *  
 * import { sendMessageToAgent } from '../services/extension';  
 * import { SendMessageCommand } from '../types/backend-wss';  
 *  
 * sendMessageToAgent<SendMessageCommand>('execute_agent_command', {  
 *   chat_id: '12345',  
 *   text: 'Hello world!',  
 * });  
 */  