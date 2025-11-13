/* background.js — MV3 Service Worker  
   Implements: Full-Stack Comm Spec v1.1.0 + Expert Review Injection Strategy  
*/  
console.log("[Agent SW] Loaded");  
  
// ==================== Config ====================  
const WS_BASE = "ws://localhost:8000/api/ws";  
const CLIENT_TYPE = "extension";  
let USER_ID = "demo_user";  
let WS_URL = `${WS_BASE}/${CLIENT_TYPE}/${USER_ID}`;  
  
let ws = null;  
let keepAliveIntervalId = null;  
  
// Track tabs injected with page-hook.js to avoid duplicate injection  
const injectedTabs = new Set();  
  
// ==================== IndexedDB Layer ====================  
const DB_NAME = "OnlyFansAnalyticsDB";  
const DB_VERSION = 1;  
  
function openDB() {  
  return new Promise((resolve, reject) => {  
    const req = indexedDB.open(DB_NAME, DB_VERSION);  
    req.onerror = () => reject(req.error);  
    req.onupgradeneeded = e => {  
      const db = e.target.result;  
      if (!db.objectStoreNames.contains("messages"))  
        db.createObjectStore("messages", { keyPath: "id" });  
      if (!db.objectStoreNames.contains("chats"))  
        db.createObjectStore("chats", { keyPath: "id" });  
      if (!db.objectStoreNames.contains("users"))  
        db.createObjectStore("users", { keyPath: "id" });  
    };  
    req.onsuccess = () => resolve(req.result);  
  });  
}  
  
function isValidRecord(obj) {  
  const id = obj?.id;  
  return id !== undefined && id !== null && id !== "" && typeof id !== "object";  
}  
  
async function dbPut(store, record) {  
  if (!isValidRecord(record)) return;  
  try {  
    const db = await openDB();  
    db.transaction(store, "readwrite").objectStore(store).put(record);  
  } catch (err) {  
    console.error(`[Agent SW] dbPut error in store ${store}:`, err);  
  }  
}  
  
async function dbBulkPut(store, records) {  
  try {  
    const db = await openDB();  
    const os = db.transaction(store, "readwrite").objectStore(store);  
    records.forEach(r => { if (isValidRecord(r)) os.put(r); });  
  } catch (err) {  
    console.error(`[Agent SW] dbBulkPut error in store ${store}:`, err);  
  }  
}  
  
// ==================== Normalizers ====================  
function normalizeChat(c, i = 0) {  
  const id =  
    c.id ||  
    c.chat_id ||  
    c?.chat?.id ||  
    c.withUser?.id ||  
    c.user_id ||  
    `chat_${Date.now()}_${i}`;  
  return { ...c, id };  
}  
  
function normalizeMessage(m, i = 0) {  
  const id = m.id || m.message_id || `msg_${Date.now()}_${i}`;  
  let previews = Array.isArray(m.previews)  
    ? m.previews.filter(p => typeof p === "object" && p !== null)  
    : [];  
  return { ...m, id, previews };  
}  
  
// ==================== Metadata Recovery ====================  
async function recoverMissingMetadata(msg) {  
  try {  
    if ((msg.chat_id && msg.chat_id !== "unknown") && msg.fromUser) {  
      return msg;  
    }  
    const db = await openDB();  
    const store = db.transaction("messages", "readonly").objectStore("messages");  
    return await new Promise(resolve => {  
      const getReq = store.get(msg.id);  
      getReq.onsuccess = ev => {  
        const cached = ev.target.result;  
        if (cached) {  
          msg.chat_id = msg.chat_id && msg.chat_id !== "unknown"  
            ? msg.chat_id  
            : cached.chat_id || "unknown";  
          msg.fromUser = msg.fromUser || cached.fromUser || null;  
        }  
        resolve(msg);  
      };  
      getReq.onerror = () => resolve(msg);  
    });  
  } catch {  
    return msg;  
  }  
}  
  
// ==================== WS Connection & Keepalive ====================  
function connectWS() {  
  console.log("[Agent SW] Connecting:", WS_URL);  
  stopKeepAlive();  
  
  if (ws) {  
    try { ws.close(); } catch {}  
    ws = null;  
  }  
  
  ws = new WebSocket(WS_URL);  
  
  ws.onopen = () => {  
    console.log("[Agent SW] WS open for USER_ID:", USER_ID);  
    sendCacheUpdate().catch(err => console.error("[Agent SW] sendCacheUpdate error:", err));  
    startKeepAlive();  
  };  
  
  ws.onmessage = event => {  
    try {  
      const msg = JSON.parse(event.data);  
      if (msg.type === "command_to_execute") {  
        forwardCommandToPage(msg.payload);  
      } else if (msg.type === "connection_ack") {  
        console.log("[Agent SW] Connection acknowledged:", msg.payload);  
        if (msg.payload?.userId && msg.payload.userId !== USER_ID) {  
          USER_ID = msg.payload.userId;  
          chrome.storage.local.set({ user_id: USER_ID });  
        }  
      }  
    } catch (err) {  
      console.error("[Agent SW] WS parse error:", err);  
    }  
  };  
  
  ws.onclose = () => {  
    console.log("[Agent SW] WS closed — reconnecting in 5s");  
    stopKeepAlive();  
    setTimeout(connectWS, 5000);  
  };  
  
  ws.onerror = err => {  
    console.error("[Agent SW] WS error:", err);  
  };  
}  
  
function startKeepAlive() {  
  stopKeepAlive();  
  keepAliveIntervalId = setInterval(() => {  
    if (ws && ws.readyState === WebSocket.OPEN) {  
      ws.send(JSON.stringify({  
        type: "keepalive",  
        payload: { timestamp: new Date().toISOString() }  
      }));  
    }  
  }, 20000);  
}  
  
function stopKeepAlive() {  
  if (keepAliveIntervalId) clearInterval(keepAliveIntervalId);  
  keepAliveIntervalId = null;  
}  
  
// ==================== Snapshot Flow ====================  
async function sendCacheUpdate() {  
  try {  
    const db = await openDB();  
    const chats = await new Promise((resolve, reject) => {  
      const req = db.transaction("chats", "readonly").objectStore("chats").getAll();  
      req.onsuccess = () => resolve(req.result.map(normalizeChat));  
      req.onerror = () => reject(req.error);  
    });  
    const messages = await new Promise((resolve, reject) => {  
      const req = db.transaction("messages", "readonly").objectStore("messages").getAll();  
      req.onsuccess = () => resolve(req.result.map(normalizeMessage));  
      req.onerror = () => reject(req.error);  
    });  
    if (ws && ws.readyState === WebSocket.OPEN) {  
      ws.send(JSON.stringify({  
        type: "cache_update",  
        payload: { chats, messages }  
      }));  
      console.log(`[Agent SW] Sent cache_update: ${chats.length} chats, ${messages.length} messages`);  
    }  
  } catch (err) {  
    console.error("[Agent SW] sendCacheUpdate error:", err);  
  }  
}  
  
// ==================== Delta Flow ====================  
async function sendNewRawMessage(messageObj) {  
  const message = await recoverMissingMetadata(normalizeMessage(messageObj));  
  if (ws && ws.readyState === WebSocket.OPEN) {  
    ws.send(JSON.stringify({  
      type: "new_raw_message",  
      payload: { message }  
    }));  
    console.log("[Agent SW] Sent new_raw_message", message.id);  
  }  
}  
  
// ==================== Online Users Update Flow ====================  
function sendOnlineUsersUpdate(userIds) {  
  if (!Array.isArray(userIds) || !userIds.every(id => Number.isInteger(id))) return;  
  if (ws && ws.readyState === WebSocket.OPEN) {  
    ws.send(JSON.stringify({  
      type: "online_users_update",  
      payload: { user_ids: userIds, timestamp: new Date().toISOString() }  
    }));  
  }  
}  
  
// ==================== Command Flow ====================  
async function forwardCommandToPage(commandPayload) {  
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });  
  tabs.forEach(tab => {  
    chrome.tabs.sendMessage(tab.id, { type: "_OF_BACKEND_", payload: commandPayload }, () => {  
      if (chrome.runtime.lastError) {  
        console.error("[Agent SW] forwardCommandToPage error:", chrome.runtime.lastError);  
      }  
    });  
  });  
}  
  
// ==================== Injection Service (Expert Review Strategy) ====================  
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {  
  if (changeInfo.status === "loading" && tab.url?.startsWith("https://onlyfans.com")) {  
    if (injectedTabs.has(tabId)) return;  
    injectedTabs.add(tabId);  
    console.log(`[Agent SW] Injecting page-hook.js into tab ${tabId}`);  
    chrome.scripting.executeScript({  
      target: { tabId, allFrames: false },  
      files: ["page-hook.js"],  
      world: "MAIN"  
    }).catch(err => console.error("[Agent SW] Failed to inject page-hook.js:", err));  
  }  
});  
chrome.tabs.onRemoved.addListener(tabId => injectedTabs.delete(tabId));  
  
// ==================== External Messaging (Bridge ➔ Agent) ====================  
chrome.runtime.onMessageExternal.addListener((request, sender, sendResponse) => {  
  if (request.type === "get_all_chats_from_db") {  
    openDB().then(db => {  
      const req = db.transaction("chats", "readonly").objectStore("chats").getAll();  
      req.onsuccess = () => sendResponse({ success: true, data: req.result.map(normalizeChat) });  
    }).catch(err => sendResponse({ success: false, error: String(err) }));  
    return true;  
  }  
  if (request.type === "get_all_messages_from_db") {  
    openDB().then(db => {  
      const req = db.transaction("messages", "readonly").objectStore("messages").getAll();  
      req.onsuccess = () => sendResponse({ success: true, data: req.result.map(normalizeMessage) });  
    }).catch(err => sendResponse({ success: false, error: String(err) }));  
    return true;  
  }  
  if (request.type === "send_cache_update") {  
    sendCacheUpdate()  
      .then(() => sendResponse({ success: true }))  
      .catch(err => sendResponse({ success: false, error: String(err) }));  
    return true;  
  }  
});  
  
// ==================== Internal Messaging (Content ➔ Background) ====================  
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {  
  if (message.type === "get_user_id") {  
    chrome.storage.local.get("user_id", (res) => {  
      sendResponse({ userId: res.user_id || USER_ID || "demo_user" });  
    });  
    return true;  
  }  
  if (message.type === "_OF_USER_ID_SET_") {  
    const newId = (message.payload?.user_id || "").trim();  
    if (newId && /^\d+$/.test(newId)) {  
      if (newId === USER_ID) {  
        console.log("[Agent SW] USER_ID unchanged:", USER_ID);  
        sendResponse({ ok: true });  
        return true;  
      }  
      USER_ID = newId;  
      chrome.storage.local.set({ user_id: USER_ID }, () => {  
        WS_URL = `${WS_BASE}/${CLIENT_TYPE}/${USER_ID}`;  
        console.log("[Agent SW] Reconnecting WS with creator USER_ID:", USER_ID);  
        connectWS();  
      });  
      sendResponse({ ok: true });  
      return true;  
    } else {  
      console.warn("[Agent SW] Invalid USER_ID payload:", message.payload?.user_id);  
      sendResponse({ ok: false, reason: "invalid_user_id" });  
      return true;  
    }  
  }  
  if (sender.tab && message.type === "_OF_FORWARDER_") {  
    const payload = message.payload;  
    if (payload.event === "fetch_response" && payload.body) {  
      try {  
        const data = JSON.parse(payload.body);  
        if (Array.isArray(data.messages)) {  
          dbBulkPut("messages", data.messages.map(normalizeMessage));  
        }  
        if (Array.isArray(data.chats)) {  
          dbBulkPut("chats", data.chats.map(normalizeChat));  
        }  
      } catch (err) {  
        console.warn("[Agent SW] Failed to parse fetch_response body:", err);  
      }  
      sendResponse({ ok: true });  
      return true;  
    }  
    if (payload.event === "fetch_request" && payload.body) {  
      try {  
        const reqData = JSON.parse(payload.body);  
        if (reqData.text) {  
          const msg = normalizeMessage({ text: reqData.text });  
          dbPut("messages", msg);  
          sendNewRawMessage(msg);  
        }  
      } catch {}  
      sendResponse({ ok: true });  
      return true;  
    }  
    if (payload.event === "ws_message" && payload.data) {  
      if (Array.isArray(payload.data.online) && payload.data.online.every(id => Number.isInteger(id))) {  
        sendOnlineUsersUpdate(payload.data.online);  
        sendResponse({ ok: true });  
        return true;  
      }  
      sendNewRawMessage(payload.data);  
      sendResponse({ ok: true });  
      return true;  
    }  
  }  
  sendResponse({ ok: true });  
  return true;  
});  
  
// ==================== Init ====================  
chrome.storage.local.get("user_id", res => {  
  USER_ID = res.user_id || "demo_user";  
  WS_URL = `${WS_BASE}/${CLIENT_TYPE}/${USER_ID}`;  
  connectWS();  
});  