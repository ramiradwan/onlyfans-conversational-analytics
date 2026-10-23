/** Story-only findings fixture using synthetic responses and source text. */
import { Box, Typography } from '@mui/material';
import { useMemo } from 'react';

import { findingsAccount, findingsBaselineFixture, findingsResponse, findingsSource } from './findingsFixtures';
import publishedComparison from '../../../shared/analytics/findings-comparison.v1.json';
import publishedEvidence from '../../../shared/analytics/findings-evidence.v1.json';
import publishedResponse from '../../../shared/analytics/findings-response.v1.json';
import type { AccountRef } from '../analytics/analyticsContract';
import type { FindingsBaseline } from '../analytics/analyticsReadModel';
import { parseFindings } from '../analytics/findingsContract';
import { parseSourceEvidence } from '../analytics/questionContract';
import { TaskFindings } from '../components/analytics/TaskFindings';
import { createFindingsStore } from '../store/findingsStore';
import { createBridgeTransportStore } from '../store/transportStore';

export function FindingsFixture({ kind = 'synthetic' }: { kind?: 'synthetic' | 'published' | 'comparison' }) {
  const wire = kind === 'comparison' ? publishedComparison : publishedResponse;
  const baseline: FindingsBaseline = useMemo(() => kind === 'synthetic' ? findingsBaselineFixture : {
    account_ref: wire.account_ref as AccountRef, source_revision: wire.source_revision,
    projection_generation: wire.projection_generation, projection_digest: wire.projection_digest,
    canonical_content_digest: wire.canonical_content_digest, graph_digest: wire.graph_digest,
    pipeline_revision: wire.pipeline_revision, pipeline_config_digest: wire.pipeline_config_digest,
    pipeline_identity_digest: wire.pipeline_identity_digest,
    requested_window: { scope: 'requested', start: wire.requested_window.start, end: wire.requested_window.end },
  }, [kind, wire]);
  const controller = useMemo(() => createFindingsStore({
    read: async (request) => parseFindings(kind === 'synthetic' ? findingsResponse(request) : wire, request),
    evidence: async (reference) => kind === 'synthetic' ? findingsSource(reference) : parseSourceEvidence(publishedEvidence.resolved_evidence, reference),
  }), [kind, wire]);
  const transport = useMemo(() => {
    const state = { ...createBridgeTransportStore().getState(), creatorAccountId: kind === 'synthetic' ? findingsAccount : publishedEvidence.account_id,
      connection: 'connected' as const, readModelState: 'realtime' as const,
      projection: { status: 'current' as const, canonical_revision: baseline.source_revision, projected_revision: baseline.source_revision, projected_at: '2026-09-19T12:00:00Z', reason: null } };
    return { getState: () => state, subscribe: () => () => undefined };
  }, [baseline.source_revision, kind]);
  return <Box sx={{ p: 3, maxWidth: '72rem', mx: 'auto' }}>
    <Typography component="h1" variant="h4">Synthetic text analysis fixture</Typography>
    <Typography>Invented values for visual inspection. No network requests. {kind !== 'synthetic' && 'Recorded backend response; only its recorded filters and one resolved source are available.'}</Typography>
    <TaskFindings baseline={baseline} controller={controller} transport={transport} />
  </Box>;
}
