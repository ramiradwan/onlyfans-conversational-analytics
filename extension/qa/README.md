# 📄 QA Usage Guide
  
This folder contains **developer‑only** helper scripts for testing the browser extension without hitting the real OnlyFans API or backend.  
  
## Files  
  
- **qa-helper-sw.js** — Run in the extension **Service Worker console** (`chrome://extensions`) to test:  
  - MV3 injection into MAIN world  
  - Upstream `_OF_FORWARDER_` bridge  
  - Downstream `_OF_BACKEND_` command flow  
  - Keepalive persistence checks  
  
- **qa-helper-page.js** — Run in the **MAIN world console** of an active OnlyFans tab to simulate:  
  - Fetch response capture  
  - WebSocket message capture  
  - Backend command execution  
  - New raw message delta  
  
## Usage  
  
### Service Worker helper  
1. Open `chrome://extensions`.  
2. Click "service worker" under the Agent extension.  
3. Paste `qa-helper-sw.js` into the console.  
4. Run:  
```js  
   qaHelper.runAllQA();  
```  
  
### Page helper
1. Navigate to https://onlyfans.com with the Agent loaded.  
2. Open DevTools (F12) → Console.  
3. Paste qa-helper-page.js into the console.  
4. Run:  
```js  
qaPageHelper.runAllPageQA();  
```  
  
> [!NOTE]  
> - Never ship qa/ to production — it’s for local dev only.  
> - Ensure .gitignore or build scripts exclude qa/ from release zips.  