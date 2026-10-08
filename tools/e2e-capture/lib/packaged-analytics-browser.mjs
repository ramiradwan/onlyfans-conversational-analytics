import { chromium } from '@playwright/test';
import { createInterface } from 'node:readline';
import {
  chats, seedMessage, appendedMessage, editedMessages, historicalMessages, interleavedMessage,
} from './packaged-analytics-fixture.mjs';

let context, bridge, platform, worker, popup, input;
let messages = new Map();
let selectedPage = [];

function csrfRequest({ route, body }) {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  if (!csrf) throw new Error('authenticated_browser_session_unavailable');
  return fetch(route, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(body) }).then(async response => ({ status: response.status, body: await response.json() }));
}

async function start(config) {
  input = config;
  messages = new Map(Array.from({ length: input.seeded_size ?? 0 }, (_, index) => {
    const value = seedMessage(index, input.seeded_size, input);
    return [value.id, value];
  }));
  const origin = new URL(input.bridge_origin);
  if (!['127.0.0.1', 'localhost'].includes(origin.hostname) && !origin.hostname.endsWith('.localhost')) {
    throw new Error('packaged_bridge_must_be_loopback');
  }
  context = await chromium.launchPersistentContext(input.browser_profile, {
    ...(input.browser_executable ? { executablePath: input.browser_executable } : {}),
    headless: false, viewport: { width: 1280, height: 800 }, serviceWorkers: 'allow', timezoneId: 'UTC',
    args: [`--disable-extensions-except=${input.agent_directory}`, `--load-extension=${input.agent_directory}`,
      `--host-resolver-rules=MAP ${origin.hostname} 127.0.0.1`, '--disable-background-networking',
      '--disable-component-update', '--no-first-run'],
  });
  worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  if (!worker.url().startsWith('chrome-extension://')) throw new Error('packaged_agent_unavailable');
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin === input.bridge_origin || url.protocol === 'chrome-extension:') return route.continue();
    if (url.origin !== input.platform_origin || route.request().method() !== 'GET') return route.abort();
    const respond = body => route.fulfill({ status: 200, contentType: 'application/json',
      headers: { 'cache-control': 'no-store' }, body: JSON.stringify(body) });
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html',
      body: '<!doctype html><title>Synthetic conversations</title><main>Synthetic conversations</main>' });
    if (url.pathname === input.identity_path) return respond({ id: input.synthetic_account_id });
    if (url.pathname === input.conversations_path) return respond({ list: chats.map(id => ({ id,
      withUser: { id }, updatedAt: input.evaluation_clock })), hasMore: false });
    if (url.pathname.startsWith(input.messages_path_prefix)) {
      return respond({ list: selectedPage, hasMore: false });
    }
    return route.abort();
  });
  bridge = await context.newPage();
  const bridgeUrl = new URL(input.bridge_path ?? '/analytics', input.bridge_origin).href;
  const deadline = Date.now() + 120000;
  while (true) {
    try {
      await bridge.goto(bridgeUrl, { waitUntil: 'domcontentloaded', timeout: 10000 });
      break;
    } catch {
      if (Date.now() >= deadline) throw new Error('packaged_bridge_readiness_timeout');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  }
  const authority = await bridge.evaluate(async () => {
    const response = await fetch('/api/v1/settings/creator-vault', { credentials: 'same-origin', cache: 'no-store' });
    return { status: response.status, body: await response.json() };
  });
  if (authority.status !== 200 || authority.body.creator_account_id !== input.synthetic_account_id) {
    throw new Error('dedicated_provisioned_creator_session_unavailable');
  }
  platform = await context.newPage();
  await platform.goto(input.platform_origin, { waitUntil: 'domcontentloaded' });
  await read(input.identity_path);
  popup = await context.newPage();
  await popup.goto(new URL('popup.html', worker.url()).href);
  const status = await popup.evaluate(() => chrome.runtime.sendMessage({ type: 'ofca.ui.status' }));
  if (status?.ok !== true || status.status?.consent?.mode !== 'full') {
    throw new Error('packaged_full_consent_unavailable');
  }
  if (!input.seeded_size) await read(input.conversations_path);
  return { authenticated: true, agent_extension_id: new URL(worker.url()).hostname,
    node_version: process.version, browser_version: context.browser()?.version() ?? null };
}

async function read(path) {
  return platform.evaluate(async route => {
    const response = await fetch(route, { credentials: 'include', cache: 'no-store' });
    await response.arrayBuffer();
    if (!response.ok) throw new Error('synthetic_upstream_read_failed');
  }, path);
}

async function capture(values) {
  for (const chat of new Set(values.map(value => value.chat_id))) {
    const rows = values.filter(value => value.chat_id === chat);
    for (let offset = 0; offset < rows.length; offset += 100) {
      selectedPage = rows.slice(offset, offset + 100);
      await read(`${input.messages_path_prefix}${encodeURIComponent(chat)}/messages`);
    }
  }
  for (const value of values) messages.set(value.id, value);
}

async function command(request) {
  const { action } = request;
  if (action === 'start') return start(request.inputs);
  if (action === 'seed') {
    await capture(Array.from({ length: Math.min(request.count, request.size - request.offset) },
      (_, index) => seedMessage(request.offset + index, request.size, input)));
    return { generated_messages: messages.size };
  }
  if (action === 'append') {
    const value = appendedMessage(request.message_id, request.chat, input);
    await capture([value]);
    return { generated_messages: messages.size };
  }
  if (action === 'edit') {
    const values = editedMessages(messages);
    await capture(values);
    return { edited: values.length };
  }
  if (action === 'delete') {
    for (let index = 100; index < 200; index++) {
      const id = `matrix-input-${index}`;
      const response = await bridge.evaluate(csrfRequest, { route: '/api/v1/settings/creator-vault/commands',
        body: { action: 'delete_message', target_id: id } });
      if (response.status !== 200 || !Number.isSafeInteger(response.body.deletion_revision)) {
        throw new Error('authenticated_creator_deletion_failed');
      }
      messages.delete(id);
    }
    return { deleted: 100 };
  }
  if (action === 'history_batch') {
    const values = historicalMessages(request.batch, input);
    await capture(values);
    await capture([interleavedMessage(request.batch, input)]);
    return { generated_messages: messages.size };
  }
  if (action === 'question') return bridge.evaluate(csrfRequest,
    { route: '/api/v1/insights/questions', body: request.plan });
  if (action === 'capture_control') return bridge.evaluate(csrfRequest,
    { route: '/api/v1/companion/browser/capture', body: { action: request.control } });
  if (action === 'capture_status') {
    const result = await popup.evaluate(() => chrome.runtime.sendMessage({ type: 'ofca.ui.status' }));
    return { mode: result?.status?.consent?.mode, pending_entries: result?.status?.delivery?.pending_entries };
  }
  if (action === 'rebuild') return bridge.evaluate(csrfRequest,
    { route: '/api/v1/insights/rebuild', body: {} });
  if (action === 'ui') {
    await bridge.getByRole('tab', { name: 'Conversation questions', exact: true }).click();
    const end = new Date(input.evaluation_clock);
    const start = new Date(end.getTime() - 48 * 3600000);
    await bridge.getByLabel('Start date', { exact: true }).fill(start.toISOString().slice(0, 10));
    await bridge.getByLabel('End date', { exact: true }).fill(end.toISOString().slice(0, 10));
    const response = bridge.waitForResponse(value => value.url() === `${input.bridge_origin}/api/v1/insights/questions`
      && value.request().method() === 'POST');
    await bridge.getByRole('button', { name: 'Run question', exact: true }).click();
    const result = await response;
    const body = await result.json();
    if (result.status() !== 200) return { status: result.status(), body, visible: false };
    await bridge.locator('[data-journey-state="questions.undetermined"]').waitFor({ state: 'visible' });
    const guidance = await bridge.locator('[data-journey-state="questions.undetermined"]').innerText();
    return { status: result.status(), body,
      visible: await bridge.locator('[data-journey-state="questions.results"]').isVisible(),
      source_links: await bridge.getByRole('button', { name: /^View source / }).count(),
      positive_rows: await bridge.getByRole('table', { name: 'Conversation question results' }).count(),
      uncertainty_visible: await bridge.locator('[data-journey-state="questions.undetermined"]').isVisible(),
      uncertainty_guidance_present: guidance.trim().length > 0 };
  }
  if (action === 'close') {
    await context?.close();
    return { browser_closed: true };
  }
  throw new Error('unsupported_packaged_browser_action');
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let request;
  try {
    request = JSON.parse(line);
    const value = await command(request);
    process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, value })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: request?.id, ok: false, error: error.name })}\n`);
  }
}
await context?.close();
