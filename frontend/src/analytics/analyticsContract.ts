import { z } from 'zod';

const forbiddenAnalyticsKeys = new Set([
  'creator_account_id',
  'conversation_id',
  'participant_id',
  'message_id',
  'display_name',
  'text',
  'content',
  'body',
  'raw',
  'raw_content',
  'message_text',
]);
const digest = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const accountRefSchema = z.string().regex(/^a1:[0-9a-f]{64}$/).brand<'AccountRef'>();
const conversationRefSchema = z.string().regex(/^c1:[0-9a-f]{64}$/).brand<'ConversationRef'>();
const participantRefSchema = z.string().regex(/^p1:[0-9a-f]{64}$/).brand<'ParticipantRef'>();
const messageRefSchema = z.string().regex(/^m1:[0-9a-f]{64}$/).brand<'MessageRef'>();
const topicRefSchema = z.string().regex(/^t1:[0-9a-f]{64}$/).brand<'TopicRef'>();
const entityRefSchema = z.string().regex(/^x1:[0-9a-f]{64}$/).brand<'EntityRef'>();

const awareDateTime = z.string().datetime({ offset: true });
const nonNegativeInteger = z.number().int().nonnegative();
const nonNegative = z.number().nonnegative();
const ratio = z.number().min(0).max(1);
const counts = z.record(z.string(), nonNegativeInteger);
const unavailableReasons = z.record(z.string(), z.string());

export type AccountRef = z.infer<typeof accountRefSchema>;
export type ConversationRef = z.infer<typeof conversationRefSchema>;
export type ParticipantRef = z.infer<typeof participantRefSchema>;
export type MessageRef = z.infer<typeof messageRefSchema>;

const analyticsWindowSchema = z
  .object({
    scope: z.enum(['all_time', 'requested', 'effective']),
    start: awareDateTime.nullable().optional().default(null),
    end: awareDateTime.nullable().optional().default(null),
  })
  .strict()
  .superRefine((window, context) => {
    if (window.start !== null && window.end !== null && Date.parse(window.start) > Date.parse(window.end)) {
      context.addIssue({ code: 'custom', message: 'Analytics window start follows its end.' });
    }
    if (window.scope === 'all_time' && window.start === null && window.end !== null) {
      context.addIssue({ code: 'custom', message: 'All-time windows cannot end without a start.' });
    }
  });

const analyzerProvenanceSchema = z
  .object({
    analyzer_name: z.string().min(1),
    revision: z.string().min(1),
    config_digest: digest,
    mode: z.enum(['baseline', 'model']),
    calibration_status: z.enum(['not_calibrated', 'calibrated', 'unavailable']),
    analyzed_sample_count: nonNegativeInteger,
    eligible_sample_count: nonNegativeInteger,
    sample_coverage: ratio.nullable(),
    mean_confidence: ratio.nullable(),
    unavailable_reason: z.string().nullable(),
  })
  .strict();

const metricProvenanceSchema = z
  .object({
    metric_name: z.string().min(1),
    revision: z.string().min(1),
    config_digest: digest,
    mode: z.enum(['baseline', 'model']),
    calibration_status: z.enum(['not_calibrated', 'calibrated', 'unavailable']),
    sample_count: nonNegativeInteger,
    sample_coverage: ratio.nullable(),
    unavailable_reason: z.string().nullable(),
  })
  .strict();

const sliceProvenanceSchema = z
  .object({
    account_ref: accountRefSchema,
    requested_window: analyticsWindowSchema,
    effective_window: analyticsWindowSchema,
    sample_count: nonNegativeInteger,
    eligible_sample_count: nonNegativeInteger,
    sample_coverage: ratio.nullable(),
    unavailable_reason: z.string().nullable(),
    source_revision: nonNegativeInteger,
    projection_generation: z.number().int().positive(),
    projection_digest: digest,
    canonical_content_digest: digest,
    graph_digest: digest,
    pipeline_revision: z.string().min(1),
    pipeline_config_digest: digest,
    pipeline_identity_digest: digest,
  })
  .strict();

const topicMetricSchema = z
  .object({
    topic: z.string().min(1),
    volume: nonNegativeInteger,
    percentage_of_total: z.number().min(0).max(100),
    trend: z.number().nullable(),
    trend_unavailable_reason: z.string().nullable(),
  })
  .strict();

const sentimentTrendPointSchema = z
  .object({
    date: awareDateTime,
    sentiment_score: z.number().min(-1).max(1),
    message_count: nonNegativeInteger,
  })
  .strict();

const identityFields = {
  account_ref: accountRefSchema,
  source_revision: nonNegativeInteger,
  projection_generation: z.number().int().positive(),
  projection_digest: digest,
  canonical_content_digest: digest,
  graph_digest: digest,
  pipeline_revision: z.string().min(1),
  pipeline_config_digest: digest,
  pipeline_identity_digest: digest,
} as const;

const sentimentTrendSchema = z
  .object({
    ...identityFields,
    availability: z.literal('available'),
    trend: z.array(sentimentTrendPointSchema),
    window: analyticsWindowSchema,
    provenance: analyzerProvenanceSchema,
    range_provenance: sliceProvenanceSchema,
  })
  .strict();

const responseTimeSchema = z
  .object({
    ...identityFields,
    availability: z.literal('available'),
    average_handling_time_minutes: nonNegative.nullable(),
    silence_percentage: z.number().min(0).max(100).nullable(),
    turns: nonNegative.nullable(),
    response_coverage: ratio.nullable(),
    response_opportunity_count: nonNegativeInteger,
    responded_count: nonNegativeInteger,
    window: analyticsWindowSchema,
    provenance: metricProvenanceSchema,
    range_provenance: sliceProvenanceSchema,
    unavailable_reasons: unavailableReasons,
  })
  .strict();

const conversationMetricsSchema = z
  .object({
    account_ref: accountRefSchema,
    conversation_ref: conversationRefSchema,
    participant_ref: participantRefSchema,
    unread_count: nonNegativeInteger,
    started_at: awareDateTime.nullable(),
    ended_at: awareDateTime.nullable(),
    duration_seconds: nonNegative,
    message_count: nonNegativeInteger,
    inbound_message_count: nonNegativeInteger,
    outbound_message_count: nonNegativeInteger,
    turn_count: nonNegativeInteger,
    response_opportunity_count: nonNegativeInteger,
    responded_count: nonNegativeInteger,
    response_coverage: ratio.nullable(),
    average_response_seconds: nonNegative.nullable(),
    median_response_seconds: nonNegative.nullable(),
    maximum_silence_seconds: nonNegative.nullable(),
    average_sentiment_score: z.number().min(-1).max(1).nullable(),
    sentiment_counts: counts,
    topic_counts: counts,
    entity_counts: counts,
    engagement_counts: counts,
    provenance: metricProvenanceSchema,
    window: analyticsWindowSchema,
    unavailable_reasons: unavailableReasons,
  })
  .strict();

const creatorMetricsSchema = z
  .object({
    account_ref: accountRefSchema,
    conversation_count: nonNegativeInteger,
    participant_count: nonNegativeInteger,
    message_count: nonNegativeInteger,
    inbound_message_count: nonNegativeInteger,
    outbound_message_count: nonNegativeInteger,
    active_from: awareDateTime.nullable(),
    active_until: awareDateTime.nullable(),
    average_messages_per_conversation: nonNegative.nullable(),
    response_opportunity_count: nonNegativeInteger,
    responded_count: nonNegativeInteger,
    response_coverage: ratio.nullable(),
    average_response_seconds: nonNegative.nullable(),
    average_sentiment_score: z.number().min(-1).max(1).nullable(),
    sentiment_counts: counts,
    topic_counts: counts,
    entity_counts: counts,
    engagement_counts: counts,
    provenance: metricProvenanceSchema,
    window: analyticsWindowSchema,
    unavailable_reasons: unavailableReasons,
  })
  .strict();

const graphSummarySchema = z
  .object({
    account_ref: accountRefSchema,
    source_revision: nonNegativeInteger,
    node_count: nonNegativeInteger,
    edge_count: nonNegativeInteger,
    node_counts_by_kind: counts,
    edge_counts_by_relation: counts,
  })
  .strict();

const sentimentResultSchema = z
  .object({
    label: z.enum(['positive', 'neutral', 'negative']),
    score: z.number().min(-1).max(1),
    confidence: ratio,
    evidence_count: nonNegativeInteger,
    analyzer_name: z.string().min(1),
    analyzer_revision: z.string().min(1),
    analyzer_config_digest: digest,
    analysis_mode: z.enum(['baseline', 'model']),
    calibration_status: z.enum(['not_calibrated', 'calibrated', 'unavailable']),
  })
  .strict();

const topicEntityResultSchema = z
  .object({
    topics: z.array(
      z
        .object({
          topic_ref: topicRefSchema,
          taxonomy_id: z.enum(['feedback', 'greeting', 'media', 'pricing', 'scheduling', 'support']),
          label: z.string().min(1),
          confidence: ratio,
          evidence_count: nonNegativeInteger,
        })
        .strict(),
    ),
    entities: z.array(
      z
        .object({
          entity_ref: entityRefSchema,
          entity_type: z.enum(['amount', 'hashtag', 'mention', 'url']),
          confidence: ratio,
        })
        .strict(),
    ),
    analyzer_name: z.string().min(1),
    analyzer_revision: z.string().min(1),
    analyzer_config_digest: digest,
    analysis_mode: z.enum(['baseline', 'model']),
    calibration_status: z.enum(['not_calibrated', 'calibrated', 'unavailable']),
  })
  .strict();

const engagementResultSchema = z
  .object({
    state: z.enum(['acknowledgement', 'commitment', 'constraint', 'coordination', 'information', 'inquiry', 'minimal', 'transactional']),
    confidence: ratio,
    signal_count: nonNegativeInteger,
    analyzer_name: z.string().min(1),
    analyzer_revision: z.string().min(1),
    analyzer_config_digest: digest,
    analysis_mode: z.enum(['baseline', 'model']),
    calibration_status: z.enum(['not_calibrated', 'calibrated', 'unavailable']),
  })
  .strict();

const messageEnrichmentSchema = z
  .object({
    account_ref: accountRefSchema,
    conversation_ref: conversationRefSchema,
    participant_ref: participantRefSchema,
    message_ref: messageRefSchema,
    source_ordinal: nonNegativeInteger,
    sent_at: awareDateTime,
    direction: z.enum(['inbound', 'outbound']),
    sentiment: sentimentResultSchema,
    topic_entities: topicEntityResultSchema,
    engagement: engagementResultSchema,
  })
  .strict();

const analyticsUpdateSchema = z
  .object({
    ...identityFields,
    availability: z.literal('available'),
    topics: z.array(topicMetricSchema),
    sentiment_trend: sentimentTrendSchema,
    response_time_metrics: responseTimeSchema,
    priorityScores: z.record(conversationRefSchema, z.number()).optional().default({}),
    unreadCounts: z.record(conversationRefSchema, nonNegativeInteger).optional().default({}),
    requested_window: analyticsWindowSchema,
    slice_windows: z.record(z.string(), analyticsWindowSchema),
    slice_provenance: z.record(z.string(), sliceProvenanceSchema),
    analyzer_provenance: z.array(analyzerProvenanceSchema),
    metric_provenance: z.record(z.string(), metricProvenanceSchema),
    conversation_metrics: z.array(conversationMetricsSchema),
    creator_metrics: creatorMetricsSchema,
    message_enrichments: z.array(messageEnrichmentSchema),
    graph: graphSummarySchema,
  })
  .strict();

const generationIdentityKeys = [
  'account_ref',
  'source_revision',
  'projection_generation',
  'projection_digest',
  'canonical_content_digest',
  'graph_digest',
  'pipeline_revision',
  'pipeline_config_digest',
  'pipeline_identity_digest',
] as const;

type GenerationIdentity = Pick<z.infer<typeof analyticsUpdateSchema>, (typeof generationIdentityKeys)[number]>;

function assertNoForbiddenAnalyticsProperties(value: unknown, path: readonly string[] = []): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoForbiddenAnalyticsProperties(item, [...path, String(index)]));
    return;
  }
  if (typeof value !== 'object' || value === null) return;
  for (const [key, child] of Object.entries(value)) {
    const normalizedKey = key
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      .replace(/[-\s]+/g, '_')
      .toLowerCase();
    if (forbiddenAnalyticsKeys.has(normalizedKey)) {
      throw new AnalyticsContractError(`Forbidden analytics property at ${[...path, key].join('.')}.`);
    }
    assertNoForbiddenAnalyticsProperties(child, [...path, key]);
  }
}

function assertIdentity(expected: GenerationIdentity, candidate: GenerationIdentity, label: string): void {
  for (const key of generationIdentityKeys) {
    if (candidate[key] !== expected[key]) {
      throw new AnalyticsContractError(`${label} does not match the active analytics generation.`);
    }
  }
}

function validateUpdateIdentities(update: z.infer<typeof analyticsUpdateSchema>): void {
  assertIdentity(update, update.sentiment_trend, 'Sentiment trend');
  assertIdentity(update, update.response_time_metrics, 'Response metrics');
  assertIdentity(update, update.sentiment_trend.range_provenance, 'Sentiment range');
  assertIdentity(update, update.response_time_metrics.range_provenance, 'Response range');
  for (const [name, provenance] of Object.entries(update.slice_provenance)) {
    assertIdentity(update, provenance, `Slice ${name}`);
  }
  if (update.creator_metrics.account_ref !== update.account_ref || update.graph.account_ref !== update.account_ref) {
    throw new AnalyticsContractError('Analytics account references do not match.');
  }
  if (update.graph.source_revision !== update.source_revision) {
    throw new AnalyticsContractError('Graph summary does not match the active analytics revision.');
  }
  for (const conversation of update.conversation_metrics) {
    if (conversation.account_ref !== update.account_ref) {
      throw new AnalyticsContractError('Conversation analytics belong to another account.');
    }
  }
  for (const message of update.message_enrichments) {
    if (message.account_ref !== update.account_ref) {
      throw new AnalyticsContractError('Message analytics belong to another account.');
    }
  }

  const requiredSlices = ['topics', 'sentiment_trend', 'response_time_metrics', 'conversation_metrics', 'creator_metrics', 'graph'];
  for (const name of requiredSlices) {
    if (!update.slice_windows[name] || !update.slice_provenance[name]) {
      throw new AnalyticsContractError(`Analytics slice ${name} has incomplete window provenance.`);
    }
  }
  const requiredMetrics = ['response_time_metrics', 'conversation_metrics', 'creator_metrics'];
  for (const name of requiredMetrics) {
    if (!update.metric_provenance[name]) {
      throw new AnalyticsContractError(`Analytics metric ${name} has no provenance.`);
    }
  }
}

export class AnalyticsContractError extends Error {
  constructor(message = 'The analytics response did not match the strict canonical contract.') {
    super(message);
    this.name = 'AnalyticsContractError';
  }
}

export type AnalyticsUpdateDocument = z.infer<typeof analyticsUpdateSchema>;
export type AnalyticsWindowDocument = z.infer<typeof analyticsWindowSchema>;
export type SliceProvenanceDocument = z.infer<typeof sliceProvenanceSchema>;
export type AnalyzerProvenanceDocument = z.infer<typeof analyzerProvenanceSchema>;
export type MetricProvenanceDocument = z.infer<typeof metricProvenanceSchema>;

export function parseAnalyticsUpdate(value: unknown): AnalyticsUpdateDocument {
  assertNoForbiddenAnalyticsProperties(value);
  const result = analyticsUpdateSchema.safeParse(value);
  if (!result.success) throw new AnalyticsContractError();
  validateUpdateIdentities(result.data);
  return result.data;
}
