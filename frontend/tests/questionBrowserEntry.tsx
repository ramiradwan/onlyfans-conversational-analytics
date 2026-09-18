import { Box, CssBaseline } from '@mui/material';
import { ThemeProvider } from '@mui/material/styles';
import { createRoot } from 'react-dom/client';

import { ConversationQuestions } from '../src/components/analytics/ConversationQuestions';
import { theme } from '../src/theme';
import { context } from './questionFixture';

let state = context();
const listeners = new Set<() => void>();
const transport = {
  getState: () => state,
  subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
};
window.addEventListener('synthetic-question-change', () => {
  state = { ...state, projection: { ...state.projection, canonical_revision: 8 } };
  listeners.forEach((listener) => listener());
});
createRoot(document.getElementById('root')!).render(
  <ThemeProvider theme={theme} defaultMode="light">
    <CssBaseline />
    <Box component="main" sx={{ maxWidth: 1200, mx: 'auto', p: { xs: 1, sm: 3 } }}>
      <ConversationQuestions transport={transport} />
    </Box>
  </ThemeProvider>,
);
