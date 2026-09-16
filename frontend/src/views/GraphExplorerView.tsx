import { Alert, AlertTitle, Box, Stack, Typography } from '@mui/material';
import { useSyncExternalStore } from 'react';

import type { AnalyticsReadState, AnalyticsWindowSource } from '../analytics';
import { AnalyticsStateFrame } from '../components/analytics';
import { GraphSummaryPanel, type GraphQueryGate } from '../components/graph';
import { bridgeTransportStore } from '../store/transportStore';
import { humanizeProjectionReason } from '../utils/dataReadiness';

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
        <Typography component="h1" variant="h4">Graph explorer</Typography>

        {projectionCurrent ? (
          <Alert severity="info">
            <AlertTitle>Not available yet</AlertTitle>
            Exploring how your fans and conversations connect isn&apos;t available in this version.
          </Alert>
        ) : (
          <Alert severity={state.projection.status === 'unavailable' ? 'error' : 'info'}>
            <AlertTitle>Not ready yet</AlertTitle>
            {humanizeProjectionReason(
              state.projection.reason,
              'Your conversations are still being prepared.',
            )}
          </Alert>
        )}
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
