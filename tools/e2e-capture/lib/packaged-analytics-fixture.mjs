export const chats = Array.from({ length: 101 }, (_, index) => `chat-${index}`);

function instant(milliseconds) { return new Date(milliseconds).toISOString(); }

function record(id, chat, sender, text, milliseconds) {
  return { id, chat_id: chats[chat], chatUserId: chats[chat], fromUser: { id: sender },
    text, createdAt: instant(milliseconds) };
}

export function seedMessage(index, size, input) {
  const half = Math.floor(size / 2);
  const chat = index < half ? 0 : 1 + ((index - half) % 100);
  const position = index < half ? index : Math.floor((index - half) / 100);
  return record(`matrix-input-${index}`, chat,
    position % 2 ? input.synthetic_account_id : chats[chat], `Synthetic support message ${index}`,
    Date.parse(input.evaluation_clock) - 48 * 3600000 + index * 48 * 3600000 / size);
}

export function appendedMessage(identity, chat, input) {
  return record(identity, chat, chats[chat], 'Synthetic current message',
    Date.parse(input.evaluation_clock) - 1);
}

export function editedMessages(messages) {
  return Array.from({ length: 100 }, (_, index) => {
    const value = messages.get(`matrix-input-${index}`);
    if (!value) throw new Error('synthetic_edit_source_missing');
    return { ...value, text: `Synthetic changed support ${index}` };
  });
}

export function historicalMessages(batch, input) {
  return Array.from({ length: 100 }, (_, offset) => {
    const index = batch * 100 + offset;
    return record(`matrix-history-${index}`, 0, chats[0], `Synthetic historical message ${index}`,
      Date.parse(input.evaluation_clock) - 30 * 86400000 + index * 1000);
  });
}

export function interleavedMessage(batch, input) {
  return record(`matrix-interleaved-live-${batch}`, 1 + batch, chats[1 + batch],
    'Synthetic interleaved message', Date.parse(input.evaluation_clock) - 100 + batch);
}
