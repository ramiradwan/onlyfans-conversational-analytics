import type {
  AnalyticsView,
  ConversationRecord,
  MessageView,
} from '../protocol';
import type { BridgeTransportState } from '../store/transportStore';

export type Sentiment = MessageView['sentiment'];

export interface MessageActivityPoint {
  [key: string]: string | number;
  date: string;
  inbound: number;
  outbound: number;
  total: number;
}

export interface SentimentCount {
  sentiment: Sentiment;
  label: string;
  count: number;
}

export interface ActiveConversation {
  conversationId: string;
  displayName: string;
  platformUserId: string;
  messageCount: number;
  inboundCount: number;
  outboundCount: number;
  lastMessageAt: string | null;
}

export interface CreatorDashboardModel {
  analytics: AnalyticsView | null;
  messageActivity: MessageActivityPoint[];
  sentimentCounts: SentimentCount[];
  mostActiveConversations: ActiveConversation[];
}

const SENTIMENTS: ReadonlyArray<{ sentiment: Sentiment; label: string }> = [
  { sentiment: 'positive', label: 'Positive' },
  { sentiment: 'neutral', label: 'Neutral' },
  { sentiment: 'negative', label: 'Negative' },
  { sentiment: 'unknown', label: 'Unknown' },
];

function sortableTimestamp(date: string | null): number {
  if (date === null) return Number.NEGATIVE_INFINITY;
  const timestamp = Date.parse(date);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}

function conversationMessages(): readonly MessageView[] {
  return [];
}

function latestMessageTimestamp(conversation: ConversationRecord): string | null {
  let latest: string | null = null;
  let latestTimestamp = Number.NEGATIVE_INFINITY;
  const candidateDates = [
    ...(conversation.last_message_at === null ? [] : [conversation.last_message_at]),
    ...conversationMessages().map(({ sent_at: sentAt }) => sentAt),
    ...(conversation.latest_message !== null
      ? [conversation.latest_message.sent_at]
      : []),
  ];

  for (const candidate of candidateDates) {
    const timestamp = Date.parse(candidate);
    if (Number.isFinite(timestamp) && timestamp > latestTimestamp) {
      latest = candidate;
      latestTimestamp = timestamp;
    }
  }

  return latest;
}

export function deriveUtcMessageActivity(
  conversations: readonly ConversationRecord[],
): MessageActivityPoint[] {
  void conversations;
  return [];
}

export function deriveSentimentCounts(
  conversations: readonly ConversationRecord[],
): SentimentCount[] {
  void conversations;
  return SENTIMENTS.map(({ sentiment, label }) => ({ sentiment, label, count: 0 }));
}

export function deriveMostActiveConversations(
  conversations: readonly ConversationRecord[],
  limit = 5,
): ActiveConversation[] {
  return conversations
    .map((conversation) => ({
      conversationId: conversation.conversation_id,
      displayName:
        conversation.display_name?.trim() ||
        conversation.platform_user_id ||
        'Unknown fan',
      platformUserId: conversation.platform_user_id ?? conversation.conversation_id,
      messageCount: conversationMessages().length,
      inboundCount: conversationMessages().filter(({ direction }) => direction === 'inbound')
        .length,
      outboundCount: conversationMessages().filter(({ direction }) => direction === 'outbound')
        .length,
      lastMessageAt: latestMessageTimestamp(conversation),
    }))
    .sort(
      (left, right) =>
        right.messageCount - left.messageCount ||
        sortableTimestamp(right.lastMessageAt) - sortableTimestamp(left.lastMessageAt) ||
        left.conversationId.localeCompare(right.conversationId),
    )
    .slice(0, Math.max(0, limit));
}

export function buildCreatorDashboardModel(
  analytics: AnalyticsView | null,
  conversations: readonly ConversationRecord[],
): CreatorDashboardModel {
  return {
    analytics,
    messageActivity: deriveUtcMessageActivity(conversations),
    sentimentCounts: deriveSentimentCounts(conversations),
    mostActiveConversations: deriveMostActiveConversations(conversations),
  };
}

export function createCreatorDashboardModel(
  state: Readonly<Pick<BridgeTransportState, 'analytics' | 'conversations'>>,
): CreatorDashboardModel {
  return buildCreatorDashboardModel(state.analytics, state.conversations);
}

export const flattenMessages = (conversations: readonly ConversationRecord[]): MessageView[] =>
  conversations.flatMap(() => [...conversationMessages()]);
export const buildMessageActivity = deriveUtcMessageActivity;
export const buildSentimentBreakdown = deriveSentimentCounts;
export const rankActiveConversations = deriveMostActiveConversations;
