import type { ConversationRecord, MessageView } from '../../protocol';
import { conversationLatestMessage } from '../../store/transportStore';

function timestampValue(value: string | null): number {
  if (value === null) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

export function getConversationTitle(conversation: ConversationRecord): string {
  const displayName = conversation.display_name?.trim();
  return displayName ||
    (conversation.platform_user_id ? `Fan ${conversation.platform_user_id}` : 'Unknown fan');
}

export function getLastMessage(messages: readonly MessageView[]): MessageView | null {
  return messages.reduce<MessageView | null>((latest, message) => {
    if (latest === null) return message;
    const timeDifference = timestampValue(message.sent_at) - timestampValue(latest.sent_at);
    if (timeDifference > 0) return message;
    if (timeDifference === 0 && message.message_id > latest.message_id) return message;
    return latest;
  }, null);
}

export function sortConversations(
  conversations: readonly ConversationRecord[],
): ConversationRecord[] {
  return [...conversations].sort((left, right) => {
    const leftLastMessage = conversationLatestMessage(left);
    const rightLastMessage = conversationLatestMessage(right);
    const leftTimestamp = left.last_message_at ?? leftLastMessage?.sent_at ?? null;
    const rightTimestamp = right.last_message_at ?? rightLastMessage?.sent_at ?? null;
    const timeDifference = timestampValue(rightTimestamp) - timestampValue(leftTimestamp);
    if (timeDifference !== 0) return timeDifference;

    const leftTitle = getConversationTitle(left).toLowerCase();
    const rightTitle = getConversationTitle(right).toLowerCase();
    if (leftTitle < rightTitle) return -1;
    if (leftTitle > rightTitle) return 1;
    return left.conversation_id < right.conversation_id ? -1 : 1;
  });
}

export function sortMessages(messages: readonly MessageView[]): MessageView[] {
  return [...messages].sort((left, right) => {
    const timeDifference = timestampValue(left.sent_at) - timestampValue(right.sent_at);
    if (timeDifference !== 0) return timeDifference;
    return left.message_id < right.message_id ? -1 : 1;
  });
}

export function formatTimestamp(value: string | null, now: Date = new Date(), timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone): string {
  if (value === null) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';

  const time = new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    hour12: false,
    minute: '2-digit',
    timeZone,
  }).format(date);

  const day = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
  const year = new Intl.DateTimeFormat('en', { timeZone, year: 'numeric' });
  const sameYear = year.format(date) === year.format(now);
  if (day.format(date) === day.format(now)) return time;

  const dateLabel = new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    timeZone,
    ...(sameYear ? {} : { year: 'numeric' }),
  }).format(date);
  return `${dateLabel}, ${time}`;
}
