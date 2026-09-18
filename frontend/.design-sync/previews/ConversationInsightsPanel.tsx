import { Box, ConversationInsightsPanel } from 'onlyfans-analytics-frontend';

import type {
  AccountRef,
  AnalyticsConversationInsight,
  ConversationRef,
} from '../../src/analytics';
import {
  storyModelState,
  storyWindowSources,
} from '../../src/story-only/analyticsFixtures';

import './card.module.css';

const conversationRef = ('c1:' + '2'.repeat(64)) as ConversationRef;

const mockSlice = {
  account_ref: ('a1:' + '1'.repeat(64)) as AccountRef,
  source_revision: 42,
  projection_generation: 3,
  projection_digest: ('sha256:' + '3'.repeat(64)) as `sha256:${string}`,
  canonical_content_digest: ('sha256:' + '4'.repeat(64)) as `sha256:${string}`,
  graph_digest: ('sha256:' + '5'.repeat(64)) as `sha256:${string}`,
  pipeline_revision: 'preview.pipeline.v1',
  pipeline_config_digest: ('sha256:' + '6'.repeat(64)) as `sha256:${string}`,
  pipeline_identity_digest: ('sha256:' + '7'.repeat(64)) as `sha256:${string}`,
  requested_window: {
    scope: 'all_time' as const,
    start: '2026-06-01T00:00:00.000Z',
    end: '2026-07-18T12:00:00.000Z',
  },
  effective_window: {
    scope: 'effective' as const,
    start: '2026-06-01T00:00:00.000Z',
    end: '2026-07-18T12:00:00.000Z',
  },
  sample_count: 14,
  eligible_sample_count: 14,
  sample_coverage: 1,
  unavailable_reason: null,
};

const mockInsight: AnalyticsConversationInsight = {
  conversationRef,
  unreadCount: 3,
  messageCount: 14,
  averageSentimentScore: 0.38,
  averageResponseSeconds: 420,
  responseCoverage: 0.85,
  topicCounts: {
    Planning: 5,
    Boundaries: 3,
    Support: 2,
    Checkins: 4,
  },
  engagementCounts: {
    inquiry: 5,
    coordination: 4,
    acknowledgement: 3,
  },
  provenance: {
    metric_name: 'conversation_metrics',
    revision: 'v1',
    config_digest: ('sha256:' + '8'.repeat(64)) as `sha256:${string}`,
    mode: 'model',
    calibration_status: 'calibrated',
    sample_count: 14,
    sample_coverage: 1,
    unavailable_reason: null,
  },
  rangeProvenance: mockSlice,
};

export function ActiveConversationInsights() {
  return (
    <Box sx={{ height: 720, maxWidth: 360 }}>
      <ConversationInsightsPanel
        fanName="Bailey Hart"
        insight={mockInsight}
        analyticsState={storyModelState}
        windowSource={storyWindowSources.conversationInsights}
      />
    </Box>
  );
}

export function UnselectedConversation() {
  return (
    <Box sx={{ height: 420, maxWidth: 360 }}>
      <ConversationInsightsPanel
        fanName={null}
        insight={null}
        analyticsState={storyModelState}
      />
    </Box>
  );
}
