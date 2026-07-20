import AccountTreeOutlinedIcon from '@mui/icons-material/AccountTreeOutlined';
import { Alert, AlertTitle, Box, Chip, Stack, Typography } from '@mui/material';
import { useSyncExternalStore } from 'react';

import type { AnalyticsReadState, AnalyticsWindowSource } from '../analytics';
import { AnalyticsStateFrame } from '../components/analytics';
import { GraphSummaryPanel, type GraphQueryGate } from '../components/graph';
import { Panel } from '../components/ui';
import { bridgeTransportStore } from '../store/transportStore';

export default function GraphExplorerView() {
  const state = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const projectionCurrent =
    state.projection.status === 'current' &&
    state.projection.projected_revision >= state.projection.canonical_revision;

  return (
    <Box sx={{ maxWidth: 960, mx: 'auto', width: '100%' }}>
      <Stack spacing={3}>
        <Box>
          <Typography component="h1" variant="h4">Graph explorer</Typography>
          <Typography color="text.secondary" variant="body2" sx={{ mt: 0.5 }}>
            Brain-owned labeled-property-graph projection status
          </Typography>
        </Box>

        <Panel>
          <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" spacing={2}>
            <Stack direction="row" spacing={1.5} alignItems="center">
              <AccountTreeOutlinedIcon color={projectionCurrent ? 'success' : 'disabled'} />
              <Box>
                <Typography component="h2" variant="h6">Local graph projection</Typography>
                <Typography color="text.secondary" variant="body2">
                  Canonical revision {state.projection.canonical_revision}; projected revision{' '}
                  {state.projection.projected_revision}
                </Typography>
              </Box>
            </Stack>
            <Chip
              label={state.projection.status}
              color={projectionCurrent ? 'success' : 'warning'}
              variant="outlined"
            />
          </Stack>

          {projectionCurrent ? (
            <Alert severity="info">
              <AlertTitle>Interactive graph queries are not enabled in this Beta</AlertTitle>
              The local projection is ready, but Brain does not expose an authenticated graph-query
              API yet. No generated queries or sample results are shown.
            </Alert>
          ) : (
            <Alert severity={state.projection.status === 'unavailable' ? 'error' : 'warning'}>
              <AlertTitle>Graph data is not ready</AlertTitle>
              {state.projection.reason ??
                'Brain is still building the local graph projection from canonical messages.'}
            </Alert>
          )}
        </Panel>
      </Stack>
    </Box>
  );
}

/**
 * Session-bound analytics presentation of the canonical relationship graph summary.
 * Not mounted by the live route: `/graph-explorer` renders the WebSocket-bounded
 * `GraphExplorerView` above. This is used by the story-only visual harness so
 * `GraphSummaryPanel` gets real render and accessibility coverage; wiring it into the
 * live route is a follow-up once the REST analytics store is the route's data source.
 */
export interface GraphExplorerPresentationProps {
  state: AnalyticsReadState;
  windowSource: AnalyticsWindowSource;
  queryGate: GraphQueryGate;
}

export function GraphExplorerPresentation({
  state,
  windowSource,
  queryGate,
}: GraphExplorerPresentationProps) {
  return (
    <Box sx={{ maxWidth: 960, mx: 'auto', width: '100%' }}>
      <Stack spacing={3}>
        <Box>
          <Typography component="h1" variant="h4">Graph explorer</Typography>
          <Typography color="text.secondary" variant="body2" sx={{ mt: 0.5 }}>
            Canonical relationship graph projection
          </Typography>
        </Box>
        <AnalyticsStateFrame state={state}>
          {state.data && (
            <GraphSummaryPanel
              summary={state.data.graph}
              queryGate={queryGate}
              windowSource={windowSource}
            />
          )}
        </AnalyticsStateFrame>
      </Stack>
    </Box>
  );
}
