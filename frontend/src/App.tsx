import { CssBaseline, GlobalStyles } from '@mui/material';
import InitColorSchemeScript from '@mui/material/InitColorSchemeScript';
import { ThemeProvider } from '@mui/material/styles';
import { useEffect } from 'react';
import { BrowserRouter } from 'react-router-dom';

import { getConfig } from '@/config/fastapiConfig';
import { requestAgentPairingTicket } from '@services/agentPairingApi';
import { bindAgentToBrain } from '@services/extensionBinding';
import { websocketService } from '@services/websocketService';
import { analyticsStoreActions } from '@store/analyticsStore';
import { useUserStore } from '@store/userStore';

import { AppRouter } from './routing/AppRouter';
import { theme } from './theme';

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
      '*': {
        boxSizing: 'border-box',
      },
    })}
  />
);

export function App() {
  useEffect(() => {
    const {
      BRIDGE_AUTH_TICKET,
      CREATOR_ID,
      EXTENSION_ID,
      FASTAPI_WS_URL,
      BRIDGE_ROLE,
    } = getConfig();
    const userRole = BRIDGE_ROLE === 'creator'
      ? 'creator-ceo'
      : BRIDGE_ROLE === 'operator'
        ? 'operator'
        : null;
    useUserStore.getState().actions.setUserRole(userRole);
    if (!FASTAPI_WS_URL || !CREATOR_ID || !BRIDGE_AUTH_TICKET || userRole === null) {
      console.error('[App] Missing required Brain URL, account, role, or Bridge binding');
      return;
    }
    const creatorAccountId = CREATOR_ID;
    const controller = new AbortController();
    if (EXTENSION_ID && EXTENSION_ID !== 'dev-extension-id') {
      void requestAgentPairingTicket(controller.signal)
        .then((ticket) => {
          return bindAgentToBrain({
            extensionId: EXTENSION_ID,
            creatorAccountId,
            authTicket: ticket.pairing_ticket,
          });
        })
        .catch(() => {
          // Brain-owned Agent state presents pairing failures; never log credentials.
        });
    }
    websocketService.connect(FASTAPI_WS_URL, creatorAccountId, BRIDGE_AUTH_TICKET);
    void analyticsStoreActions.activate();

    return () => {
      controller.abort();
      websocketService.disconnect();
      analyticsStoreActions.deactivate();
      useUserStore.getState().actions.setUserRole(null);
    };
  }, []);

  return (
    <>
      <InitColorSchemeScript
        attribute="data-mui-color-scheme"
        defaultMode="light"
      />

      <ThemeProvider theme={theme} defaultMode="light" disableTransitionOnChange>
        <CssBaseline />
        {globalStyles}
        <BrowserRouter>
          <AppRouter />
        </BrowserRouter>
      </ThemeProvider>
    </>
  );
}
