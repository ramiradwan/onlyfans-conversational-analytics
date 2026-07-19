const PLATFORM_ORIGIN = 'https://onlyfans.com';
const PLATFORM_SOCKET = 'wss://ws2.onlyfans.com/ws3/';
const BRAIN_ORIGIN = 'http://bridge.localhost:17871';

export const SYNTHETIC = Object.freeze({
  creatorId: 'fixture-creator',
  chatId: 'fixture-peer-primary',
  displayName: 'Synthetic Primary Fan',
  historyMessageIds: Object.freeze([
    'fixture-message-history-1',
    'fixture-message-history-2',
    'fixture-message-history-3',
  ]),
  historyTexts: Object.freeze([
    'Synthetic history message one',
    'Synthetic history message two',
    'Synthetic history message three',
  ]),
  messageOnlyPeerId: 'fixture-peer-message-only',
  messageOnlyMessageId: 'fixture-message-peer-only',
  messageOnlyText: 'Synthetic message-only peer observation',
  offlinePeerId: 'fixture-peer-offline',
  offlineMessageId: 'fixture-message-offline',
  offlineText: 'Synthetic offline peer observation',
});

const SYNTHETIC_PAGE = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>OFCA synthetic platform</title></head>
  <body>
    <main><h1>OFCA synthetic platform</h1></main>
    <script>
      (() => {
        globalThis.fixtureDocumentToken = crypto.randomUUID();
        const allowedReads = new Set([
          '/api2/v2/users/me',
          '/api2/v2/chats',
          '/api2/v2/chats/${SYNTHETIC.chatId}/messages',
        ]);
        globalThis.fixtureRead = async (path) => {
          if (!allowedReads.has(path)) throw new Error('Fixture read is not allow-listed');
          const response = await fetch(path, {
            method: 'GET',
            credentials: 'include',
            cache: 'no-store',
          });
          if (!response.ok) throw new Error('Synthetic read failed');
          await response.arrayBuffer();
          return response.status;
        };
        globalThis.fixtureOpenSocket = () => new Promise((resolve, reject) => {
          if (globalThis.fixtureSocket?.readyState === WebSocket.OPEN) {
            resolve(true);
            return;
          }
          const socket = new WebSocket('${PLATFORM_SOCKET}');
          globalThis.fixtureSocket = socket;
          socket.addEventListener('open', () => resolve(true), { once: true });
          socket.addEventListener('error', () => reject(new Error('Synthetic socket failed')), {
            once: true,
          });
        });
      })();
    </script>
  </body>
</html>`;

function jsonResponse(route, document) {
  return route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    headers: { 'cache-control': 'no-store' },
    body: JSON.stringify(document),
  });
}

function safeRequestShape(request) {
  const url = new URL(request.url());
  return { method: request.method(), origin: url.origin, pathname: url.pathname };
}

export class SyntheticPlatform {
  constructor() {
    this.httpReads = [];
    this.mutationAttempts = [];
    this.unexpectedRequests = [];
    this.openSockets = new Set();
    this.websocketFramesSent = 0;
  }

  async install(context) {
    await context.routeWebSocket(`${PLATFORM_SOCKET}**`, (socket) => {
      this.openSockets.add(socket);
      socket.onClose(() => this.openSockets.delete(socket));
      socket.onMessage(() => {
        // Client-to-platform messages are intentionally ignored. The fixture never mutates state.
      });
    });

    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === BRAIN_ORIGIN) {
        await route.continue();
        return;
      }
      if (url.origin !== PLATFORM_ORIGIN) {
        this.unexpectedRequests.push(safeRequestShape(request));
        await route.abort('blockedbyclient');
        return;
      }
      if (request.method() !== 'GET') {
        this.mutationAttempts.push(safeRequestShape(request));
        await route.abort('blockedbyclient');
        return;
      }

      this.httpReads.push(url.pathname);
      if (url.pathname === '/') {
        await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          headers: { 'cache-control': 'no-store' },
          body: SYNTHETIC_PAGE,
        });
        return;
      }
      if (url.pathname === '/favicon.ico') {
        await route.fulfill({ status: 204, body: '' });
        return;
      }
      if (url.pathname === '/api2/v2/users/me') {
        await jsonResponse(route, { id: SYNTHETIC.creatorId });
        return;
      }
      if (url.pathname === '/api2/v2/chats') {
        await jsonResponse(route, {
          list: [
            {
              withUser: { id: SYNTHETIC.chatId, name: SYNTHETIC.displayName },
              lastMessage: { createdAt: '2026-07-19T08:02:00Z' },
            },
          ],
        });
        return;
      }
      if (url.pathname === `/api2/v2/chats/${SYNTHETIC.chatId}/messages`) {
        await jsonResponse(route, {
          response: {
            result: {
              items: [
                {
                  id: SYNTHETIC.historyMessageIds[0],
                  text: SYNTHETIC.historyTexts[0],
                  createdAt: '2026-07-19T08:00:00Z',
                  fromUser: { id: SYNTHETIC.chatId, isMe: false },
                },
                {
                  messageId: SYNTHETIC.historyMessageIds[1],
                  body: SYNTHETIC.historyTexts[1],
                  postedAt: '2026-07-19T08:01:00Z',
                  senderId: SYNTHETIC.creatorId,
                  isOutgoing: true,
                },
                {
                  message_id: SYNTHETIC.historyMessageIds[2],
                  text: SYNTHETIC.historyTexts[2],
                  sent_at: '2026-07-19T08:02:00Z',
                  sender: { id: SYNTHETIC.chatId },
                  direction: 'inbound',
                },
              ],
            },
          },
        });
        return;
      }

      this.unexpectedRequests.push(safeRequestShape(request));
      await route.abort('blockedbyclient');
    });
  }

  sendInitialMessageOnlyPeer() {
    this.#sendFrame({
      api2_chat_message: {
        id: SYNTHETIC.messageOnlyMessageId,
        text: SYNTHETIC.messageOnlyText,
        createdAt: '2026-07-19T08:03:00Z',
        fromUser: { id: SYNTHETIC.messageOnlyPeerId, isMe: false },
      },
    });
  }

  sendOfflineMessageOnlyPeer() {
    this.#sendFrame({
      new_message: {
        id: SYNTHETIC.offlineMessageId,
        text: SYNTHETIC.offlineText,
        createdAt: '2026-07-19T08:04:00Z',
        fromUser: { id: SYNTHETIC.offlinePeerId, isMe: false },
      },
    });
  }

  #sendFrame(document) {
    if (this.openSockets.size === 0) {
      throw new Error('Synthetic platform WebSocket is not open.');
    }
    const frame = JSON.stringify(document);
    for (const socket of this.openSockets) socket.send(frame);
    this.websocketFramesSent += 1;
  }

  assertFailClosed() {
    if (this.mutationAttempts.length > 0) {
      throw new Error(`Synthetic platform observed mutation attempts: ${JSON.stringify(this.mutationAttempts)}`);
    }
    if (this.unexpectedRequests.length > 0) {
      throw new Error(`Browser attempted non-fixture network access: ${JSON.stringify(this.unexpectedRequests)}`);
    }
  }
}
