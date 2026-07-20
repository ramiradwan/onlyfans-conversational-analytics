/**
 * STORY ONLY: deterministic synthetic values for tests and visual inspection.
 * This module is never imported by the product runtime.
 */
import type {
  AccountRef,
  AnalyticsDateRange,
  AnalyticsReadModel,
  AnalyticsReadState,
  AnalyticsRefMap,
  AnalyticsWindowDocument,
  AnalyticsWindowSources,
  AnalyzerProvenanceDocument,
  ConversationRef,
  MetricProvenanceDocument,
  SliceProvenanceDocument,
} from '../analytics';

export const STORY_ONLY_NOTICE = 'STORY ONLY · synthetic values · not product data';

export const storyDateRange: AnalyticsDateRange = {
  startDate: '2026-06-01',
  endDate: '2026-06-30',
};

const accountRef = `a1:${'1'.repeat(64)}` as AccountRef;
const conversationRef = `c1:${'2'.repeat(64)}` as ConversationRef;
const projectionDigest = `sha256:${'3'.repeat(64)}`;
const canonicalContentDigest = `sha256:${'4'.repeat(64)}`;
const graphDigest = `sha256:${'5'.repeat(64)}`;
const pipelineConfigDigest = `sha256:${'6'.repeat(64)}`;
const pipelineIdentityDigest = `sha256:${'7'.repeat(64)}`;
const metricConfigDigest = `sha256:${'8'.repeat(64)}`;
const analyzerConfigDigest = `sha256:${'9'.repeat(64)}`;
const pipelineRevision = 'story.analytics.pipeline.v1';

const allTimeWindow: AnalyticsWindowDocument = {
  scope: 'all_time',
  start: '2026-06-02T00:00:00.000Z',
  end: '2026-06-28T00:00:00.000Z',
};
const requestedWindow: AnalyticsWindowDocument = {
  scope: 'requested',
  start: '2026-06-01T00:00:00.000Z',
  end: '2026-06-30T23:59:59.999Z',
};
const effectiveWindow: AnalyticsWindowDocument = {
  scope: 'effective',
  start: '2026-06-02T00:00:00.000Z',
  end: '2026-06-28T00:00:00.000Z',
};

const identity = {
  account_ref: accountRef,
  source_revision: 42,
  projection_generation: 3,
  projection_digest: projectionDigest,
  canonical_content_digest: canonicalContentDigest,
  graph_digest: graphDigest,
  pipeline_revision: pipelineRevision,
  pipeline_config_digest: pipelineConfigDigest,
  pipeline_identity_digest: pipelineIdentityDigest,
};

function slice(window: AnalyticsWindowDocument, sampleCount: number): SliceProvenanceDocument {
  return {
    ...identity,
    requested_window: window,
    effective_window: effectiveWindow,
    sample_count: sampleCount,
    eligible_sample_count: sampleCount,
    sample_coverage: sampleCount ? 1 : null,
    unavailable_reason: sampleCount ? null : 'no_eligible_samples',
  };
}

function metric(mode: 'baseline' | 'model'): MetricProvenanceDocument {
  return {
    metric_name: 'story_metric',
    revision: 'story.metric.v1',
    config_digest: metricConfigDigest,
    mode,
    calibration_status: mode === 'model' ? 'calibrated' : 'not_calibrated',
    sample_count: 48,
    sample_coverage: 1,
    unavailable_reason: null,
  };
}

function analyzer(mode: 'baseline' | 'model'): AnalyzerProvenanceDocument {
  return {
    analyzer_name: 'story_analyzer',
    revision: 'story.analyzer.v1',
    config_digest: analyzerConfigDigest,
    mode,
    calibration_status: mode === 'model' ? 'calibrated' : 'not_calibrated',
    analyzed_sample_count: 48,
    eligible_sample_count: 48,
    sample_coverage: 1,
    mean_confidence: 0.88,
    unavailable_reason: null,
  };
}

export const storyWindowSources: AnalyticsWindowSources = {
  creatorMetrics: { window: allTimeWindow, provenance: slice(allTimeWindow, 48) },
  responseMetrics: { window: requestedWindow, provenance: slice(requestedWindow, 48) },
  sentimentTrend: { window: requestedWindow, provenance: slice(requestedWindow, 48) },
  topics: { window: requestedWindow, provenance: slice(requestedWindow, 48) },
  conversationInsights: { window: allTimeWindow, provenance: slice(allTimeWindow, 14) },
  graph: { window: allTimeWindow, provenance: slice(allTimeWindow, 84) },
};

function analyticsModel(mode: 'baseline' | 'model'): AnalyticsReadModel {
  const metricProvenance = metric(mode);
  return {
    accountRef,
    sourceRevision: 42,
    projectionGeneration: 3,
    projectionDigest,
    canonicalContentDigest,
    graphDigest,
    pipelineRevision,
    pipelineConfigDigest,
    pipelineIdentityDigest,
    analyzerProvenance: [analyzer(mode)],
    metricProvenance: {
      response_time_metrics: metricProvenance,
      conversation_metrics: metricProvenance,
      creator_metrics: metricProvenance,
    },
    windowSources: storyWindowSources,
    topics: [
      { id: 'planning', label: 'Planning', volume: 18, sharePercent: 37.5, trendPercent: 12.5 },
      { id: 'check-ins', label: 'Check-ins', volume: 12, sharePercent: 25, trendPercent: -4 },
      { id: 'boundaries', label: 'Boundaries', volume: 8, sharePercent: 16.7, trendPercent: 0 },
      { id: 'support', label: 'Support', volume: 5, sharePercent: 10.4, trendPercent: null },
      { id: 'scheduling', label: 'Scheduling', volume: 3, sharePercent: 6.3, trendPercent: 2 },
      { id: 'follow-up', label: 'Follow-up', volume: 2, sharePercent: 4.1, trendPercent: -1 },
      { id: 'preferences', label: 'Preferences', volume: 1, sharePercent: 2.1, trendPercent: null },
      { id: 'context', label: 'Context', volume: 1, sharePercent: 2.1, trendPercent: null },
    ],
    sentimentTrend: [
      { at: '2026-06-02T00:00:00.000Z', value: -0.32, sampleCount: 8 },
      { at: '2026-06-08T00:00:00.000Z', value: -0.04, sampleCount: 11 },
      { at: '2026-06-14T00:00:00.000Z', value: 0.18, sampleCount: 9 },
      { at: '2026-06-20T00:00:00.000Z', value: 0.52, sampleCount: 13 },
      { at: '2026-06-28T00:00:00.000Z', value: 0.35, sampleCount: 7 },
    ],
    response: {
      averageHandlingMinutes: 6.4,
      silencePercent: 22.5,
      turns: 31,
      responseCoverage: 0.75,
      responseOpportunityCount: 20,
      respondedCount: 15,
      provenance: metricProvenance,
    },
    creator: {
      conversationCount: 12,
      participantCount: 10,
      messageCount: 48,
      inboundMessageCount: 27,
      outboundMessageCount: 21,
      averageMessagesPerConversation: 4,
      averageResponseSeconds: 384,
      averageSentimentScore: 0.35,
      responseCoverage: 0.75,
      provenance: metricProvenance,
    },
    conversations: [
      {
        conversationRef,
        unreadCount: 2,
        messageCount: 14,
        averageSentimentScore: 0.28,
        averageResponseSeconds: 420,
        responseCoverage: 0.8,
        topicCounts: { Planning: 5, Boundaries: 3, Support: 2 },
        engagementCounts: { inquiry: 4, coordination: 3, acknowledgement: 2 },
        provenance: metricProvenance,
        rangeProvenance: storyWindowSources.conversationInsights.provenance,
      },
    ],
    graph: {
      sourceRevision: 42,
      nodeCount: 84,
      edgeCount: 126,
      nodeCountsByKind: {
        participant: 10,
        conversation: 12,
        message: 48,
        topic: 8,
        affect_state: 3,
        engagement_state: 3,
      },
      edgeCountsByRelation: {
        participates_in: 20,
        contains: 48,
        expresses_affect: 24,
        mentions_topic: 18,
        has_engagement_state: 16,
      },
    },
  };
}

export const storyAnalyticsModel = analyticsModel('baseline');
export const storyModelAnalyticsModel = analyticsModel('model');

export const storyAnalyticsRefs: AnalyticsRefMap = Object.freeze({
  generation: 3,
  sourceRevision: 42,
  resolveConversation(canonicalConversationId: string) {
    return canonicalConversationId === 'story-conversation-one' ? conversationRef : null;
  },
});

export const storyModelState: AnalyticsReadState = {
  status: 'model',
  data: storyModelAnalyticsModel,
  isRefreshing: false,
  message: null,
};
export const storyAvailableState = storyModelState;

export const storyBaselineState: AnalyticsReadState = {
  status: 'baseline',
  data: storyAnalyticsModel,
  isRefreshing: false,
  message: 'Directional baseline — not calibrated production analysis.',
};
export const storyLoadingState: AnalyticsReadState = {
  status: 'loading',
  data: null,
  isRefreshing: false,
  message: 'Loading the requested analytics view…',
};
export const storyUnavailableState: AnalyticsReadState = {
  status: 'unavailable',
  data: null,
  isRefreshing: false,
  message: 'No analytics projection is available for this account.',
};
export const storyBuildingState: AnalyticsReadState = {
  status: 'building',
  data: storyAnalyticsModel,
  isRefreshing: false,
  message: 'The next analytics projection is building.',
  previousStatus: 'baseline',
};
export const storyErrorState: AnalyticsReadState = {
  status: 'error',
  data: null,
  isRefreshing: false,
  message: 'The analytics read failed for this visual state.',
  previousStatus: null,
};

export type StoryAnalyticsStateKey = 'loading' | 'unavailable' | 'building' | 'baseline' | 'model' | 'error';
export const storyAnalyticsStateOptions: readonly { key: StoryAnalyticsStateKey; label: string }[] = [
  { key: 'loading', label: 'Loading' },
  { key: 'unavailable', label: 'Unavailable' },
  { key: 'building', label: 'Building' },
  { key: 'baseline', label: 'Baseline' },
  { key: 'model', label: 'Model' },
  { key: 'error', label: 'Error' },
];

export function storyAnalyticsState(key: StoryAnalyticsStateKey): AnalyticsReadState {
  if (key === 'loading') return storyLoadingState;
  if (key === 'unavailable') return storyUnavailableState;
  if (key === 'building') return storyBuildingState;
  if (key === 'baseline') return storyBaselineState;
  if (key === 'error') return storyErrorState;
  return storyModelState;
}
