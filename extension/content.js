// content.js — Bridge between page context and extension background (real-time)  
  
// === Relay messages from the page context to background.js ===  
window.addEventListener("message", event => {  
  if (event.source !== window) return;  
  if (event.data?.__OF_FORWARDER__) {  
    // Debug log for development; remove or disable in prod  
    console.debug("[Content.js] Forwarding page → background:", event.data.payload?.type || event.data);  
    chrome.runtime.sendMessage(event.data);  
  }  
});  
  
// === Relay messages from background.js to the page context ===  
chrome.runtime.onMessage.addListener(msg => {  
  if (msg?.fromServer || msg?.__OF_BACKEND__ || msg?.type === "connection_status") {  
    console.debug("[Content.js] Forwarding background → page:", msg);  
    window.postMessage({ __OF_BACKEND__: true, payload: msg.payload || msg }, "*");  
  }  
});  
  
// === Inject page-hook.js BEFORE site scripts execute ===  
(function injectPageHook() {  
  const script = document.createElement("script");  
  script.src = chrome.runtime.getURL("page-hook.js");  
  script.type = "text/javascript";  
  // Prepend ensures our hook runs before OF scripts attach fetch/WebSocket  
  document.documentElement.prepend(script);  
  console.log("[Content.js] Injected page-hook.js before site scripts");  
})();  