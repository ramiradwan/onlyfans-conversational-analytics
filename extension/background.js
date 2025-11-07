// background.js — Passive DB backend for OnlyFans Analytics (real-time sync)  
  
console.log("[Extension] background.js ready — passive capture active.");  
  
const DB_NAME = "OnlyFansAnalyticsDB";  
const DB_VERSION = 1;  
const WS_URL = "ws://localhost:8000/api/ws/extension"; // Backend WebSocket URL  
let ws;  
  
// ============================================================================  
// IndexedDB Layer  
// ============================================================================  
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
        };  
        req.onsuccess = () => resolve(req.result);  
    });  
}  
  
async function dbPut(store, record) {  
    if (!isValidRecord(record)) {  
        console.warn("[DB] Skipping invalid record:", record);  
        return;  
    }  
    const db = await openDB();  
    const tx = db.transaction(store, "readwrite");  
    tx.objectStore(store).put(record);  
    return new Promise((res, rej) => {  
        tx.oncomplete = () => res(true);  
        tx.onerror = () => rej(tx.error);  
    });  
}  
  
async function dbBulkPut(store, records) {  
    const db = await openDB();  
    const tx = db.transaction(store, "readwrite");  
    const os = tx.objectStore(store);  
    let count = 0;  
    for (const r of records) {  
        if (isValidRecord(r)) {  
            os.put(r);  
            count++;  
        } else {  
            console.warn("[DB] Skipped invalid record:", r);  
        }  
    }  
    return new Promise((res, rej) => {  
        tx.oncomplete = () => {  
            console.log(`[DB] Wrote ${count} valid records to ${store}`);  
            res(true);  
        };  
        tx.onerror = () => rej(tx.error);  
    });  
}  
  
function isValidRecord(obj) {  
    const id = obj?.id;  
    return id !== undefined && id !== null && id !== "" && typeof id !== "object";  
}  
  
// ============================================================================  
// Normalization Helpers  
// ============================================================================  
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
    return { ...m, id };  
}  
  
// ============================================================================  
// Parsers  
// ============================================================================  
function stripHTML(t) {  
    return t ? t.replace(/<\/?[^>]+(>|$)/g, "") : null;  
}  
  
function parseWS(payload) {  
    try {  
        const d = JSON.parse(payload);  
        const msg = d?.api2_chat_message;  
        if (!msg || msg.responseType !== "message") return null;  
        return {  
            ...msg,  
            type: "inbound_message",  
            text: stripHTML(msg.text),  
            is_inbound: true  
        };  
    } catch (e) {  
        console.error("[ParseWS] fail", e);  
        return null;  
    }  
}  
  
function parseFetch(url, body, isResponse) {  
    try {  
        if (/\/api2\/v2\/chats(\/)?(\?|$)/.test(url) && isResponse) {  
            const data = JSON.parse(body);  
            const list =  
                data.list ||  
                data.chats ||  
                data.response?.list ||  
                data.response?.chats ||  
                (Array.isArray(data) ? data : null);  
            if (Array.isArray(list)) return { type: "chat_list", chats: list };  
            console.warn("[ParseFetch] Unknown chat list format", data);  
            return null;  
        }  
        if (/\/api2\/v2\/chats\/\d+\/messages/.test(url) && isResponse) {  
            const data = JSON.parse(body);  
            const list =  
                data.list ||  
                data.messages ||  
                data.response?.list ||  
                data.response?.messages ||  
                (Array.isArray(data) ? data : null);  
            if (!Array.isArray(list)) return null;  
            const chat_id = url.match(/\/chats\/(\d+)\//)?.[1];  
            return {  
                type: "history",  
                messages: list.map(m => ({  
                    ...m,  
                    chat_id,  
                    text: stripHTML(m.text)  
                }))  
            };  
        }  
        if (/\/api2\/v2\/chats\/\d+\/messages$/.test(url) && !isResponse) {  
            const data = JSON.parse(body);  
            return { type: "outbound_message", text: stripHTML(data.text || "") };  
        }  
        if (/\/messages\/\d+\/like$/.test(url) && !isResponse) {  
            const data = JSON.parse(body);  
            return { type: "engagement_like", ...data };  
        }  
        if (/\/mark-as-read$/.test(url) && !isResponse) {  
            return {  
                type: "engagement_read",  
                chat_id: url.match(/chats\/(\d+)\//)?.[1] || null  
            };  
        }  
        return null;  
    } catch (e) {  
        console.error("[ParseFetch] fail", e, url);  
        return null;  
    }  
}  
  
// ============================================================================  
// WebSocket connection to backend (bi-directional)  
// ============================================================================  
function connectWS() {  
    ws = new WebSocket(WS_URL);  
  
    ws.onopen = () => console.log("[Extension WS] Connected to backend");  
  
    ws.onclose = () => {  
        console.warn("[Extension WS] Disconnected, retrying...");  
        setTimeout(connectWS, 5000);  
    };  
  
    ws.onerror = e => console.error("[Extension WS] Error:", e);  
  
    ws.onmessage = event => {  
        try {  
            const payload = JSON.parse(event.data);  
            console.log("[Extension WS] Received:", payload);  
  
            if (payload.type === "send_command") {  
                handleBackendCommand(payload);  
            }  
        } catch (err) {  
            console.error("[Extension WS] Failed to parse message:", event.data, err);  
        }  
    };  
}  
connectWS();  
  
// ============================================================================  
// Broadcast cache update to backend (reads DB directly)  
// ============================================================================  
async function broadcastCacheUpdate() {  
    try {  
        const db = await openDB();  
  
        const chats = await new Promise((resolve, reject) => {  
            const tx = db.transaction("chats", "readonly");  
            const os = tx.objectStore("chats");  
            const req = os.getAll();  
            req.onsuccess = () => resolve(req.result);  
            req.onerror = () => reject(req.error);  
        });  
  
        const messages = await new Promise((resolve, reject) => {  
            const tx = db.transaction("messages", "readonly");  
            const os = tx.objectStore("messages");  
            const req = os.getAll();  
            req.onsuccess = () => resolve(req.result);  
            req.onerror = () => reject(req.error);  
        });  
  
        const payload = { type: "cache_update", chats, messages };  
  
        if (ws && ws.readyState === WebSocket.OPEN) {  
            ws.send(JSON.stringify(payload));  
            console.log("[Extension WS] Sent cache_update with", chats.length, "chats,", messages.length, "messages");  
        }  
    } catch (err) {  
        console.error("[Extension] Failed to broadcast cache update:", err);  
    }  
}  
  
// ============================================================================  
// Handle backend → extension commands  
// ============================================================================  
function handleBackendCommand(cmd) {  
    if (cmd.action === "send_message") {  
        // Simple POST via background fetch  
        sendMessageToOF(cmd.chat_id, cmd.text);  
    }  
    if (cmd.action === "send_ws_message") {  
        // Forward to page context for WS send  
        forwardToPage({ action: "send_ws_message", data: cmd.data });  
    }  
    if (cmd.action === "send_fetch_command") {  
        // Forward to page context for complex fetch  
        forwardToPage({ action: "send_fetch_command", url: cmd.url, init: cmd.init });  
    }  
}  
  
function forwardToPage(payload) {  
    chrome.tabs.query({ url: "*://onlyfans.com/*" }, tabs => {  
        tabs.forEach(tab => {  
            chrome.tabs.sendMessage(tab.id, {  
                __OF_BACKEND__: true,  
                payload  
            });  
        });  
    });  
}  
  
function sendMessageToOF(chatId, text) {  
    fetch(`https://onlyfans.com/api2/v2/chats/${chatId}/messages`, {  
        method: "POST",  
        headers: { "Content-Type": "application/json" },  
        body: JSON.stringify({ text })  
    })  
    .then(res => res.json())  
    .then(data => {  
        console.log("[Extension] Sent message:", data);  
        // After sending, refresh cache for backend  
        broadcastCacheUpdate();  
    })  
    .catch(err => console.error("[Extension] Failed to send message:", err));  
}  
  
// ============================================================================  
// Passive capture: page → background  
// ============================================================================  
chrome.runtime.onMessage.addListener(async (msg, sender) => {  
    if (!msg?.__OF_FORWARDER__) return;  
    const payload = msg.payload;  
    let parsed = null;  
  
    if (payload.type === "ws_message") {  
        parsed = parseWS(payload.data);  
    } else if (payload.type === "fetch_response") {  
        parsed = parseFetch(payload.url, payload.body, true);  
    } else if (payload.type === "fetch_request") {  
        parsed = parseFetch(payload.url, payload.body, false);  
    }  
  
    if (!parsed) return;  
    console.log("[Extension] Parsed:", parsed.type, parsed);  
  
    try {  
        if (parsed.type === "inbound_message") {  
            await dbPut("messages", normalizeMessage(parsed));  
        } else if (parsed.type === "history") {  
            await dbBulkPut("messages", parsed.messages.map(normalizeMessage));  
        } else if (parsed.type === "chat_list") {  
            await dbBulkPut("chats", parsed.chats.map(normalizeChat));  
        } else if (parsed.type === "outbound_message") {  
            await dbPut("messages", normalizeMessage(parsed));  
        }  
        // Trigger real-time WS broadcast  
        broadcastCacheUpdate();  
    } catch (err) {  
        console.error("[DB] Write failed:", err);  
    }  
});  
  
// ============================================================================  
// External Messaging (UI / Backend bridge)  
// ============================================================================  
chrome.runtime.onMessageExternal.addListener((request, sender, sendResponse) => {  
    console.log("[Extension] External message received:", request);  
    if (request.type === "ping") {  
        sendResponse({ success: true });  
        return true;  
    }  
    if (request.type === "get_all_chats_from_db") {  
        openDB().then(db => {  
            const tx = db.transaction("chats", "readonly");  
            const os = tx.objectStore("chats");  
            const req = os.getAll();  
            req.onsuccess = () => sendResponse({ success: true, data: req.result });  
            req.onerror = () => sendResponse({ success: false, error: req.error });  
        });  
        return true;  
    }  
    if (request.type === "get_all_messages_from_db") {  
        openDB().then(db => {  
            const tx = db.transaction("messages", "readonly");  
            const os = tx.objectStore("messages");  
            const req = os.getAll();  
            req.onsuccess = () => sendResponse({ success: true, data: req.result });  
            req.onerror = () => sendResponse({ success: false, error: req.error });  
        });  
        return true;  
    }  
    sendResponse({ success: false, error: "Unknown external message type" });  
    return false;  
});  