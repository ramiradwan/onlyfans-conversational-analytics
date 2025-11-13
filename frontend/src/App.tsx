import React from "react";  
import { ThemeProvider, CssBaseline } from "@mui/material";  
import { theme } from "./theme";  
import { useSocket } from "./hooks/useSocket";  
import { GlobalLoadingSpinner } from "./components/GlobalLoadingSpinner";  
import { ErrorSnackbar } from "./components/ErrorSnackbar";  
import { usePermissions } from "./hooks/usePermissions";  
import AppLayout from "./AppLayout";  
import { getConfig } from "./utils";  
import { useChatStore } from "./store/useChatStore";  
  
export default function App() {  
  const config = getConfig();  
  const { FASTAPI_WS_URL, API_BASE_URL, EXTENSION_ID, CREATOR_ID, USER_ID } = config;  
  
  // Connect to Brain WS  
  useSocket(FASTAPI_WS_URL);  
  
  const permissions = usePermissions();  
  const [showDebug, setShowDebug] = React.useState(true);  
  const [mobileChatsOpen, setMobileChatsOpen] = React.useState(false);  
  
  const replaceStateFromSnapshot = useChatStore((s) => s.replaceStateFromSnapshot);  
  
  React.useEffect(() => {  
    const userId = USER_ID || FASTAPI_WS_URL.split("/").pop();  
  
    // 1️⃣ REST bootstrap (fallback snapshot from backend)  
    const bootstrapUrl = CREATOR_ID  
      ? `${API_BASE_URL}/api/v1/frontend/bootstrap/${userId}?creator_id=${CREATOR_ID}`  
      : `${API_BASE_URL}/api/v1/frontend/bootstrap/${userId}`;  
  
    fetch(bootstrapUrl)  
      .then((res) => res.json())  
      .then((data) => {  
        replaceStateFromSnapshot(data);  
      })  
      .catch((err) => {  
        console.error("[Frontend] Failed to bootstrap snapshot:", err);  
      });  
  
    // 2️⃣ Request fresh snapshot from extension Agent via external messaging  
    if (EXTENSION_ID && chrome?.runtime?.sendMessage) {  
      try {  
        chrome.runtime.sendMessage(  
          EXTENSION_ID,  
          { type: "send_cache_update" },  
          (res) => {  
            if (chrome.runtime.lastError) {  
              console.warn("[Frontend] send_cache_update error:", chrome.runtime.lastError);  
              return;  
            }  
            console.log("[Frontend] Requested cache_update from Agent:", res);  
          }  
        );  
      } catch (err) {  
        console.warn("[Frontend] Could not contact extension Agent:", err);  
      }  
    }  
  }, [FASTAPI_WS_URL, API_BASE_URL, EXTENSION_ID, CREATOR_ID, USER_ID, replaceStateFromSnapshot]);  
  
  return (  
    <ThemeProvider theme={theme}>  
      <CssBaseline />  
      <GlobalLoadingSpinner />  
      <ErrorSnackbar />  
      <AppLayout  
        permissions={permissions}  
        showDebug={showDebug}  
        toggleDebug={() => setShowDebug((p) => !p)}  
        mobileChatsOpen={mobileChatsOpen}  
        openMobileChats={() => setMobileChatsOpen(true)}  
        closeMobileChats={() => setMobileChatsOpen(false)}  
      />  
    </ThemeProvider>  
  );  
}  