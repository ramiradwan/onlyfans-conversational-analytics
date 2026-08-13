import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { BRAIN_ORIGIN, BRAIN_PORT } from './brain.mjs';
import { pythonExecutable } from './paths.mjs';

const CONFIG_PATTERN = /<script[^>]*id=["']fastapi-config["'][^>]*>([\s\S]*?)<\/script>/i;
const CSRF_PATTERN = /<meta[^>]*name=["']csrf-token["'][^>]*content=["']([^"']+)["'][^>]*>/i;

function requestBrain(pathname, { headers = {}, method = 'GET' } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      headers: {
        Host: new URL(BRAIN_ORIGIN).host,
        ...headers,
      },
      host: '127.0.0.1',
      method,
      path: pathname,
      port: BRAIN_PORT,
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({
        body: Buffer.concat(chunks).toString('utf8'),
        status: response.statusCode ?? 0,
      }));
    });
    request.on('error', reject);
    request.end();
  });
}

const E2E_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const GRANT_SEEDER = path.join(E2E_ROOT, 'helpers', 'seed_webauthn_grants.py');

async function browserSessionCookieHeader(context) {
  const cookies = await context.cookies(BRAIN_ORIGIN);
  const header = cookies.map(({ name, value }) => `${name}=${value}`).join('; ');
  if (!header) throw new Error('Browser context does not have a local Bridge session cookie.');
  return header;
}

function runGrantSeeder(authDatabasePath, { extraCurrentBinding = false } = {}) {
  return new Promise((resolve, reject) => {
    const argumentsList = [
      GRANT_SEEDER,
      '--auth-database', authDatabasePath,
      ...(extraCurrentBinding ? ['--extra-current-binding'] : []),
    ];
    const child = spawn(pythonExecutable(), argumentsList, {
      env: {
        ...process.env,
        AUTH_DATABASE_PATH: authDatabasePath,
        LOCAL_SESSION_BOOTSTRAP_TOKEN: 'e2e-grant-seeder-bootstrap-token-000000',
        LOCAL_PRINCIPAL_ID: 'e2e-local-principal',
        LOCAL_CREATOR_ACCOUNT_ID: 'dev-creator-account',
        LOCAL_PLATFORM_CREATOR_ID: 'e2e-local-platform-creator',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on('data', (chunk) => stdout.push(String(chunk)));
    child.stderr.on('data', (chunk) => stderr.push(String(chunk)));
    child.once('error', reject);
    child.once('exit', (code) => {
      if (code !== 0) {
        reject(new Error(`Verified-grant seeding failed (${code}): ${stderr.join('').trim()}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout.join('')));
      } catch {
        reject(new Error('Verified-grant seeding returned malformed output.'));
      }
    });
  });
}

async function waitForAuthentication(page) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    const cookie = await page.context().cookies(BRAIN_ORIGIN);
    if (cookie.length > 0) return;
    const error = page.getByRole('alert');
    if (await error.count()) {
      throw new Error(`WebAuthn authentication failed: ${await error.textContent()}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('WebAuthn authentication did not issue a Bridge session cookie.');
}

export async function establishBrowserWebAuthnSession(page, authDatabasePath) {
  const extraCurrentBinding = process.env.OFCA_E2E_FALSIFY_AMBIGUOUS_BINDING === '1';
  const seeded = await runGrantSeeder(authDatabasePath, { extraCurrentBinding });
  if (seeded.grant_reference_ids?.length !== (extraCurrentBinding ? 4 : 3)) {
    throw new Error('Verified-grant seeding returned an unexpected authority reference count.');
  }
  const session = await page.context().newCDPSession(page);
  await session.send('WebAuthn.enable');
  await session.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  await page.goto(`${BRAIN_ORIGIN}/`, { waitUntil: 'domcontentloaded' });
  await page.bringToFront();
  await page.locator('body').focus();
  const result = await page.evaluate(async () => {
    const decode = (value) => {
      const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
      const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=');
      const binary = atob(padded);
      return Uint8Array.from(binary, (character) => character.charCodeAt(0)).buffer;
    };
    const encode = (value) => {
      let binary = '';
      new Uint8Array(value).forEach((byte) => { binary += String.fromCharCode(byte); });
      return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    };
    const post = async (path, body) => {
      const response = await fetch(`/api/v1/webauthn${path}`, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
        method: 'POST',
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.ok) return { body: null, status: response.status };
      return { body: await response.json(), status: response.status };
    };
    const registration = await post('/registration/begin');
    if (registration.status !== 200) return { stage: 'registration_begin', status: registration.status };
    const registrationCredential = await navigator.credentials.create({
      publicKey: {
        ...registration.body,
        challenge: decode(registration.body.challenge),
        user: { ...registration.body.user, id: decode(registration.body.user.id) },
      },
    });
    if (registrationCredential === null) return { stage: 'registration_credential', status: 0 };
    const registrationResponse = registrationCredential.response;
    const registrationFinish = await post('/registration/finish', {
      id: registrationCredential.id,
      rawId: encode(registrationCredential.rawId),
      type: 'public-key',
      response: {
        clientDataJSON: encode(registrationResponse.clientDataJSON),
        attestationObject: encode(registrationResponse.attestationObject),
      },
    });
    if (registrationFinish.status !== 200) return { stage: 'registration_finish', status: registrationFinish.status };
    const login = await post('/login/begin');
    if (login.status !== 200) return { stage: 'login_begin', status: login.status };
    const loginCredential = await navigator.credentials.get({
      publicKey: {
        ...login.body,
        challenge: decode(login.body.challenge),
        allowCredentials: login.body.allowCredentials.map((allowed) => ({
          ...allowed,
          id: decode(allowed.id),
        })),
      },
    });
    if (loginCredential === null) return { stage: 'login_credential', status: 0 };
    const loginResponse = loginCredential.response;
    const loginFinish = await post('/login/finish', {
      id: loginCredential.id,
      rawId: encode(loginCredential.rawId),
      type: 'public-key',
      response: {
        clientDataJSON: encode(loginResponse.clientDataJSON),
        authenticatorData: encode(loginResponse.authenticatorData),
        signature: encode(loginResponse.signature),
        userHandle: loginResponse.userHandle === null ? null : encode(loginResponse.userHandle),
      },
    });
    return { stage: 'login_finish', status: loginFinish.status };
  });
  if (result.status !== 200) {
    throw new Error(`WebAuthn authentication failed at ${result.stage} (${result.status}).`);
  }
  await waitForAuthentication(page);
  await browserSessionCookieHeader(page.context());
}

async function servedBootstrap(context) {
  const cookie = await browserSessionCookieHeader(context);
  const response = await requestBrain('/', {
    headers: {
      Accept: 'text/html',
      Cookie: cookie,
    },
  });
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Brain runtime bootstrap failed (${response.status}).`);
  }
  const html = response.body;
  const configMatch = CONFIG_PATTERN.exec(html);
  const csrfMatch = CSRF_PATTERN.exec(html);
  if (configMatch === null || csrfMatch === null) {
    throw new Error('Brain did not serve runtime configuration and CSRF metadata.');
  }
  let config;
  try {
    config = JSON.parse(configMatch[1]);
  } catch {
    throw new Error('Brain served malformed runtime configuration.');
  }
  if (
    typeof config !== 'object'
    || config === null
    || config.CREATOR_ID === undefined
    || typeof config.EXTENSION_ID !== 'string'
    || !/^[a-p]{32}$/.test(config.EXTENSION_ID)
    || typeof config.BRIDGE_AUTH_TICKET !== 'string'
    || config.BRIDGE_AUTH_TICKET.length === 0
    || typeof config.FASTAPI_WS_URL !== 'string'
    || config.FASTAPI_WS_URL.length === 0
    || config.API_BASE_URL !== BRAIN_ORIGIN
    || config.FASTAPI_WS_URL !== BRAIN_ORIGIN.replace('http://', 'ws://') + '/ws/bridge'
  ) {
    throw new Error('Brain runtime configuration is incomplete.');
  }
  return { config, csrfToken: csrfMatch[1] };
}

export async function readServedRuntimeConfig(context) {
  return (await servedBootstrap(context)).config;
}

export async function requestAgentPairingTicket(context) {
  const cookie = await browserSessionCookieHeader(context);
  const { config, csrfToken } = await servedBootstrap(context);
  const response = await requestBrain('/api/v1/agent/pairing', {
    headers: {
      Accept: 'application/json',
      Cookie: cookie,
      Origin: BRAIN_ORIGIN,
      'X-CSRF-Token': csrfToken,
    },
    method: 'POST',
  });
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Agent pairing request failed (${response.status}).`);
  }
  const document = JSON.parse(response.body);
  if (
    typeof document?.pairing_ticket !== 'string'
    || document.pairing_ticket.length === 0
    || typeof document.expires_at !== 'string'
  ) {
    throw new Error('Brain returned an invalid Agent pairing response.');
  }
  return {
    creatorAccountId: config.CREATOR_ID,
    extensionId: config.EXTENSION_ID,
    pairingTicket: document.pairing_ticket,
    expiresAt: document.expires_at,
  };
}

export async function readBrainSummary(context, { timeoutMs = 10_000 } = {}) {
  const config = await readServedRuntimeConfig(context);
  return new Promise((resolve, reject) => {
    const probeUrl = new URL(config.FASTAPI_WS_URL);
    probeUrl.hostname = '127.0.0.1';
    const socket = new WebSocket(probeUrl);
    const bridgeSessionId = randomUUID();
    const helloId = randomUUID();
    let snapshot = null;
    let agent = null;
    let settled = false;
    const timeout = setTimeout(() => finish(new Error('Timed out reading Brain state.')), timeoutMs);

    function finish(error = null) {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      try {
        socket.close(1000, 'E2E probe complete');
      } catch {
        // The socket may not have opened.
      }
      if (error) {
        reject(error);
        return;
      }
      const totalMessages = snapshot.analytics.total_messages;
      resolve({
        viewRevision: snapshot.view_revision,
        conversationCount: snapshot.conversations.length,
        messageCount: totalMessages.value,
        analyticsBasis: totalMessages.basis,
        analytics: snapshot.analytics,
        coverage: snapshot.coverage,
        projection: snapshot.projection,
        liveFreshness: snapshot.live_freshness,
        summaryOnly: snapshot.conversations.every((conversation) => (
          !Object.hasOwn(conversation, 'messages')
        )),
        agentStatus: agent.status,
        appliedConfigRevision: agent.applied_config_revision,
        requiredConfigRevision: agent.required_config_revision,
        lastHeartbeatAt: agent.last_heartbeat_at,
        connectionToken: agent.connection_id,
      });
    }

    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({
        type: 'bridge.hello',
        protocol_version: '2',
        message_id: helloId,
        payload: {
          auth_ticket: config.BRIDGE_AUTH_TICKET,
          bridge_session_id: bridgeSessionId,
          requested_creator_account_id: config.CREATOR_ID,
          capabilities: ['state.snapshot', 'state.delta', 'presence.state', 'message.page'],
          client_version: 'e2e-capture-2',
          last_view_revision: null,
        },
      }));
    });
    socket.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        finish(new Error('Brain probe received malformed JSON.'));
        return;
      }
      if (message.type === 'protocol.error') {
        finish(new Error(`Brain probe protocol error: ${message.payload?.code ?? 'unknown'}`));
        return;
      }
      if (message.type === 'state.snapshot') snapshot = message.payload;
      if (message.type === 'agent.state') agent = message.payload;
      if (snapshot !== null && agent !== null) finish();
    });
    socket.addEventListener('error', () => finish(new Error('Brain probe WebSocket failed.')));
    socket.addEventListener('close', () => {
      if (!settled) finish(new Error('Brain probe closed before state was received.'));
    });
  });
}
