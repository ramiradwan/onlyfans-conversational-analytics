import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  previewChatObservation,
  previewMessageObservation,
} from '../capture/normalization.mjs';
import {
  CAPTURE_MESSAGE_TYPE,
  CAPTURE_PROTOCOL_VERSION,
  CaptureDiagnostics,
  CaptureIngestionService,
  createCaptureMessageBridge,
  mapPlatformObservation,
} from '../transport/capture-ingestion.mjs';

/** Argument count of every call to `name`, one entry per call site. */
function callArgumentCounts(source, name) {
  const counts = [];
  const opening = `${name}(`;
  for (
    let index = source.indexOf(opening);
    index !== -1;
    index = source.indexOf(opening, index + 1)
  ) {
    let depth = 0;
    let separators = 0;
    let cursor = index + opening.length - 1;
    for (; cursor < source.length; cursor += 1) {
      const character = source[cursor];
      if ('([{'.includes(character)) depth += 1;
      else if (')]}'.includes(character)) {
        depth -= 1;
        if (depth === 0) break;
      } else if (character === ',' && depth === 1) separators += 1;
    }
    const args = source.slice(index + opening.length, cursor).trim();
    if (args.length === 0) counts.push(0);
    else counts.push(args.endsWith(',') ? separators : separators + 1);
  }
  return counts;
}

const CHAT_OBSERVATION = Object.freeze({
  event_type: 'chat.observed',
  observed_at: '2030-01-08T12:00:00Z',
  source_path: '/api2/v2/chats',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: null,
  record: {
    chat_id: 'chat-synthetic',
    platform_user_id: 'fan-synthetic',
    display_name: 'Synthetic Fan',
    updated_at: '2030-01-08T11:59:00Z',
  },
});

const MESSAGE_OBSERVATION = Object.freeze({
  event_type: 'message.observed',
  observed_at: '2030-01-08T12:01:00Z',
  source_path: '/api2/v2/chats/chat-synthetic/messages',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: 'chat-synthetic',
  record: {
    id: 'message-synthetic',
    fromUser: { id: 'creator-synthetic' },
    text: 'Synthetic message',
    createdAt: '2030-01-08T12:00:30Z',
  },
});

function runtimeMessage(observation) {
  return {
    type: CAPTURE_MESSAGE_TYPE,
    protocol_version: CAPTURE_PROTOCOL_VERSION,
    observation,
  };
}

test('manifest keeps OnlyFans access optional and installs no static content scripts', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.host_permissions, undefined);
  assert.deepEqual(manifest.optional_host_permissions, [
    'https://onlyfans.com/*',
    'http://bridge.localhost:17871/*',
  ]);
  assert.equal(manifest.permissions.includes('tabs'), false);
  assert.equal(manifest.permissions.includes('cookies'), false);
});

test('capture sources contain observation channels but no platform mutation channels', async () => {
  const pageHook = await readFile(new URL('../page-hook.js', import.meta.url), 'utf8');
  const contentBridge = await readFile(new URL('../content.js', import.meta.url), 'utf8');
  for (const mutationChannel of ['_OF_BACKEND_', 'send_ws_message', 'send_fetch_command']) {
    assert.equal(pageHook.includes(mutationChannel), false);
    assert.equal(contentBridge.includes(mutationChannel), false);
  }
  assert.match(pageHook, /observedFetch/);
  assert.match(contentBridge, /isPreviewEnvelope/);
});

test('standalone preview observations contain counts-only fields', () => {
  const message = previewMessageObservation({
    id: 'message-synthetic',
    text: 'Synthetic content that must not cross the preview boundary',
    fromUser: { id: 'fan-synthetic' },
    chatUserId: 'fan-synthetic',
  }, '2030-01-08T12:00:00Z');
  const chat = previewChatObservation('2030-01-08T12:00:00Z');
  assert.deepEqual(message, {
    kind: 'message',
    observed_at: '2030-01-08T12:00:00.000Z',
    direction: 'inbound',
  });
  assert.deepEqual(chat, {
    kind: 'chat',
    observed_at: '2030-01-08T12:00:00.000Z',
  });
  assert.equal(JSON.stringify({ message, chat }).includes('Synthetic content'), false);
});

test('page hook calls preview normalizers with their declared parameter list', async () => {
  const pageHook = await readFile(new URL('../page-hook.js', import.meta.url), 'utf8');
  for (const normalizer of [previewChatObservation, previewMessageObservation]) {
    const counts = callArgumentCounts(pageHook, normalizer.name);
    assert.equal(counts.length > 0, true, `${normalizer.name} is unreachable from the page hook`);
    // An extra positional argument shifts the timestamp out of its parameter,
    // and the reduced observation is then null for every record ever observed.
    for (const count of counts) assert.equal(count, normalizer.length, normalizer.name);
  }
});

test('full chat observations map to canonical ingestion changes', () => {
  assert.deepEqual(mapPlatformObservation(CHAT_OBSERVATION), {
    ok: true,
    eventType: 'chat.observed',
    resource: 'chats',
    sourcePath: '/api2/v2/chats',
    change: {
      type: 'chat.upsert',
      chat: {
        chat_id: 'chat-synthetic',
        record_kind: 'full',
        platform_user_id: 'fan-synthetic',
        display_name: 'Synthetic Fan',
        updated_at: '2030-01-08T11:59:00.000Z',
      },
    },
  });
});

test('wrapper aliases are normalized and malformed observations remain payload-free drops', async () => {
  const wrapped = mapPlatformObservation({
    ...CHAT_OBSERVATION,
    record: { with_user: { id: 'fan-wrapper', username: 'Wrapper Fan' } },
  });
  assert.equal(wrapped.ok, true);
  assert.equal(wrapped.change.chat.chat_id, 'fan-wrapper');

  const reports = [];
  const ingestion = new CaptureIngestionService({
    diagnostics: new CaptureDiagnostics((diagnostic) => reports.push(diagnostic)),
    runtime: { async wake() { throw new Error('malformed data must not wake the runtime'); } },
  });
  assert.deepEqual(
    await ingestion.ingest({ ...MESSAGE_OBSERVATION, record: { text: 'private marker' } }),
    { ok: false, code: 'invalid_message', retryable: false },
  );
  assert.deepEqual(
    await ingestion.ingest({ event_type: 'unrecognized.synthetic' }),
    { ok: false, code: 'unrecognized_event', retryable: false },
  );
  assert.deepEqual(reports, [
    { reason: 'invalid_message', event_type: 'message.observed', count: 1 },
    { reason: 'unrecognized_event', event_type: 'unknown', count: 1 },
  ]);
  assert.equal(JSON.stringify(reports).includes('private marker'), false);
});

test('message capture creates one placeholder parent and validates capture policy before durable enqueue', async () => {
  const calls = [];
  let enabled = true;
  const runtime = {
    configuration: {
      activeDocument: {
        capture_policy: {
          rules: [{
            resource: 'messages',
            url_pattern: '/api2/v2/chats/*/messages',
            enabled: true,
          }],
        },
      },
    },
    async wake() {
      return {
        async captureDelta() { throw new Error('message must use dependency-closed capture'); },
        async captureMessageWithParent(message, parent) {
          calls.push({ message, parent });
          return { source_seq: 4 };
        },
      };
    },
  };
  const ingestion = new CaptureIngestionService({ runtime });
  assert.deepEqual(await ingestion.ingest(MESSAGE_OBSERVATION), {
    ok: true,
    event_type: 'message.observed',
    source_seq: 4,
    material_transition: true,
  });
  assert.deepEqual(calls[0].parent, {
    type: 'chat.upsert',
    chat: {
      chat_id: 'chat-synthetic',
      record_kind: 'placeholder',
      platform_user_id: null,
      display_name: null,
      updated_at: null,
    },
  });

  enabled = false;
  runtime.configuration.activeDocument.capture_policy.rules[0].enabled = enabled;
  assert.deepEqual(await ingestion.ingest(MESSAGE_OBSERVATION), {
    ok: false,
    code: 'capture_disabled',
    retryable: false,
  });
  assert.equal(calls.length, 1);
});

test('content bridge rejects foreign senders before durable ingestion', () => {
  const listeners = [];
  const diagnostics = new CaptureDiagnostics();
  const ingestion = new CaptureIngestionService({
    diagnostics,
    runtime: { async wake() { throw new Error('foreign messages must not wake the runtime'); } },
  });
  const bridge = createCaptureMessageBridge({
    ingestion,
    chromeApi: {
      runtime: {
        id: 'synthetic-extension-id',
        onMessage: {
          addListener(listener) { listeners.push(listener); },
          removeListener() {},
        },
      },
    },
  });
  bridge.register();
  assert.equal(listeners.length, 1);
  assert.equal(listeners[0](runtimeMessage(CHAT_OBSERVATION), {
    id: 'synthetic-extension-id',
    frameId: 0,
    url: 'https://example.test/',
  }, () => {}), false);
  assert.deepEqual(diagnostics.snapshot(), {});
});
