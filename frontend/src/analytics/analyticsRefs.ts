import { AnalyticsContractError, type ConversationRef } from './analyticsContract';

export interface AnalyticsRefMap {
  readonly generation: number;
  readonly sourceRevision: number;
  resolveConversation(canonicalConversationId: string): ConversationRef | null;
}

/**
 * Builds a lookup from canonical conversation identifiers to the opaque analytics
 * reference used by `AnalyticsUpdateDocument.conversation_metrics`.
 *
 * Nothing currently supplies canonical-id/ref pairs to the frontend: protocol-v2's
 * `ConversationSummary` (see `../protocol`) carries no analytics reference, and there
 * is no REST bootstrap endpoint that returns one alongside conversations. This factory
 * exists so conversation-level analytics lookups (see `resolveConversationInsight`) are
 * ready to activate once such a pairing is exposed; callers without a source of pairs
 * should not construct a map and should treat conversation-level insight as unavailable.
 */
export function buildAnalyticsRefMap(
  pairs: Iterable<{ canonicalConversationId: string; conversationRef: ConversationRef }>,
  generation: number,
  sourceRevision: number,
): AnalyticsRefMap {
  const canonicalToOpaque = new Map<string, ConversationRef>();
  for (const pair of pairs) {
    if (canonicalToOpaque.has(pair.canonicalConversationId)) {
      throw new AnalyticsContractError('Duplicate canonical conversation identifiers were supplied.');
    }
    canonicalToOpaque.set(pair.canonicalConversationId, pair.conversationRef);
  }
  return Object.freeze({
    generation,
    sourceRevision,
    resolveConversation(canonicalConversationId: string) {
      return canonicalToOpaque.get(canonicalConversationId) ?? null;
    },
  });
}
