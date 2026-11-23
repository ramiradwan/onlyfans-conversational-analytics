import { CssBaseline, GlobalStyles } from '@mui/material';
import { ThemeProvider } from '@mui/material/styles';
import { useEffect, useState } from 'react';
import { BrowserRouter } from 'react-router-dom';

import { getConfig } from '@/config/fastapiConfig';
import { websocketService } from '@services/websocketService';
import { analyticsStoreActions } from '@store/analyticsStore';
import { bridgeTransportStore } from '@store/transportStore';
import { useUserStore } from '@store/userStore';

import type { ProtocolErrorPayload } from './protocol';
import { AppRouter } from './routing/AppRouter';
import { authorityRecoveryApi } from './services/desktopOnboarding';
import { colorSchemeProps, theme } from './theme';
import { protocolErrorText } from './utils/statusCopy';
import { WebAuthnAccessView } from './views/WebAuthnAccessView';

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
  const config = getConfig();
  const [endedSession, setEndedSession] = useState<ProtocolErrorPayload | null>(null);
  const userRole = config.BRIDGE_ROLE === 'creator'
    ? 'creator-ceo'
    : config.BRIDGE_ROLE === 'operator'
      ? 'operator'
      : null;
  const hasSessionIdentity = Boolean(
    config.CREATOR_ID && config.BRIDGE_AUTH_TICKET && userRole !== null,
  );
  useEffect(() => {
    const {
      BRIDGE_AUTH_TICKET,
      CREATOR_ID,
      FASTAPI_WS_URL,
    } = config;
    useUserStore.getState().actions.setUserRole(userRole);
    if (!FASTAPI_WS_URL || !CREATOR_ID || !BRIDGE_AUTH_TICKET || userRole === null) {
      console.error('[App] Missing required Brain URL, account, role, or Bridge binding');
      return;
    }
    const creatorAccountId = CREATOR_ID;
    let active = true;
    const unsubscribe = bridgeTransportStore.subscribe(() => {
      const error = bridgeTransportStore.getState().protocolError;
      if (error?.fatal && error.code === 'unauthorized') {
        stopSession();
        setEndedSession(error);
      }
    });
    function stopSession() {
      if (!active) return;
      active = false;
      unsubscribe();
      websocketService.disconnect();
      analyticsStoreActions.deactivate();
      useUserStore.getState().actions.setUserRole(null);
    }
    websocketService.connect(FASTAPI_WS_URL, creatorAccountId, BRIDGE_AUTH_TICKET);
    if (active) void analyticsStoreActions.activate();

    return stopSession;
  }, []);

  let nativePresentation = false;
  try { nativePresentation = Boolean(sessionStorage.getItem('desktop-presentation')); } catch { /* No native presentation. */ }

  return (
    <ThemeProvider theme={theme} {...colorSchemeProps} disableTransitionOnChange>
      <CssBaseline />
      {globalStyles}
      {hasSessionIdentity && !endedSession ? (
        <BrowserRouter>
          <AppRouter />
        </BrowserRouter>
      ) : <WebAuthnAccessView recoveryApi={nativePresentation ? authorityRecoveryApi : undefined}
        sessionNotice={endedSession ? protocolErrorText(endedSession) : undefined} />}
    </ThemeProvider>
  );
}
