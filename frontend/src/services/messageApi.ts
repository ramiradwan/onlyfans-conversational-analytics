import type { MessagePageResponse } from '../protocol';
import {
  array,
  boolean,
  conversationCoverage,
  integer,
  isoDateTime,
  messageView,
  nonEmptyString,
  nullable,
  object,
  projectionState,
} from '../protocol/validation';

const messagePage = object({
  creator_account_id: nonEmptyString,
  conversation_id: nonEmptyString,
  projection_generation: nonEmptyString,
  read_revision: integer(0),
  generated_at: isoDateTime,
  items: array(messageView),
  older_cursor: nullable(nonEmptyString),
  has_older_stored_items: boolean,
  conversation_coverage: conversationCoverage,
  projection: projectionState,
});

export class MessageApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = 'MessageApiError';
  }
}

export class StaleMessageCursorError extends MessageApiError {
  constructor() {
    super('The message window changed while paging. Reloading the latest window is required.', 409);
    this.name = 'StaleMessageCursorError';
  }
}

export interface MessageApi {
  getPage(input: {
    before?: string | null;
    conversationId: string;
    limit?: number;
    signal?: AbortSignal;
  }): Promise<MessagePageResponse>;
}

interface MessageApiOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
}

function endpoint(baseUrl: string, conversationId: string, before: string | null, limit: number) {
  const base = baseUrl.replace(/\/$/, '');
  const path = `${base}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`;
  const url = new URL(path, window.location.origin);
  url.searchParams.set('limit', String(limit));
  if (before !== null) url.searchParams.set('before', before);
  return url;
}

export function createMessageApi(options: MessageApiOptions = {}): MessageApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const baseUrl = options.baseUrl ?? '';

  return {
    async getPage({ before = null, conversationId, limit = 50, signal }) {
      if (!conversationId.trim()) throw new MessageApiError('A conversation is required.');
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
        throw new MessageApiError('Message page size must be between 1 and 100.');
      }
      const response = await request(endpoint(baseUrl, conversationId, before, limit), {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        method: 'GET',
        signal,
      });
      if (response.status === 409) throw new StaleMessageCursorError();
      if (!response.ok) {
        throw new MessageApiError(`Message history request failed (${response.status}).`, response.status);
      }
      let document: unknown;
      try {
        document = await response.json();
        messagePage(document, '$');
        const page = document as MessagePageResponse;
        if (page.conversation_id !== conversationId) {
          throw new MessageApiError('Brain returned a page for another conversation.', response.status);
        }
        if (page.has_older_stored_items !== (page.older_cursor !== null)) {
          throw new MessageApiError('Brain returned inconsistent message paging state.', response.status);
        }
        for (let index = 1; index < page.items.length; index += 1) {
          const previous = page.items[index - 1];
          const current = page.items[index];
          const previousTimestamp = Date.parse(previous.sent_at);
          const currentTimestamp = Date.parse(current.sent_at);
          if (
            previousTimestamp > currentTimestamp ||
            (previousTimestamp === currentTimestamp && previous.message_id >= current.message_id)
          ) {
            throw new MessageApiError('Brain returned an unstable message order.', response.status);
          }
        }
      } catch (error) {
        if (error instanceof MessageApiError) throw error;
        throw new MessageApiError('Brain returned an invalid message page.', response.status);
      }
      return document as MessagePageResponse;
    },
  };
}

export const messageApi = createMessageApi();
