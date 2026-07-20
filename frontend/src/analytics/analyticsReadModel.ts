import type {
  AccountRef,
  AnalyzerProvenanceDocument,
  AnalyticsUpdateDocument,
  ConversationRef,
  MetricProvenanceDocument,
  SliceProvenanceDocument,
} from './analyticsContract';
import type { AnalyticsRefMap } from './analyticsRefs';
import type { AnalyticsWindowSources } from './analyticsWindow';

export interface AnalyticsDateRange {
  startDate: string;
  endDate: string;
}

export interface AnalyticsTopicMetric {
  id: string;
  label: string;
  volume: number;
  sharePercent: number;
  trendPercent: number | null;
}

export interface AnalyticsTrendPoint {
  at: string;
  value: number;
  sampleCount: number;
}

export interface AnalyticsResponseMetrics {
  averageHandlingMinutes: number | null;
  silencePercent: number | null;
  turns: number | null;
  responseCoverage: number | null;
  responseOpportunityCount: number;
  respondedCount: number;
  provenance: MetricProvenanceDocument;
}

export interface AnalyticsCreatorMetrics {
  conversationCount: number;
  participantCount: number;
  messageCount: number;
  inboundMessageCount: number;
  outboundMessageCount: number;
  averageMessagesPerConversation: number | null;
  averageResponseSeconds: number | null;
  averageSentimentScore: number | null;
  responseCoverage: number | null;
  provenance: MetricProvenanceDocument;
}

export interface AnalyticsConversationInsight {
  conversationRef: ConversationRef;
  unreadCount: number;
  messageCount: number;
  averageSentimentScore: number | null;
  averageResponseSeconds: number | null;
  responseCoverage: number | null;
  topicCounts: Readonly<Record<string, number>>;
  engagementCounts: Readonly<Record<string, number>>;
  provenance: MetricProvenanceDocument;
  rangeProvenance: SliceProvenanceDocument;
}

export interface AnalyticsGraphSummary {
  sourceRevision: number;
  nodeCount: number;
  edgeCount: number;
  nodeCountsByKind: Readonly<Record<string, number>>;
  edgeCountsByRelation: Readonly<Record<string, number>>;
}

export interface AnalyticsReadModel {
  accountRef: AccountRef;
  sourceRevision: number;
  projectionGeneration: number;
  projectionDigest: string;
  canonicalContentDigest: string;
  graphDigest: string;
  pipelineRevision: string;
  pipelineConfigDigest: string;
  pipelineIdentityDigest: string;
  analyzerProvenance: readonly AnalyzerProvenanceDocument[];
  metricProvenance: Readonly<Record<string, MetricProvenanceDocument>>;
  windowSources: AnalyticsWindowSources;
  topics: readonly AnalyticsTopicMetric[];
  sentimentTrend: readonly AnalyticsTrendPoint[];
  response: AnalyticsResponseMetrics;
  creator: AnalyticsCreatorMetrics;
  conversations: readonly AnalyticsConversationInsight[];
  graph: AnalyticsGraphSummary;
}

export type AnalyticsFrameStatus = 'baseline' | 'model';
export type AnalyticsReadState =
  | { status: 'loading'; data: null; isRefreshing: false; message: string }
  | { status: 'building'; data: AnalyticsReadModel | null; isRefreshing: false; message: string; previousStatus: AnalyticsFrameStatus | null }
  | { status: 'unavailable'; data: null; isRefreshing: false; message: string }
  | { status: AnalyticsFrameStatus; data: AnalyticsReadModel; isRefreshing: boolean; message: string | null }
  | {
      status: 'error';
      data: AnalyticsReadModel | null;
      isRefreshing: false;
      message: string;
      previousStatus: AnalyticsFrameStatus | null;
    };

function windowSource(
  update: AnalyticsUpdateDocument,
  name: string,
): AnalyticsWindowSources[keyof AnalyticsWindowSources] {
  return {
    window: update.slice_windows[name],
    provenance: update.slice_provenance[name],
  };
}

export function adaptAnalyticsReadModel(update: AnalyticsUpdateDocument): AnalyticsReadModel {
  const conversationRange = windowSource(update, 'conversation_metrics').provenance;
  return {
    accountRef: update.account_ref,
    sourceRevision: update.source_revision,
    projectionGeneration: update.projection_generation,
    projectionDigest: update.projection_digest,
    canonicalContentDigest: update.canonical_content_digest,
    graphDigest: update.graph_digest,
    pipelineRevision: update.pipeline_revision,
    pipelineConfigDigest: update.pipeline_config_digest,
    pipelineIdentityDigest: update.pipeline_identity_digest,
    analyzerProvenance: update.analyzer_provenance,
    metricProvenance: update.metric_provenance,
    windowSources: {
      creatorMetrics: windowSource(update, 'creator_metrics'),
      responseMetrics: windowSource(update, 'response_time_metrics'),
      sentimentTrend: windowSource(update, 'sentiment_trend'),
      topics: windowSource(update, 'topics'),
      conversationInsights: windowSource(update, 'conversation_metrics'),
      graph: windowSource(update, 'graph'),
    },
    topics: update.topics
      .map((topic) => ({
        id: topic.topic,
        label: topic.topic,
        volume: topic.volume,
        sharePercent: topic.percentage_of_total,
        trendPercent: topic.trend,
      }))
      .sort((left, right) => right.volume - left.volume || left.label.localeCompare(right.label)),
    sentimentTrend: update.sentiment_trend.trend
      .map((point) => ({ at: point.date, value: point.sentiment_score, sampleCount: point.message_count }))
      .sort((left, right) => Date.parse(left.at) - Date.parse(right.at)),
    response: {
      averageHandlingMinutes: update.response_time_metrics.average_handling_time_minutes,
      silencePercent: update.response_time_metrics.silence_percentage,
      turns: update.response_time_metrics.turns,
      responseCoverage: update.response_time_metrics.response_coverage,
      responseOpportunityCount: update.response_time_metrics.response_opportunity_count,
      respondedCount: update.response_time_metrics.responded_count,
      provenance: update.response_time_metrics.provenance,
    },
    creator: {
      conversationCount: update.creator_metrics.conversation_count,
      participantCount: update.creator_metrics.participant_count,
      messageCount: update.creator_metrics.message_count,
      inboundMessageCount: update.creator_metrics.inbound_message_count,
      outboundMessageCount: update.creator_metrics.outbound_message_count,
      averageMessagesPerConversation: update.creator_metrics.average_messages_per_conversation,
      averageResponseSeconds: update.creator_metrics.average_response_seconds,
      averageSentimentScore: update.creator_metrics.average_sentiment_score,
      responseCoverage: update.creator_metrics.response_coverage,
      provenance: update.creator_metrics.provenance,
    },
    conversations: update.conversation_metrics
      .map((conversation) => ({
        conversationRef: conversation.conversation_ref,
        unreadCount: conversation.unread_count,
        messageCount: conversation.message_count,
        averageSentimentScore: conversation.average_sentiment_score,
        averageResponseSeconds: conversation.average_response_seconds,
        responseCoverage: conversation.response_coverage,
        topicCounts: conversation.topic_counts,
        engagementCounts: conversation.engagement_counts,
        provenance: conversation.provenance,
        rangeProvenance: conversationRange,
      }))
      .sort((left, right) => left.conversationRef.localeCompare(right.conversationRef)),
    graph: {
      sourceRevision: update.graph.source_revision,
      nodeCount: update.graph.node_count,
      edgeCount: update.graph.edge_count,
      nodeCountsByKind: update.graph.node_counts_by_kind,
      edgeCountsByRelation: update.graph.edge_counts_by_relation,
    },
  };
}

function provenanceIsModel(provenance: AnalyzerProvenanceDocument | MetricProvenanceDocument): boolean {
  return provenance.mode === 'model' && provenance.calibration_status === 'calibrated';
}

export function classifyAnalyticsModel(model: AnalyticsReadModel): AnalyticsFrameStatus {
  const displayedProvenance = [
    ...model.analyzerProvenance,
    model.response.provenance,
    model.creator.provenance,
    ...model.conversations.map((conversation) => conversation.provenance),
  ];
  return displayedProvenance.length > 0 && displayedProvenance.every(provenanceIsModel)
    ? 'model'
    : 'baseline';
}

export function resolveConversationInsight(
  model: AnalyticsReadModel | null,
  refs: AnalyticsRefMap | null,
  canonicalConversationId: string | null,
): AnalyticsConversationInsight | null {
  if (!model || !refs || !canonicalConversationId) return null;
  const analyticsRef = refs.resolveConversation(canonicalConversationId);
  if (!analyticsRef) return null;
  return model.conversations.find((conversation) => conversation.conversationRef === analyticsRef) ?? null;
}
