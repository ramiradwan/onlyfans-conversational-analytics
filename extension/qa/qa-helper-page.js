/* QA Helper — MAIN World dev harness  
   Paste into DevTools console of an active OnlyFans tab during development  
*/  
  
console.log("%c[QA Helper:PAGE] Loaded", "color: lime; font-weight: bold;");  
  
const FORWARD_TYPE = "_OF_FORWARDER_";  
const BACKEND_TYPE = "_OF_BACKEND_";  
  
function simulateFetchResponse() {  
  const fakeChats = [  
    { id: "c1", title: "Fan One", createdAt: new Date().toISOString() },  
    { id: "c2", title: "Fan Two", createdAt: new Date().toISOString() }  
  ];  
  console.log("[QA Helper:PAGE] Simulating fetch_response for chats");  
  window.postMessage({  
    type: FORWARD_TYPE,  
    payload: {  
      event: "fetch_response",  
      url: "https://onlyfans.com/api2/v2/chats",  
      body: JSON.stringify({ chats: fakeChats })  
    }  
  }, "*");  
}  
  
function simulateWsMessage() {  
  console.log("[QA Helper:PAGE] Simulating ws_message with online users");  
  window.postMessage({  
    type: FORWARD_TYPE,  
    payload: {  
      event: "ws_message",  
      url: "wss://ws2.onlyfans.com/ws3/",  
      data: { online: [101, 102, 103] }  
    }  
  }, "*");  
}  
  
function simulateBackendCommand() {  
  console.log("[QA Helper:PAGE] Simulating backend command execution");  
  window.postMessage({  
    type: BACKEND_TYPE,  
    payload: {  
      action: "send_ws_message",  
      data: { chat_id: "c1", text: "Hello from backend QA" }  
    }  
  }, "*");  
}  
  
function simulateNewMessage() {  
  console.log("[QA Helper:PAGE] Simulating new_raw_message");  
  window.postMessage({  
    type: FORWARD_TYPE,  
    payload: {  
      event: "new_raw_message",  
      data: { id: "mQA", chat_id: "c1", text: "This is a QA message", createdAt: new Date().toISOString() }  
    }  
  }, "*");  
}  
  
function runAllPageQA() {  
  simulateFetchResponse();  
  simulateWsMessage();  
  simulateNewMessage();  
  simulateBackendCommand();  
  console.log("%c[QA Helper:PAGE] All simulated events posted — check background SW logs", "color: cyan;");  
}  
  
window.qaPageHelper = {  
  simulateFetchResponse,  
  simulateWsMessage,  
  simulateBackendCommand,  
  simulateNewMessage,  
  runAllPageQA  
};  
  
console.log("%c[QA Helper:PAGE] Commands: qaPageHelper.simulateFetchResponse(), qaPageHelper.simulateWsMessage(), qaPageHelper.simulateBackendCommand(), qaPageHelper.simulateNewMessage(), qaPageHelper.runAllPageQA()", "color: yellow;");  