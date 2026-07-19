(function installObservationHook() {
  if (globalThis.__OFCA_PAGE_HOOK_ACTIVE__) return;
  globalThis.__OFCA_PAGE_HOOK_ACTIVE__ = true;

  const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
  const PROTOCOL_VERSION = '2';
  const MAX_PAYLOAD_WRAPPER_DEPTH = 3;
  const PAYLOAD_WRAPPER_KEYS = Object.freeze(['data', 'response', 'result']);
  const targetOrigin = window.location.origin;
  const xhrUrls = new WeakMap();
  let creatorPlatformUserId = null;

  function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  function identifier(value) {
    if (typeof value === 'string' && value.trim().length > 0) return value.trim();
    if (Number.isSafeInteger(value) && value >= 0) return String(value);
    return null;
  }

  function postObservation(observation) {
    try {
      window.postMessage(
        {
          type: CAPTURE_MESSAGE_TYPE,
          protocol_version: PROTOCOL_VERSION,
          observation,
        },
        targetOrigin,
      );
    } catch (_error) {
      // A non-cloneable platform record is ignored without exposing its contents.
    }
  }

  function postDiagnostic(sourceEventType, code, sourcePath) {
    postObservation({
      event_type: 'hook.diagnostic',
      source_event_type: sourceEventType,
      code,
      observed_at: new Date().toISOString(),
      source_path: sourcePath,
    });
  }

  function resolveUrl(input) {
    try {
      const value = typeof input === 'string' || input instanceof URL
        ? String(input)
        : input?.url;
      return typeof value === 'string' ? new URL(value, window.location.href) : null;
    } catch (_error) {
      return null;
    }
  }

  function classifyPath(pathname) {
    if (/^\/api2\/v2\/(?:users\/me|init)\/?$/.test(pathname)) return 'identity';
    if (/^\/api2\/v2\/(?:chats|users\/[^/]+\/chats)\/?$/.test(pathname)) return 'chats';
    if (/^\/api2\/v2\/chats\/[^/]+\/messages\/?$/.test(pathname)) return 'messages';
    return null;
  }

  function boundedPayloads(value) {
    const pending = [{ value, depth: 0 }];
    const payloads = [];
    const seen = new Set();
    while (pending.length > 0) {
      const candidate = pending.shift();
      if (
        candidate === undefined
        || candidate.depth > MAX_PAYLOAD_WRAPPER_DEPTH
        || (!isRecord(candidate.value) && !Array.isArray(candidate.value))
        || seen.has(candidate.value)
      ) continue;
      seen.add(candidate.value);
      payloads.push(candidate.value);
      if (!isRecord(candidate.value) || candidate.depth === MAX_PAYLOAD_WRAPPER_DEPTH) continue;
      for (const key of PAYLOAD_WRAPPER_KEYS) {
        if (Object.hasOwn(candidate.value, key)) {
          pending.push({ value: candidate.value[key], depth: candidate.depth + 1 });
        }
      }
    }
    return payloads;
  }

  function recordsFrom(value, keys) {
    const payloads = boundedPayloads(value);
    for (const payload of payloads) {
      if (Array.isArray(payload)) {
        const records = payload.filter(isRecord);
        return {
          recognized: payload.length === 0 || records.length > 0,
          records,
        };
      }
      for (const key of keys) {
        if (Object.hasOwn(payload, key) && Array.isArray(payload[key])) {
          const records = payload[key].filter(isRecord);
          return {
            recognized: payload[key].length === 0 || records.length > 0,
            records,
          };
        }
      }
    }
    return { recognized: false, records: [] };
  }

  function chatRecords(body) {
    const extraction = recordsFrom(body, ['list', 'chats', 'items']);
    if (extraction.recognized) return extraction;
    const record = boundedPayloads(body).find((payload) => (
      isRecord(payload)
      && (isRecord(payload.withUser) || isRecord(payload.with_user))
    ));
    return record === undefined
      ? extraction
      : { recognized: true, records: [record] };
  }

  function messageRecords(body) {
    const extraction = recordsFrom(body, ['list', 'messages', 'items']);
    if (extraction.recognized) return extraction;
    for (const payload of boundedPayloads(body)) {
      if (!isRecord(payload)) continue;
      if (isRecord(payload.message)) {
        return { recognized: true, records: [payload.message] };
      }
      if (
        ('text' in payload || 'body' in payload)
        && ('id' in payload || 'message_id' in payload || 'messageId' in payload)
      ) return { recognized: true, records: [payload] };
    }
    return extraction;
  }

  function contextChatId(pathname) {
    const match = /^\/api2\/v2\/chats\/([^/]+)\/messages\/?$/.exec(pathname);
    if (!match) return null;
    try {
      return decodeURIComponent(match[1]);
    } catch (_error) {
      return match[1];
    }
  }

  function updateCreatorIdentity(pathname, body) {
    const rawId = /^\/api2\/v2\/users\/me\/?$/.test(pathname)
      ? body?.id
      : body?.user?.id;
    const detected = identifier(rawId);
    if (detected !== null) creatorPlatformUserId = detected;
  }

  function emitRecords(resource, pathname, body, sourceEventType) {
    const extraction = resource === 'chats' ? chatRecords(body) : messageRecords(body);
    if (!extraction.recognized) {
      postDiagnostic(sourceEventType, 'unrecognized_payload', pathname);
      return;
    }
    const { records } = extraction;
    const observedAt = new Date().toISOString();
    const chatId = resource === 'messages' ? contextChatId(pathname) : null;
    for (const record of records) {
      postObservation({
        event_type: resource === 'chats' ? 'chat.observed' : 'message.observed',
        observed_at: observedAt,
        source_path: pathname,
        creator_platform_user_id: creatorPlatformUserId,
        context_chat_id: chatId,
        record,
      });
    }
  }

  function handleResponseBody(url, body, sourceEventType) {
    const resource = classifyPath(url.pathname);
    if (resource === 'identity') {
      updateCreatorIdentity(url.pathname, body);
      return;
    }
    if (resource === 'chats' || resource === 'messages') {
      emitRecords(resource, url.pathname, body, sourceEventType);
    }
  }

  async function observeFetchResponse(url, response) {
    try {
      handleResponseBody(url, await response.clone().json(), 'http.response');
    } catch (_error) {
      postDiagnostic('http.response', 'invalid_json', url.pathname);
    }
  }

  function webSocketMessageRecords(frame) {
    if (!isRecord(frame)) return [];
    for (const key of ['api2_chat_message', 'new_message']) {
      if (!Object.hasOwn(frame, key)) continue;
      const extraction = messageRecords(frame[key]);
      if (extraction.recognized) return extraction.records;
    }
    const eventName = String(frame.type ?? frame.event ?? frame.method ?? '').toLowerCase();
    const messageEvents = new Set([
      'new_message',
      'message',
      'message_created',
      'message_updated',
      'messages.new',
      'chat.message',
      'chat_message',
    ]);
    if (!messageEvents.has(eventName)) return [];
    const payload = frame.data ?? frame.payload ?? frame.message ?? frame;
    const extraction = messageRecords(payload);
    return extraction.recognized ? extraction.records : [];
  }

  function webSocketContextChatId(record, frame) {
    const explicit = identifier(
      record.chat_id
      ?? record.chatId
      ?? record.chat?.id
      ?? frame.chat_id
      ?? frame.chatId,
    );
    if (explicit !== null) return explicit;
    const senderId = identifier(
      record.sender_platform_user_id
      ?? record.senderPlatformUserId
      ?? record.sender_id
      ?? record.senderId
      ?? record.fromUser?.id
      ?? record.from_user?.id
      ?? record.sender?.id,
    );
    const recipientId = identifier(
      record.toUser?.id
      ?? record.to_user?.id
      ?? record.recipient?.id
      ?? record.recipient_id
      ?? record.recipientId,
    );
    const creatorId = identifier(creatorPlatformUserId);
    if (creatorId === null) {
      const senderIsCreator = record.isFromCreator
        ?? record.is_from_creator
        ?? record.isOutgoing
        ?? record.is_outgoing
        ?? record.outgoing
        ?? record.fromUser?.is_me
        ?? record.fromUser?.isMe
        ?? record.fromUser?.me
        ?? record.from_user?.is_me;
      if (senderIsCreator === true) return recipientId;
      if (senderIsCreator === false) return senderId;
      const direction = typeof record.direction === 'string'
        ? record.direction.toLowerCase()
        : null;
      if (['outbound', 'creator', 'outgoing', 'sent'].includes(direction)) {
        return recipientId;
      }
      if (['inbound', 'fan', 'incoming', 'received'].includes(direction)) {
        return senderId;
      }
      return null;
    }
    if (senderId !== null && senderId !== creatorId) return senderId;
    return recipientId !== null && recipientId !== creatorId ? recipientId : null;
  }

  if (typeof window.WebSocket === 'function') {
    const OriginalWebSocket = window.WebSocket;
    window.WebSocket = new Proxy(OriginalWebSocket, {
      construct(target, argumentsList, newTarget) {
        const socket = Reflect.construct(target, argumentsList, newTarget);
        const url = resolveUrl(argumentsList[0]);
        if (url?.protocol === 'wss:' && url.hostname === 'ws2.onlyfans.com') {
          socket.addEventListener('message', (event) => {
            if (typeof event.data !== 'string') return;
            let frame;
            try {
              frame = JSON.parse(event.data);
            } catch (_error) {
              postDiagnostic('websocket.message', 'invalid_json', url.pathname);
              return;
            }
            const records = webSocketMessageRecords(frame);
            if (records.length === 0) return;
            const observedAt = new Date().toISOString();
            for (const record of records) {
              postObservation({
                event_type: 'message.observed',
                observed_at: observedAt,
                source_path: url.pathname,
                creator_platform_user_id: creatorPlatformUserId,
                context_chat_id: webSocketContextChatId(record, frame),
                record,
              });
            }
          });
        }
        return socket;
      },
    });
  }

  if (typeof window.fetch === 'function') {
    const originalFetch = window.fetch;
    window.fetch = async function observedFetch(...args) {
      const url = resolveUrl(args[0]);
      const response = await originalFetch.apply(this, args);
      if (
        url?.origin === targetOrigin
        && classifyPath(url.pathname) !== null
      ) void observeFetchResponse(url, response);
      return response;
    };
  }

  if (typeof XMLHttpRequest === 'function') {
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function observedOpen(_method, rawUrl, ...rest) {
      xhrUrls.set(this, resolveUrl(rawUrl));
      return originalOpen.call(this, _method, rawUrl, ...rest);
    };

    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function observedSend(...args) {
      const url = xhrUrls.get(this);
      if (url?.origin === targetOrigin && classifyPath(url.pathname) !== null) {
        this.addEventListener('load', () => {
          try {
            const body = this.responseType === 'json'
              ? this.response
              : JSON.parse(this.responseText);
            handleResponseBody(url, body, 'http.response');
          } catch (_error) {
            postDiagnostic('http.response', 'invalid_json', url.pathname);
          }
        });
      }
      return originalSend.apply(this, args);
    };
  }
})();
