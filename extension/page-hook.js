/* page-hook.js — MAIN world network interceptor for OnlyFans  
   Injected securely via chrome.scripting.executeScript in background.js  
   Captures network events and forwards them to content.js (_OF_FORWARDER_)  
   Receives commands from content.js (_OF_BACKEND_) and executes them  
*/  
  
(function () {  
  // Prevent double injection  
  if (window.__OF_HOOK_ACTIVE__) return;  
  window.__OF_HOOK_ACTIVE__ = true;  
  
  console.log("[OF Hook] MAIN world interceptor active");  
  
  const FORWARD_TYPE = "_OF_FORWARDER_";  
  const BACKEND_TYPE = "_OF_BACKEND_";  
  
  // ========== Helper: Safe Forward ==========  
  function forward(payload) {  
    if (!payload || typeof payload !== "object" || !payload.event) {  
      console.warn("[OF Hook] Ignored malformed payload:", payload);  
      return;  
    }  
    window.postMessage({ type: FORWARD_TYPE, payload }, "*");  
  }  
  
  // ========== User ID Detection ==========  
  function detectUserIdFromApi(url, parsedBody) {  
    let uid = null;  
    if (/\/api2\/v2\/users\/me/.test(url) && typeof parsedBody?.id === "number") {  
      uid = parsedBody.id;  
    } else if (/\/api2\/v2\/init/.test(url) && typeof parsedBody?.user?.id === "number") {  
      uid = parsedBody.user.id;  
    } else if (/\/api2\/v2\/chats(\?|$)/.test(url) && Array.isArray(parsedBody?.list)) {  
      const meChat = parsedBody.list.find(  
        c => (c.withUser?.is_me === true || c.withUser?.me === true) && typeof c.withUser?.id === "number"  
      );  
      if (meChat) uid = meChat.withUser.id;  
    }  
    if (uid) {  
      forward({ event: "set_user_id", user_id: String(uid) });  
      console.log("[OF Hook] Detected creator user_id:", uid);  
    }  
  }  
  
  // ========== Target URL Patterns ==========  
  const FETCH_URL_PATTERNS = [  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats(\?|$)/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/messages(\?|$)/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/messages\/\d+\/like$/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/messages$/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/mark-as-read$/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/users\/\d+\/chats(\?|$)/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/users\/me(\?|$)/,  
    /^https:\/\/onlyfans\.com\/api2\/v2\/init(\?|$)/  
  ];  
  
  const WSS_URL_PATTERN = "wss://ws2.onlyfans.com/ws3/";  
  
  // ========== WebSocket Interception ==========  
  try {  
    const OriginalWS = window.WebSocket;  
    window.WebSocket = function (url, protocols) {  
      const ws = protocols ? new OriginalWS(url, protocols) : new OriginalWS(url);  
      if (url.startsWith(WSS_URL_PATTERN)) {  
        console.log("[OF Hook] Capturing OnlyFans WS:", url);  
        ws.addEventListener("message", event => {  
          try {  
            const parsed = JSON.parse(event.data);  
            if (parsed && typeof parsed === "object") {  
              if (Array.isArray(parsed.online) && parsed.online.every(id => Number.isInteger(id))) {  
                forward({ event: "ws_message", url, data: { online: parsed.online } });  
                return;  
              }  
              forward({ event: "ws_message", url, data: parsed });  
            }  
          } catch (err) {  
            console.warn("[OF Hook] WS parse error:", err);  
          }  
        });  
        window.activeOfSocket = ws;  
      }  
      return ws;  
    };  
  } catch (err) {  
    console.error("[OF Hook] Failed to patch WebSocket:", err);  
  }  
  
  // ========== Fetch Interception ==========  
  try {  
    const originalFetch = window.fetch;  
    window.fetch = async function (input, init) {  
      const url = typeof input === "string" ? input : input.url;  
      const isTarget = FETCH_URL_PATTERNS.some(p => p.test(url));  
      if (!isTarget) return originalFetch.apply(this, arguments);  
  
      if (init?.body) forward({ event: "fetch_request", url, body: init.body });  
  
      const response = await originalFetch.apply(this, arguments);  
      const cloned = response.clone();  
  
      cloned.text().then(body => {  
        let payload = { event: "fetch_response", url, body };  
        try {  
          const parsedBody = JSON.parse(body);  
          detectUserIdFromApi(url, parsedBody);  
          if (/\/api2\/v2\/chats(\?|$)/.test(url)) {  
            let chatsArray = Array.isArray(parsedBody?.list) ? parsedBody.list : [];  
            chatsArray = chatsArray.map(c => ({  
              ...c,  
              id: c.id || c.chat_id || "unknown",  
              createdAt: c.createdAt || c.created_at || new Date().toISOString()  
            }));  
            payload = { event: "fetch_response", url, body: JSON.stringify({ chats: chatsArray }) };  
          }  
          if (/\/chats\/\d+\/messages/.test(url)) {  
            let msgsArray = Array.isArray(parsedBody?.messages) ? parsedBody.messages : [];  
            msgsArray = msgsArray.map(m => ({  
              ...m,  
              id: m.id || m.message_id || `msg_${Date.now()}`,  
              createdAt: m.createdAt || m.created_at || new Date().toISOString(),  
              previews: Array.isArray(m.previews)  
                ? m.previews.filter(p => typeof p === "object" && p !== null)  
                : []  
            }));  
            payload = { event: "fetch_response", url, body: JSON.stringify({ messages: msgsArray }) };  
          }  
        } catch {}  
        forward(payload);  
      }).catch(e => console.warn("[OF Hook] Failed to read response body:", e));  
  
      return response;  
    };  
  } catch (err) {  
    console.error("[OF Hook] Failed to patch fetch:", err);  
  }  
  
  // ========== XHR Interception ==========  
  try {  
    const open = XMLHttpRequest.prototype.open;  
    XMLHttpRequest.prototype.open = function (...args) {  
      this._url = args[1];  
      return open.apply(this, args);  
    };  
    const send = XMLHttpRequest.prototype.send;  
    XMLHttpRequest.prototype.send = function (body) {  
      const url = this._url || "";  
      const isTarget = FETCH_URL_PATTERNS.some(p => p.test(url));  
      if (isTarget) {  
        if (body) forward({ event: "fetch_request", url, body });  
        this.addEventListener("load", function () {  
          let payload = { event: "fetch_response", url, body: this.responseText };  
          try {  
            const parsedBody = JSON.parse(this.responseText);  
            detectUserIdFromApi(url, parsedBody);  
          } catch {}  
          forward(payload);  
        });  
      }  
      return send.call(this, body);  
    };  
  } catch (err) {  
    console.error("[OF Hook] Failed to patch XHR:", err);  
  }  
  
  // ========== Command Execution ==========  
  window.addEventListener("message", event => {  
    if (event.source !== window || event.data?.type !== BACKEND_TYPE) return;  
    const payload = event.data.payload;  
  
    if (payload?.action === "send_ws_message" && window.activeOfSocket) {  
      try {  
        window.activeOfSocket.send(JSON.stringify(payload.data));  
      } catch (err) {  
        console.error("[OF Hook] Failed to send WS message:", err);  
      }  
    }  
  
    if (payload?.action === "send_fetch_command" && payload.url && payload.init) {  
      const allowed = [  
        /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/messages$/,  
        /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/mark-as-read$/,  
        /^https:\/\/onlyfans\.com\/api2\/v2\/messages\/\d+\/like$/  
      ].some(p => p.test(payload.url));  
      if (!allowed) {  
        return console.warn("[OF Hook] Blocked disallowed fetch:", payload.url);  
      }  
      fetch(payload.url, payload.init).catch(e => console.error("[OF Hook] Fetch fail:", e));  
    }  
  });  
  
})();  