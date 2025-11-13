/* QA Helper — Service Worker (background.js) dev harness  
   Run in SW console via `chrome://extensions` during development  
*/  
  
console.log("%c[QA Helper:SW] Loaded", "color: lime; font-weight: bold;");  
  
async function getActiveOFTab() {  
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });  
  if (!tabs.length) {  
    console.warn("[QA Helper:SW] No active OnlyFans tab found");  
    return null;  
  }  
  console.log("[QA Helper:SW] Found OF tab:", tabs[0].id);  
  return tabs[0];  
}  
  
async function testUpstreamForwarder() {  
  const tab = await getActiveOFTab();  
  if (!tab) return;  
  await chrome.scripting.executeScript({  
    target: { tabId: tab.id },  
    world: "MAIN",  
    func: () => {  
      console.log("[QA Helper:MAIN] Posting _OF_FORWARDER_ test payload");  
      window.postMessage({  
        type: "_OF_FORWARDER_",  
        payload: { event: "qa_test", data: "hello from QA helper" }  
      }, "*");  
    }  
  });  
}  
  
async function testDownstreamBackendCommand() {  
  const tab = await getActiveOFTab();  
  if (!tab) return;  
  chrome.tabs.sendMessage(tab.id, {  
    type: "_OF_BACKEND_",  
    payload: {  
      action: "send_ws_message",  
      data: { chat_id: "12345", text: "Hello from QA helper" }  
    }  
  }, () => {  
    if (chrome.runtime.lastError) {  
      console.error("[QA Helper:SW] Error sending _OF_BACKEND_:", chrome.runtime.lastError);  
    } else {  
      console.log("[QA Helper:SW] Sent _OF_BACKEND_ payload to tab", tab.id);  
    }  
  });  
}  
  
function checkKeepaliveStatus() {  
  if (typeof keepAliveIntervalId !== "undefined" && keepAliveIntervalId) {  
    console.log("[QA Helper:SW] Keepalive active:", keepAliveIntervalId);  
    console.log("[QA Helper:SW] Next keepalive within 20s");  
  } else {  
    console.warn("[QA Helper:SW] No keepalive detected — check WS connection");  
  }  
}  
  
async function runAllQA() {  
  console.log("%c[QA Helper:SW] Running all QA tests...", "color: cyan; font-weight: bold;");  
  await testUpstreamForwarder();  
  await testDownstreamBackendCommand();  
  checkKeepaliveStatus();  
  console.log("%c[QA Helper:SW] QA complete — check page & SW consoles", "color: cyan;");  
}  
  
globalThis.qaHelper = {  
  testUpstreamForwarder,  
  testDownstreamBackendCommand,  
  checkKeepaliveStatus,  
  runAllQA  
};  
  
console.log("%c[QA Helper:SW] Commands: qaHelper.testUpstreamForwarder(), qaHelper.testDownstreamBackendCommand(), qaHelper.checkKeepaliveStatus(), qaHelper.runAllQA()", "color: yellow;");  