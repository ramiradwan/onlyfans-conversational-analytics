/* content.js — Isolated World Message Bridge  
   Matches: Full-Stack Comm Spec v1.1.0 + Expert Review Injection Strategy  
*/  
console.log("[Content.js] Isolated bridge loaded");  
  
/**  
 * UPSTREAM FLOW (Page ➔ Agent ➔ Brain)  
 */  
window.addEventListener(  
  "message",  
  event => {  
    if (event.source !== window || !event.data?.type) return;  
  
    if (event.data.type === "_OF_FORWARDER_") {  
      const payload = event.data.payload;  
      if (!payload || typeof payload !== "object") {  
        console.warn("[Content.js] Ignored malformed forwarder payload:", event.data);  
        return;  
      }  
      chrome.runtime.sendMessage({ type: "_OF_FORWARDER_", payload });  
    }  
  },  
  false  
);  
  
/**  
 * DOWNSTREAM FLOW (Agent ➔ Page)  
 */  
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {  
  if (sender.id !== chrome.runtime.id) return;  
  
  if (message.type === "_OF_BACKEND_") {  
    window.postMessage({ type: "_OF_BACKEND_", payload: message.payload }, "*");  
    sendResponse({ ok: true });  
    return;  
  }  
  if (["connection_status", "connection_ack", "system_status", "online_users_update"].includes(message.type)) {  
    window.postMessage({ type: "_OF_BACKEND_", payload: message }, "*");  
    sendResponse({ ok: true });  
    return;  
  }  
});  