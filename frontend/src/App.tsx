// src/App.tsx

import { CssBaseline, GlobalStyles } from '@mui/material';
import InitColorSchemeScript from '@mui/material/InitColorSchemeScript';
import { ThemeProvider } from '@mui/material/styles';
import React, { useEffect } from 'react';
import { BrowserRouter } from 'react-router-dom';

import { getConfig } from '@/config/fastapiConfig';
import { websocketService } from '@services/websocketService';

import { AppRouter } from './routing/AppRouter';
import { theme } from './theme';

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
  useEffect(() => {
    const { FASTAPI_WS_URL, CREATOR_ID } = getConfig();
    const creatorAccountId = CREATOR_ID ?? 'dev-creator-account';

    if (!FASTAPI_WS_URL) {
      console.error('[App] Missing FASTAPI_WS_URL in injected config');
      return;
    }
    websocketService.connect(FASTAPI_WS_URL, creatorAccountId);

    return () => {
      websocketService.disconnect();
    };
  }, []);

  return (
    <>
      {/* Prevent SSR flicker — must match theme.cssVariables.colorSchemeSelector */}
      <InitColorSchemeScript attribute="data-mui-color-scheme" defaultMode="dark" />

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
