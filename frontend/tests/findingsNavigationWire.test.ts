import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import wire from '../../shared/analytics/findings-grouped.v4.json';
import { parseGroupedFindings, type FindingsRequest } from '../src/analytics/findingsContract';

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());

it('verifies the backend grouped wire fixture and its Unicode policy digest', async () => {
  const request: FindingsRequest = {
    baseline: {
      account_ref: wire.account_ref, source_revision: wire.source_revision,
      projection_generation: wire.projection_generation, projection_digest: wire.projection_digest,
      canonical_content_digest: wire.canonical_content_digest, graph_digest: wire.graph_digest,
      pipeline_revision: wire.pipeline_revision, pipeline_config_digest: wire.pipeline_config_digest,
      pipeline_identity_digest: wire.pipeline_identity_digest,
      requested_window: { scope: 'requested', start: wire.requested_window.start, end: wire.requested_window.end },
    },
    direction: 'all', conversation_ref: wire.conversation_ref, category_filter: null, view: 'grouped',
    evidence_offset: 0, evidence_limit: 50, conversation_offset: 0, conversation_limit: 50,
  };
  const result = await parseGroupedFindings(wire, request);
  expect(result.navigation.policy?.sha256).toBe('755376dcbaf77d423ff36f534a88ca944f2ea92e707baada84b5cbd53e62afd9');
  expect(result.navigation.current[0]).toMatchObject({
    supporting_messages: 1, partial_supporting_messages: 1, share_of_complete_messages: null,
  });
  expect(result.navigation.previous?.[0]).toMatchObject({
    supporting_messages: 2, complete_supporting_messages: 2, share_of_complete_messages: 1,
  });
});
