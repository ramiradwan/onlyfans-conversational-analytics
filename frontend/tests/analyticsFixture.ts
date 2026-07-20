import type { AnalyticsUpdateDocument, ConversationRef } from '../src/analytics';

const hex = (value: string) => value.repeat(64);

export const ACCOUNT_REF = `a1:${hex('1')}` as const;
export const CONVERSATION_REF = `c1:${hex('2')}` as ConversationRef;
export const PARTICIPANT_REF = `p1:${hex('3')}` as const;
export const MESSAGE_REF = `m1:${hex('4')}` as const;
export const DIGEST = `sha256:${hex('a')}` as const;

const identity = {
  account_ref: ACCOUNT_REF,
  source_revision: 42,
  projection_generation: 3,
  projection_digest: DIGEST,
  canonical_content_digest: DIGEST,
  graph_digest: DIGEST,
  pipeline_revision: 'pipeline.v1',
  pipeline_config_digest: DIGEST,
  pipeline_identity_digest: DIGEST,
} as const;

const requestedWindow = {
  scope: 'requested' as const,
  start: '2026-06-01T00:00:00.000Z',
  end: '2026-06-30T23:59:59.999Z',
};

const effectiveWindow = {
  scope: 'effective' as const,
  start: '2026-06-02T00:00:00.000Z',
  end: '2026-06-28T00:00:00.000Z',
};

const analyzerProvenance = {
  analyzer_name: 'analyzer',
  revision: 'analyzer.v1',
  config_digest: DIGEST,
  mode: 'model' as const,
  calibration_status: 'calibrated' as const,
  analyzed_sample_count: 1,
  eligible_sample_count: 1,
  sample_coverage: 1,
  mean_confidence: 0.9,
  unavailable_reason: null,
};

const metricProvenance = {
  metric_name: 'metric',
  revision: 'metric.v1',
  config_digest: DIGEST,
  mode: 'model' as const,
  calibration_status: 'calibrated' as const,
  sample_count: 1,
  sample_coverage: 1,
  unavailable_reason: null,
};

const sliceProvenance = {
  ...identity,
  requested_window: requestedWindow,
  effective_window: effectiveWindow,
  sample_count: 1,
  eligible_sample_count: 1,
  sample_coverage: 1,
  unavailable_reason: null,
};

const conversationMetric = {
  account_ref: ACCOUNT_REF,
  conversation_ref: CONVERSATION_REF,
  participant_ref: PARTICIPANT_REF,
  unread_count: 0,
  started_at: effectiveWindow.start,
  ended_at: effectiveWindow.end,
  duration_seconds: 60,
  message_count: 1,
  inbound_message_count: 1,
  outbound_message_count: 0,
  turn_count: 1,
  response_opportunity_count: 0,
  responded_count: 0,
  response_coverage: null,
  average_response_seconds: null,
  median_response_seconds: null,
  maximum_silence_seconds: null,
  average_sentiment_score: 0.2,
  sentiment_counts: { positive: 1 },
  topic_counts: { support: 1 },
  entity_counts: {},
  engagement_counts: { inquiry: 1 },
  provenance: metricProvenance,
  window: effectiveWindow,
  unavailable_reasons: {},
};

const creatorMetric = {
  account_ref: ACCOUNT_REF,
  conversation_count: 1,
  participant_count: 1,
  message_count: 1,
  inbound_message_count: 1,
  outbound_message_count: 0,
  active_from: effectiveWindow.start,
  active_until: effectiveWindow.end,
  average_messages_per_conversation: 1,
  response_opportunity_count: 0,
  responded_count: 0,
  response_coverage: null,
  average_response_seconds: null,
  average_sentiment_score: 0.2,
  sentiment_counts: { positive: 1 },
  topic_counts: { support: 1 },
  entity_counts: {},
  engagement_counts: { inquiry: 1 },
  provenance: metricProvenance,
  window: effectiveWindow,
  unavailable_reasons: {},
};

const graph = {
  account_ref: ACCOUNT_REF,
  source_revision: 42,
  node_count: 4,
  edge_count: 3,
  node_counts_by_kind: { conversation: 1, participant: 1, message: 1, topic: 1 },
  edge_counts_by_relation: { contains: 1, participates_in: 1, mentions_topic: 1 },
};

const update: AnalyticsUpdateDocument = {
  ...identity,
  availability: 'available',
  topics: [{ topic: 'Support', volume: 1, percentage_of_total: 100, trend: null, trend_unavailable_reason: 'no_prior_period' }],
  sentiment_trend: {
    ...identity,
    availability: 'available',
    trend: [{ date: effectiveWindow.start, sentiment_score: 0.2, message_count: 1 }],
    window: requestedWindow,
    provenance: analyzerProvenance,
    range_provenance: sliceProvenance,
  },
  response_time_metrics: {
    ...identity,
    availability: 'available',
    average_handling_time_minutes: null,
    silence_percentage: null,
    turns: 1,
    response_coverage: null,
    response_opportunity_count: 0,
    responded_count: 0,
    window: requestedWindow,
    provenance: metricProvenance,
    range_provenance: sliceProvenance,
    unavailable_reasons: {},
  },
  priorityScores: { [CONVERSATION_REF]: 0.4 },
  unreadCounts: { [CONVERSATION_REF]: 0 },
  requested_window: requestedWindow,
  slice_windows: {
    topics: requestedWindow,
    sentiment_trend: requestedWindow,
    response_time_metrics: requestedWindow,
    conversation_metrics: requestedWindow,
    creator_metrics: requestedWindow,
    graph: requestedWindow,
  },
  slice_provenance: {
    topics: sliceProvenance,
    sentiment_trend: sliceProvenance,
    response_time_metrics: sliceProvenance,
    conversation_metrics: sliceProvenance,
    creator_metrics: sliceProvenance,
    graph: sliceProvenance,
  },
  analyzer_provenance: [analyzerProvenance],
  metric_provenance: {
    response_time_metrics: metricProvenance,
    conversation_metrics: metricProvenance,
    creator_metrics: metricProvenance,
  },
  conversation_metrics: [conversationMetric],
  creator_metrics: creatorMetric,
  message_enrichments: [{
    account_ref: ACCOUNT_REF,
    conversation_ref: CONVERSATION_REF,
    participant_ref: PARTICIPANT_REF,
    message_ref: MESSAGE_REF,
    source_ordinal: 0,
    sent_at: effectiveWindow.start,
    direction: 'inbound',
    sentiment: {
      label: 'positive',
      score: 0.2,
      confidence: 0.9,
      evidence_count: 1,
      analyzer_name: 'sentiment',
      analyzer_revision: 'sentiment.v1',
      analyzer_config_digest: DIGEST,
      analysis_mode: 'model',
      calibration_status: 'calibrated',
    },
    topic_entities: {
      topics: [],
      entities: [],
      analyzer_name: 'topics',
      analyzer_revision: 'topics.v1',
      analyzer_config_digest: DIGEST,
      analysis_mode: 'model',
      calibration_status: 'calibrated',
    },
    engagement: {
      state: 'inquiry',
      confidence: 0.9,
      signal_count: 1,
      analyzer_name: 'engagement',
      analyzer_revision: 'engagement.v1',
      analyzer_config_digest: DIGEST,
      analysis_mode: 'model',
      calibration_status: 'calibrated',
    },
  }],
  graph,
};

export function analyticsUpdateFixture(): AnalyticsUpdateDocument {
  return structuredClone(update);
}
