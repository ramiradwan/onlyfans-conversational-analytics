import {
  identifier,
  normalizeChatRecord,
  normalizeMessageRecord,
  previewChatObservation,
  previewMessageObservation,
} from './capture/normalization.mjs';
import {
  CAPTURE_MESSAGE_TYPE,
  CAPTURE_PROTOCOL_VERSION,
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  PREVIEW_MESSAGE_TYPE,
  PREVIEW_PROTOCOL_VERSION,
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_VERSION,
} from './capture/envelopes.mjs';

(function installObservationHook() {
  const mode = globalThis.__OFCA_CAPTURE_MODE__;
  if (!['identity', 'preview', 'full'].includes(mode)) return;
  globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.stop?.();

  const MAX_PAYLOAD_WRAPPER_DEPTH = 3;
  const PAYLOAD_WRAPPER_KEYS = Object.freeze(['data', 'response', 'result']);
  const targetOrigin = window.location.origin;
  const xhrUrls = new WeakMap();
  const socketListeners = new Set();
  let active = true;
  let creatorPlatformUserId = null;
  let installedFetch = null;
  let installedWebSocket = null;
  let installedXhrOpen = null;
  let installedXhrSend = null;

  function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  function postPageMessage(message) {
    if (!active) return;
    try {
      window.postMessage(message, targetOrigin);
    } catch (_error) {
      // A non-cloneable value is dropped without logging its contents.
    }
  }

  function postObservation(observation) {
    postPageMessage({
      type: CAPTURE_MESSAGE_TYPE,
      protocol_version: CAPTURE_PROTOCOL_VERSION,
      observation,
    });
  }

  function postPreview(observation) {
    if (observation === null) return;
    postPageMessage({
      type: PREVIEW_MESSAGE_TYPE,
      version: PREVIEW_PROTOCOL_VERSION,
      observation,
    });
  }

  function postProvisioningIdentity(authenticatedProfile) {
    if (!['identity', 'full'].includes(mode)) return;
    postPageMessage({
      type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
      version: PROVISIONING_IDENTITY_VERSION,
      authenticated_profile: authenticatedProfile,
    });
  }

  function postDiagnostic(sourceEventType, code, sourcePath) {
    if (mode !== 'full') return;
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
        return { recognized: payload.length === 0 || records.length > 0, records };
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
      isRecord(payload) && (isRecord(payload.withUser) || isRecord(payload.with_user))
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
      if (isRecord(payload.message)) return { recognized: true, records: [payload.message] };
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
      return identifier(decodeURIComponent(match[1]));
    } catch (_error) {
      return identifier(match[1]);
    }
  }

  function updateCreatorIdentity(pathname, body) {
    const rawId = /^\/api2\/v2\/users\/me\/?$/.test(pathname)
      ? body?.id
      : body?.user?.id;
    const detected = identifier(rawId);
    if (detected !== null) creatorPlatformUserId = detected;
    postProvisioningIdentity(
      detected !== null && detected.length <= 200
        ? { creator_account_id: detected }
        : null,
    );
  }

  function emitRecords(resource, pathname, body, sourceEventType) {
    if (mode === 'identity') return;
    const extraction = resource === 'chats' ? chatRecords(body) : messageRecords(body);
    if (!extraction.recognized) {
      postDiagnostic(sourceEventType, 'unrecognized_payload', pathname);
      return;
    }
    const observedAt = new Date().toISOString();
    const routeChatId = resource === 'messages' ? contextChatId(pathname) : null;
    for (const rawRecord of extraction.records) {
      if (resource === 'chats') {
        postPreview(previewChatObservation(observedAt));
        if (mode !== 'full') continue;
        const record = normalizeChatRecord(rawRecord, observedAt);
        if (record === null) continue;
        postObservation({
          event_type: 'chat.observed',
          observed_at: observedAt,
          source_path: pathname,
          creator_platform_user_id: creatorPlatformUserId,
          context_chat_id: null,
          record,
        });
        continue;
      }

      postPreview(previewMessageObservation(rawRecord, creatorPlatformUserId, observedAt));
      if (mode !== 'full') continue;
      const record = normalizeMessageRecord(rawRecord, {
        contextChatId: routeChatId,
        creatorPlatformUserId,
      });
      if (record === null) continue;
      postObservation({
        event_type: 'message.observed',
        observed_at: observedAt,
        source_path: pathname,
        creator_platform_user_id: creatorPlatformUserId,
        context_chat_id: record.chat_id,
        record,
      });
    }
  }

  function handleResponseBody(url, body, sourceEventType) {
    if (!active) return;
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
      record.chat_id ?? record.chatId ?? record.chat?.id ?? frame.chat_id ?? frame.chatId,
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
    if (creatorPlatformUserId === null) return null;
    if (senderId !== null && senderId !== creatorPlatformUserId) return senderId;
    return recipientId !== null && recipientId !== creatorPlatformUserId ? recipientId : null;
  }

  const originalWebSocket = window.WebSocket;
  if (mode !== 'identity' && typeof originalWebSocket === 'function') {
    installedWebSocket = new Proxy(originalWebSocket, {
      construct(target, argumentsList, newTarget) {
        const socket = Reflect.construct(target, argumentsList, newTarget);
        const url = resolveUrl(argumentsList[0]);
        if (url?.protocol === 'wss:' && url.hostname === 'ws2.onlyfans.com') {
          const listener = (event) => {
            if (!active || typeof event.data !== 'string') return;
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
            for (const rawRecord of records) {
              postPreview(previewMessageObservation(
                rawRecord,
                creatorPlatformUserId,
                observedAt,
              ));
              if (mode !== 'full') continue;
              const record = normalizeMessageRecord(rawRecord, {
                contextChatId: webSocketContextChatId(rawRecord, frame),
                creatorPlatformUserId,
              });
              if (record === null) continue;
              postObservation({
                event_type: 'message.observed',
                observed_at: observedAt,
                source_path: url.pathname,
                creator_platform_user_id: creatorPlatformUserId,
                context_chat_id: record.chat_id,
                record,
              });
            }
          };
          socket.addEventListener('message', listener);
          socketListeners.add({ socket, listener });
        }
        return socket;
      },
    });
    window.WebSocket = installedWebSocket;
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    installedFetch = async function observedFetch(...args) {
      const url = resolveUrl(args[0]);
      const response = await originalFetch.apply(this, args);
      if (
        active
        && url?.origin === targetOrigin
        && classifyPath(url.pathname) !== null
        && (mode !== 'identity' || classifyPath(url.pathname) === 'identity')
      ) void observeFetchResponse(url, response);
      return response;
    };
    window.fetch = installedFetch;
  }

  const originalXhrOpen = XMLHttpRequest.prototype.open;
  const originalXhrSend = XMLHttpRequest.prototype.send;
  installedXhrOpen = function observedOpen(_method, rawUrl, ...rest) {
    xhrUrls.set(this, resolveUrl(rawUrl));
    return originalXhrOpen.call(this, _method, rawUrl, ...rest);
  };
  installedXhrSend = function observedSend(...args) {
    const url = xhrUrls.get(this);
    if (
      active
      && url?.origin === targetOrigin
      && classifyPath(url.pathname) !== null
      && (mode !== 'identity' || classifyPath(url.pathname) === 'identity')
    ) {
      this.addEventListener('load', () => {
        if (!active) return;
        try {
          const body = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
          handleResponseBody(url, body, 'http.response');
        } catch (_error) {
          postDiagnostic('http.response', 'invalid_json', url.pathname);
        }
      }, { once: true });
    }
    return originalXhrSend.apply(this, args);
  };
  XMLHttpRequest.prototype.open = installedXhrOpen;
  XMLHttpRequest.prototype.send = installedXhrSend;

  function stop() {
    if (!active) return;
    active = false;
    if (installedFetch !== null && window.fetch === installedFetch) window.fetch = originalFetch;
    if (installedWebSocket !== null && window.WebSocket === installedWebSocket) {
      window.WebSocket = originalWebSocket;
    }
    if (XMLHttpRequest.prototype.open === installedXhrOpen) {
      XMLHttpRequest.prototype.open = originalXhrOpen;
    }
    if (XMLHttpRequest.prototype.send === installedXhrSend) {
      XMLHttpRequest.prototype.send = originalXhrSend;
    }
    for (const { socket, listener } of socketListeners) {
      socket.removeEventListener?.('message', listener);
    }
    socketListeners.clear();
    window.removeEventListener('message', controlListener);
    delete globalThis.__OFCA_CAPTURE_MODE__;
    delete globalThis.__OFCA_PAGE_HOOK_CONTROLLER__;
  }

  function controlListener(event) {
    if (event.source !== window || event.origin !== targetOrigin) return;
    const message = event.data;
    if (
      !isRecord(message)
      || Object.keys(message).length !== 3
      || message.type !== PAGE_CONTROL_MESSAGE_TYPE
      || message.version !== PAGE_CONTROL_VERSION
      || message.action !== 'stop'
    ) return;
    stop();
  }

  window.addEventListener('message', controlListener);
  globalThis.__OFCA_PAGE_HOOK_CONTROLLER__ = Object.freeze({ mode, stop });
})();
