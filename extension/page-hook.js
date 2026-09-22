import { CAPTURE_LIMITS, fitsUtf8 } from './capture/limits.mjs';
import { readBoundedJson, parseBoundedJson } from './capture/bounded-json.mjs';
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
  PROVISIONING_IDENTITY_RESET_TYPE,
  PROVISIONING_IDENTITY_VERSION,
} from './capture/envelopes.mjs';

(function installObservationHook() {
  const mode = globalThis.__OFCA_CAPTURE_MODE__;
  if (!['identity', 'preview', 'full'].includes(mode)) return;
  // Reinjecting the same observer must not detach listeners from sockets that
  // the site already opened. A real mode transition still tears everything down.
  if (globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode === mode) return;
  globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.stop?.();

  const MAX_PAYLOAD_WRAPPER_DEPTH = 3;
  const PAYLOAD_WRAPPER_KEYS = Object.freeze(['data', 'response', 'result']);
  const targetOrigin = window.location.origin;
  const xhrUrls = new WeakMap();
  const socketListeners = new Set();
  let active = true;
  let creatorPlatformUserId = null;
  let lastConfirmedCreatorId = null;
  let socketAccountGeneration = 0;
  let previewCreatorId = null;
  let previewGeneration = 0;
  const pendingPreview = [];
  let pageEpoch = crypto.randomUUID();
  let identityRequestSequence = 0;
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
    const sourceEventType = observation.source_path?.startsWith('/api2/')
      ? 'http.response' : 'websocket.message';
    if (observation.record?.text !== undefined
      && !fitsUtf8(observation.record.text, CAPTURE_LIMITS.textBytes)) {
      postDiagnostic(sourceEventType, 'capture_too_large', observation.source_path);
      return;
    }
    if (observation.record?.display_name !== undefined && observation.record.display_name !== null
      && !fitsUtf8(observation.record.display_name, CAPTURE_LIMITS.displayNameBytes)) {
      postDiagnostic(sourceEventType, 'capture_too_large', observation.source_path);
      return;
    }
    postPageMessage({
      type: CAPTURE_MESSAGE_TYPE,
      protocol_version: CAPTURE_PROTOCOL_VERSION,
      observation,
    });
  }

  function postPreview(observation, ownership) {
    if (observation === null) return;
    if (ownership.previewGeneration !== previewGeneration) return;
    if (previewCreatorId === null) {
      // Retain only bounded, reduced metadata while the page's own identity read completes.
      if (pendingPreview.length < 1000) pendingPreview.push(observation);
      return;
    }
    postPageMessage({
      type: PREVIEW_MESSAGE_TYPE,
      version: PREVIEW_PROTOCOL_VERSION,
      observation: { ...observation, creator_id: previewCreatorId },
    });
  }

  function postProvisioningIdentity(authenticatedProfile) {
    if (!['identity', 'full'].includes(mode)) return;
    postPageMessage({
      type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
      version: PROVISIONING_IDENTITY_VERSION,
      page_epoch: pageEpoch,
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

  function requestOwnership() {
    return Object.freeze({
      creatorPlatformUserId,
      pageEpoch,
      previewGeneration,
    });
  }

  function ownershipIsCurrent(ownership) {
    return ownership?.creatorPlatformUserId === creatorPlatformUserId
      && ownership?.pageEpoch === pageEpoch;
  }

  function replaceCreatorIdentity(nextId, updatePreview = true, confirmed = true) {
    const normalized = typeof nextId === 'string' && nextId.length <= 200 ? nextId : null;
    if (confirmed) {
      if (normalized === null || (lastConfirmedCreatorId !== null && normalized !== lastConfirmedCreatorId)) {
        socketAccountGeneration += 1;
      }
      lastConfirmedCreatorId = normalized;
    }
    if (updatePreview) {
      if (normalized === null || (previewCreatorId !== null && normalized !== previewCreatorId)) {
        previewGeneration += 1;
        pendingPreview.length = 0;
      }
      previewCreatorId = normalized;
      if (normalized !== null) {
        for (const observation of pendingPreview.splice(0)) postPreview(observation, { previewGeneration });
      }
    }
    if (normalized !== creatorPlatformUserId) {
      creatorPlatformUserId = normalized;
      pageEpoch = crypto.randomUUID();
    }
    if (!confirmed && ['identity', 'full'].includes(mode)) {
      // Unknown document state fences capture without asserting account sign-out.
      postPageMessage({ type: PROVISIONING_IDENTITY_RESET_TYPE,
        version: PROVISIONING_IDENTITY_VERSION, page_epoch: pageEpoch });
    } else if (confirmed) {
      postProvisioningIdentity(normalized === null ? null : { creator_account_id: normalized });
    }
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

  function updateCreatorIdentity(pathname, body, requestSequence) {
    if (requestSequence !== identityRequestSequence) return;
    const rawId = /^\/api2\/v2\/users\/me\/?$/.test(pathname)
      ? body?.id
      : body?.user?.id;
    const accountId = identifier(rawId);
    replaceCreatorIdentity(accountId, true, accountId !== null);
  }

  function emitRecords(resource, pathname, body, sourceEventType, ownership) {
    if (mode === 'identity') return;
    if (mode === 'full' && (!ownershipIsCurrent(ownership)
      || ownership.creatorPlatformUserId === null)) return;
    const extraction = resource === 'chats' ? chatRecords(body) : messageRecords(body);
    if (!extraction.recognized) {
      postDiagnostic(sourceEventType, 'unrecognized_payload', pathname);
      return;
    }
    const observedAt = new Date().toISOString();
    const routeChatId = resource === 'messages' ? contextChatId(pathname) : null;
    for (const rawRecord of extraction.records) {
      if (resource === 'chats') {
        postPreview(previewChatObservation(rawRecord, observedAt, previewCreatorId ?? 'pending'), ownership);
        if (mode !== 'full') continue;
        const record = normalizeChatRecord(rawRecord, observedAt);
        if (record === null) continue;
        postObservation({
          event_type: 'chat.observed',
          observed_at: observedAt,
          source_path: pathname,
          creator_platform_user_id: ownership.creatorPlatformUserId,
          context_chat_id: null,
          page_epoch: ownership.pageEpoch,
          record,
        });
        continue;
      }

      postPreview(previewMessageObservation(rawRecord, observedAt, previewCreatorId ?? 'pending', routeChatId), ownership);
      if (mode !== 'full') continue;
      const record = normalizeMessageRecord(rawRecord, { contextChatId: routeChatId });
      if (record === null) continue;
      postObservation({
        event_type: 'message.observed',
        observed_at: observedAt,
        source_path: pathname,
        creator_platform_user_id: ownership.creatorPlatformUserId,
        context_chat_id: record.chat_id,
        page_epoch: ownership.pageEpoch,
        record,
      });
    }
  }

  function handleResponseBody(url, body, sourceEventType, ownership, identitySequence = null) {
    if (!active) return;
    const resource = classifyPath(url.pathname);
    if (resource === 'identity') {
      updateCreatorIdentity(url.pathname, body, identitySequence);
      return;
    }
    if (resource === 'chats' || resource === 'messages') {
      emitRecords(resource, url.pathname, body, sourceEventType, ownership);
    }
  }

  async function observeFetchResponse(url, response, ownership, identitySequence) {
    try {
      if (response.ok === false) throw new Error('capture_http_error');
      handleResponseBody(
        url,
        await readBoundedJson(response.clone(), CAPTURE_LIMITS.responseBytes),
        'http.response',
        ownership,
        identitySequence,
      );
    } catch (_error) {
      if (identitySequence !== null && identitySequence === identityRequestSequence) {
        replaceCreatorIdentity(null, true, response.status === 401 || response.status === 403);
      }
      postDiagnostic('http.response', _error?.message === 'capture_response_too_large'
        ? 'capture_too_large' : 'invalid_json', url.pathname);
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

  function webSocketContextChatId(record, frame, creatorId) {
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
    if (creatorId === null) return null;
    if (senderId !== null && senderId !== creatorId) return senderId;
    return recipientId !== null && recipientId !== creatorId ? recipientId : null;
  }

  const originalWebSocket = window.WebSocket;
  if (mode !== 'identity' && typeof originalWebSocket === 'function') {
    installedWebSocket = new Proxy(originalWebSocket, {
      construct(target, argumentsList, newTarget) {
        const socket = Reflect.construct(target, argumentsList, newTarget);
        const url = resolveUrl(argumentsList[0]);
        if (url?.protocol === 'wss:' && url.hostname === 'ws2.onlyfans.com') {
          const socketGeneration = socketAccountGeneration;
          let socketCreatorId = creatorPlatformUserId;
          const listener = (event) => {
            if (!active || typeof event.data !== 'string'
              || socketGeneration !== socketAccountGeneration) return;
            const currentCreator = mode === 'preview' ? previewCreatorId : creatorPlatformUserId;
            if (currentCreator === null && mode === 'full') return;
            if (currentCreator !== null) {
              // A connection opened before the first identity may bind once.
              // A confirmed sign-out/switch permanently retires its generation.
              socketCreatorId ??= currentCreator;
              if (socketCreatorId !== currentCreator) return;
            }
            // SPA navigation fences frames until fresh identity evidence, but
            // does not retire a socket for the same verified creator. Each new
            // frame belongs to the current document epoch, unlike an old HTTP response.
            const ownership = requestOwnership();
            let frame;
            try {
              frame = parseBoundedJson(event.data, CAPTURE_LIMITS.responseBytes);
            } catch (_error) {
              postDiagnostic('websocket.message', _error?.message === 'capture_response_too_large'
                ? 'capture_too_large' : 'invalid_json', url.pathname);
              return;
            }
            const records = webSocketMessageRecords(frame);
            if (records.length === 0) return;
            const observedAt = new Date().toISOString();
            for (const rawRecord of records) {
              postPreview(previewMessageObservation(rawRecord, observedAt, previewCreatorId ?? 'pending',
                webSocketContextChatId(rawRecord, frame, previewCreatorId)), ownership);
              if (mode !== 'full' || ownership.creatorPlatformUserId === null) continue;
              const record = normalizeMessageRecord(rawRecord, {
                contextChatId: webSocketContextChatId(
                  rawRecord,
                  frame,
                  ownership.creatorPlatformUserId,
                ),
              });
              if (record === null) continue;
              postObservation({
                event_type: 'message.observed',
                observed_at: observedAt,
                source_path: url.pathname,
                creator_platform_user_id: ownership.creatorPlatformUserId,
                context_chat_id: record.chat_id,
                page_epoch: ownership.pageEpoch,
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
      const resource = url?.origin === targetOrigin ? classifyPath(url.pathname) : null;
      const ownership = requestOwnership();
      const identitySequence = resource === 'identity' ? ++identityRequestSequence : null;
      let response;
      try { response = await originalFetch.apply(this, args); }
      catch (error) {
        if (identitySequence !== null && identitySequence === identityRequestSequence) replaceCreatorIdentity(null, true, false);
        throw error;
      }
      if (
        active
        && resource !== null
        && (mode !== 'identity' || resource === 'identity')
      ) void observeFetchResponse(url, response, ownership, identitySequence);
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
    const resource = url?.origin === targetOrigin ? classifyPath(url.pathname) : null;
    if (
      active
      && resource !== null
      && (mode !== 'identity' || resource === 'identity')
    ) {
      const ownership = requestOwnership();
      const identitySequence = resource === 'identity' ? ++identityRequestSequence : null;
      this.addEventListener('loadend', () => {
        if (!active) return;
        try {
          if (this.status < 200 || this.status >= 300) throw new Error('capture_http_error');
          const body = parseBoundedJson(this.responseType === 'json'
            ? JSON.stringify(this.response) : this.responseText, CAPTURE_LIMITS.responseBytes);
          handleResponseBody(url, body, 'http.response', ownership, identitySequence);
        } catch (_error) {
          if (identitySequence !== null && identitySequence === identityRequestSequence) {
            replaceCreatorIdentity(null, true, this.status === 401 || this.status === 403);
          }
          postDiagnostic('http.response', _error?.message === 'capture_response_too_large'
        ? 'capture_too_large' : 'invalid_json', url.pathname);
        }
      }, { once: true });
    }
    return originalXhrSend.apply(this, args);
  };
  XMLHttpRequest.prototype.open = installedXhrOpen;
  XMLHttpRequest.prototype.send = installedXhrSend;

  const navigationChanged = () => {
    identityRequestSequence += 1;
    pageEpoch = crypto.randomUUID();
    replaceCreatorIdentity(null, false, false);
  };
  const historyWrappers = [];
  for (const method of ['pushState', 'replaceState']) {
    const original = window.history?.[method];
    if (typeof original !== 'function') continue;
    const wrapped = function (...args) {
      const previous = window.location.href;
      const result = original.apply(this, args);
      if (window.location.href !== previous) navigationChanged();
      return result;
    };
    window.history[method] = wrapped;
    historyWrappers.push({ method, original, wrapped });
  }
  window.addEventListener('popstate', navigationChanged);
  window.addEventListener('hashchange', navigationChanged);

  function stop() {
    if (!active) return;
    active = false;
    pendingPreview.length = 0;
    previewCreatorId = null;
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
    window.removeEventListener('popstate', navigationChanged);
    window.removeEventListener('hashchange', navigationChanged);
    for (const { method, original, wrapped } of historyWrappers) {
      if (window.history[method] === wrapped) window.history[method] = original;
    }
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
      || !['stop', 'refresh_identity'].includes(message.action)
    ) return;
    if (message.action === 'stop') stop();
    // A readiness probe before the profile response is not evidence of sign-out.
    // Navigation already removes the worker's old document context.
    else if (creatorPlatformUserId !== null) {
      postProvisioningIdentity({ creator_account_id: creatorPlatformUserId });
    }
  }

  window.addEventListener('message', controlListener);
  globalThis.__OFCA_PAGE_HOOK_CONTROLLER__ = Object.freeze({ mode, stop });
})();
