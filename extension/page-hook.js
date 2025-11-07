// page-hook.js — Passive interceptor for OnlyFans chat API (real-time optimized)  
(function () {  
  const FORWARD_FLAG = "__OF_FORWARDER__";  
  const BACKEND_FLAG = "__OF_BACKEND__";  
  
  function forward(payload) {  
    // Sends payload to extension background.js via window.postMessage  
    window.postMessage({ [FORWARD_FLAG]: true, payload }, "*");  
  }  
  
  // Targeted API patterns we care about  
  const FETCH_URL_PATTERNS = [  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats(\?|$)/,                 // chat list  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/messages(\?|$)/,  // history GET  
    /^https:\/\/onlyfans\.com\/api2\/v2\/messages\/\d+\/like$/,         // like POST  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/messages$/,        // send POST  
    /^https:\/\/onlyfans\.com\/api2\/v2\/chats\/\d+\/mark-as-read$/,    // mark read POST  
    /^https:\/\/onlyfans\.com\/api2\/v2\/users\/\d+\/chats(\?|$)/       // user-specific chat list  
  ];  
  
  const WSS_URL_PATTERN = "wss://ws2.onlyfans.com/ws3/";  
  
  // === WebSocket interception ===  
  const OriginalWS = window.WebSocket;  
  window.WebSocket = function (url, protocols) {  
    if (!url.startsWith(WSS_URL_PATTERN)) {  
      // Not an OF chat socket — pass through untouched  
      return protocols ? new OriginalWS(url, protocols) : new OriginalWS(url);  
    }  
  
    console.log("[OF Hook] Capturing OnlyFans WS:", url);  
    const ws = protocols ? new OriginalWS(url, protocols) : new OriginalWS(url);  
  
    // Capture incoming WS messages  
    ws.addEventListener("message", event => {  
      forward({ type: "ws_message", url, data: event.data });  
    });  
  
    ws.addEventListener("close", () => {  
      console.log("[OF Hook] WS closed");  
    });  
  
    // Keep a reference so commands can send via WS  
    window.activeOfSocket = ws;  
    return ws;  
  };  
  
  // === Fetch interception ===  
  const originalFetch = window.fetch;  
  window.fetch = async function (input, init) {  
    const url = typeof input === "string" ? input : input.url;  
    const isTarget = FETCH_URL_PATTERNS.some(p => p.test(url));  
    if (!isTarget) {  
      return originalFetch.apply(this, arguments);  
    }  
  
    // Capture outbound request body  
    if (init && init.body) {  
      forward({ type: "fetch_request", url, body: init.body });  
    }  
  
    // Capture response body  
    const response = await originalFetch.apply(this, arguments);  
    try {  
      const cloned = response.clone();  
      cloned.text().then(body => {  
        forward({ type: "fetch_response", url, body });  
      });  
    } catch (e) {  
      console.warn("[OF Hook] Failed to clone response", e);  
    }  
    return response;  
  };  
  
  // === XHR interception ===  
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
      if (body) {  
        try { forward({ type: "fetch_request", url, body }); } catch {}  
      }  
      this.addEventListener("load", function () {  
        try {  
          forward({ type: "fetch_response", url, body: this.responseText });  
        } catch {}  
      });  
    }  
    return send.call(this, body);  
  };  
  
  // === Commands from extension/backend ===  
  window.addEventListener("message", event => {  
    if (event.source !== window || !event.data?.[BACKEND_FLAG]) return;  
    const payload = event.data.payload;  
  
    // Backend wants to send a WS message  
    if (payload?.action === "send_ws_message" && window.activeOfSocket) {  
      try {  
        window.activeOfSocket.send(JSON.stringify(payload.data));  
      } catch (err) {  
        console.error("[OF Hook] Failed to send WS message", err);  
      }  
    }  
  
    // Backend wants to send a fetch command  
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
  
  console.log("[OF Hook] Passive page-hook active (real-time mode).");  
})();  