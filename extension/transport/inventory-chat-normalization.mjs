import { normalizedMaterialEqual } from './entity-merge.mjs';

const CHAT_KEYS = ['chat_id', 'record_kind', 'platform_user_id', 'display_name', 'updated_at'];
const isFullChat = (chat) => chat?.record_kind === 'full'
  && Object.keys(chat).length === CHAT_KEYS.length
  && CHAT_KEYS.every((key) => Object.hasOwn(chat, key))
  && (chat.display_name === null || typeof chat.display_name === 'string');

/**
 * Inventory establishes membership, not a newer profile label. Keep the already
 * committed label when that alone disagrees at the same activity version.
 * This is applied inside the page transaction; canonical merge remains strict.
 */
export function inventoryChatMaterial(existing, incoming) {
  if (!isFullChat(existing) || !isFullChat(incoming)
    || existing.display_name === incoming.display_name) return incoming;
  const existingTime = Date.parse(existing.updated_at);
  if (!Number.isFinite(existingTime) || Date.parse(incoming.updated_at) !== existingTime) return incoming;
  const retainedLabel = {
    ...incoming, display_name: existing.display_name, updated_at: existing.updated_at,
  };
  return normalizedMaterialEqual(existing, retainedLabel) ? retainedLabel : incoming;
}
