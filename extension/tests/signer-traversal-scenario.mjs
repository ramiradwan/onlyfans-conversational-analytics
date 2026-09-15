// Synthetic deterministic native pages shared by Node and real-browser recovery qualification.
export const TRAVERSAL_ACCOUNT = 'synthetic-companion-account';
export const TRAVERSAL_CREATOR = '9001';
export const TRAVERSAL_TIME = '2026-09-11T12:00:00.000Z';
export const TRAVERSAL_CHAT_IDS = ['101', '102', '103'];
export const TRAVERSAL_MESSAGE_IDS = ['897', '898', '899', '900', '901'];
export const TRAVERSAL_KEY = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

const conversation = (id) => ({ id, withUser: { id, name: `Synthetic ${id}` },
  updatedAt: '2026-09-10T12:00:00.000Z' });
const message = (id, chatId = '101', sender = chatId) => ({
  id, chatUserId: chatId, fromUser: { id: sender }, text: `Synthetic message ${id}`,
  createdAt: '2026-09-10T11:00:00.000Z',
});

export function traversalConfiguration(overrides = {}) {
  return { creator_account_id: TRAVERSAL_ACCOUNT, config_revision: 'synthetic-config-v1',
    history_acquisition: { enabled: true, consent_revision: 'synthetic-consent-v1',
      authorized_platform_creator_id: TRAVERSAL_CREATOR, recent_window_days: 30,
      page_size: 2, pages_per_wake: 1, request_interval_ms: 0, retry_limit: 1,
      ...overrides,
    },
  };
}

export function nativeTraversalBody(request) {
  const url = new URL(request.url);
  if (request.operation === 'identity') return { id: TRAVERSAL_CREATOR };
  if (request.operation === 'conversations') {
    const offset = url.searchParams.get('offset');
    if (offset === null) return { list: ['101', '102'].map(conversation), hasMore: true, nextOffset: 2 };
    if (offset === '2') return { list: ['102', '103'].map(conversation), hasMore: false, nextOffset: 0 };
  }
  if (request.operation === 'message-page') {
    if (url.searchParams.get('order') !== 'desc' || url.searchParams.has('lastId')) {
      throw new Error('Expected signer-owned descending pagination');
    }
    if (url.pathname.endsWith('/101/messages')) {
      const anchor = url.searchParams.get('id');
      if (anchor === null) return { list: ['900', '899'].map((id) => message(id)), hasMore: true };
      if (anchor === '899') return { list: ['899', '898'].map((id) => message(id)), hasMore: true };
      if (anchor === '898') return { list: [message('897')], hasMore: false };
    }
    if (url.pathname.endsWith('/102/messages')) return { list: [], hasMore: false };
    if (url.pathname.endsWith('/103/messages')) return { list: [message('901', '103', TRAVERSAL_CREATOR)], hasMore: false };
  }
  throw new Error('Unexpected synthetic traversal request');
}

export function expectedTraversalMessage(id) {
  return { message_id: id, chat_id: id === '901' ? '103' : '101',
    sender_platform_user_id: id === '901' ? TRAVERSAL_CREATOR : '101',
    text: `Synthetic message ${id}`, sent_at: '2026-09-10T11:00:00.000Z',
    direction: id === '901' ? 'outbound' : 'inbound',
  };
}
