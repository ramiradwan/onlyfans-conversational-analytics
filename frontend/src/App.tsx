// src/App.tsx  
import React, { useEffect } from 'react';  
import InitColorSchemeScript from '@mui/material/InitColorSchemeScript';  
import { ThemeProvider } from '@mui/material/styles';  
import { CssBaseline, GlobalStyles } from '@mui/material';  
import { BrowserRouter } from 'react-router-dom';  
  
import { AppRouter } from './routing/AppRouter';  
import { theme } from './theme';  
import { websocketService } from '@services/websocketService';  
import { getConfig } from '@/config/fastapiConfig';  
import { useUserRole } from '@store/userStore';  
import { useChatStore } from '@store/chatStore';  
import { extensionService } from '@services/extensionService';  
import { systemStoreActions } from '@store/systemStore';  
  
// Global styles using theme.vars for flicker-free dark/light mode  
const globalStyles = (  
  <GlobalStyles  
    styles={(theme) => ({  
      'html, body, #root': {  
        height: '100%',  
        margin: 0,  
        padding: 0,  
        backgroundColor: theme.vars.palette.background.default,  
        color: theme.vars.palette.text.primary,  
      },  
      body: {  
        fontFamily: theme.typography.fontFamily,  
      },  
    })}  
  />  
);  
  
export function App() {  
  const replaceStateFromSnapshot = useChatStore((s) => s.actions.replaceStateFromSnapshot);  
  const role = useUserRole();  
  
  useEffect(() => {  
    const { FASTAPI_WS_URL, API_BASE_URL, CREATOR_ID, USER_ID } = getConfig();  
    const userId = USER_ID || FASTAPI_WS_URL?.split('/').pop();  
  
    if (!FASTAPI_WS_URL || !userId) {  
      console.error('[App] Missing FASTAPI_WS_URL or userId in injected config');  
      return;  
    }  
  
    // Set extension connection state immediately  
    if (extensionService.isAgentAvailable()) {  
      systemStoreActions.setExtensionConnectionState('connected');  
    } else {  
      systemStoreActions.setExtensionConnectionState('disconnected');  
    }  
  
    console.log(`[App] Initializing WebSocket for user: ${userId}`);  
    websocketService.connect(FASTAPI_WS_URL, userId);  
  
    const isDevMode = import.meta.env.DEV;  
    const hasAgent = extensionService.isAgentAvailable();  
  
    // DEV MODE FALLBACK: bootstrap state via REST when Agent is missing  
    if (isDevMode && !hasAgent) {  
      const bootstrapUrl = CREATOR_ID  
        ? `${API_BASE_URL}/api/v1/frontend/bootstrap/${userId}?creator_id=${CREATOR_ID}`  
        : `${API_BASE_URL}/api/v1/frontend/bootstrap/${userId}`;  
  
      fetch(bootstrapUrl)  
        .then((res) => res.json())  
        .then((data) => {  
          console.log('[App] Bootstrap snapshot loaded (dev fallback):', data);  
          replaceStateFromSnapshot(data);  
        })  
        .catch((err) => {  
          console.error('[App] Failed to bootstrap snapshot:', err);  
        });  
    }  
  
    return () => {  
      websocketService.disconnect();  
    };  
  }, []);  
  
  return (  
    <>  
      {/* Prevent SSR flicker — must match theme.cssVariables.colorSchemeSelector */}  
      <InitColorSchemeScript attribute="class" defaultMode="dark" />  
  
      <ThemeProvider theme={theme} defaultMode="dark" disableTransitionOnChange>  
        <CssBaseline />  
        {globalStyles}  
        <BrowserRouter>  
          <AppRouter />  
        </BrowserRouter>  
      </ThemeProvider>  
    </>  
  );  
}  