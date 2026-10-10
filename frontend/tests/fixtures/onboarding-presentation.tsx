// Browser-test entry only; the production build has a separate HTML entry.
import { CssBaseline } from '@mui/material';
import { ThemeProvider } from '@mui/material/styles';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';

import { OnboardingContinuation } from '../../src/components/OnboardingContinuation';
import { AppRouter } from '../../src/routing/AppRouter';
import { startOnboardingSession } from '../../src/services/onboardingSession';
import { bridgeTransportStore } from '../../src/store/transportStore';
import { useUserStore } from '../../src/store/userStore';
import { theme } from '../../src/theme';
import { WebAuthnAccessView } from '../../src/views/WebAuthnAccessView';

const surface = new URLSearchParams(location.search).get('surface');
useUserStore.getState().actions.setUserRole('creator-ceo');
bridgeTransportStore.bindAccount('presentation-test-creator');
bridgeTransportStore.setConnection('connected');
if (surface === 'complete') {
  const now = new Date().toISOString();
  const metric = { value: 0, basis: 'complete' as const, observed_range: { start: now, end: now },
    complete_range: { start: now, end: now }, sample_size: 0, as_of: now, projection_revision: 0 };
  bridgeTransportStore.setAgent({ creator_account_id: 'presentation-test-creator', status: 'connected',
    agent_installation_id: '20000000-0000-4000-8000-000000000001', connection_id: '10000000-0000-4000-8000-000000000001',
    required_config_revision: 'fixture', applied_config_revision: 'fixture', required_history_settings_revision: 1,
    applied_history_settings_revision: 1, last_heartbeat_at: now, degraded_reason: null, browser: null });
  bridgeTransportStore.applySnapshot({ creator_account_id: 'presentation-test-creator', view_revision: 1,
    generated_at: now, conversations: [], analytics: { total_conversations: metric, total_messages: metric,
      inbound_messages: metric, outbound_messages: metric },
    coverage: { status: 'complete', phase: 'complete', generation_id: '90000000-0000-4000-8000-000000000001',
      as_of: now, discovered_conversations: 0, complete_conversations: 0, complete_as_of: now, reason: null },
    projection: { status: 'current', canonical_revision: 0, projected_revision: 0, projected_at: now, reason: null },
    live_freshness: { status: 'current', last_observed_at: now, last_committed_at: now,
      expires_at: new Date(Date.now() + 60000).toISOString(), pending_count: 0, reason: null },
  });
}
const config = document.createElement('script');
config.id = 'fastapi-config'; config.type = 'application/json';
config.textContent = JSON.stringify({ CREATOR_ID: 'presentation-test-creator' });
document.body.append(config);
if (surface !== 'passkey') {
  const session = startOnboardingSession({ journeyId: '11111111-1111-4111-8111-111111111111',
    extensionId: 'presentation-fixture', resolveRuntime: () => undefined });
  addEventListener('pagehide', session.stop, { once: true });
}
createRoot(document.getElementById('root')!).render(<ThemeProvider theme={theme}><CssBaseline />
  {surface === 'passkey' ? <WebAuthnAccessView onAuthenticated={() => { document.body.dataset.authenticated = 'true'; }} />
    : <MemoryRouter><OnboardingContinuation><AppRouter /></OnboardingContinuation></MemoryRouter>}
</ThemeProvider>);
