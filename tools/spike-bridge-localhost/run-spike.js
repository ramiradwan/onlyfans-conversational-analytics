import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

import {
  BRIDGE_HOST,
  BRIDGE_ORIGIN,
  BridgeSpikeHarness,
  PRIMARY_PORT,
  SECONDARY_PORT,
  SESSION_COOKIE,
  SESSION_VALUE,
  SET_COOKIE,
  isLoopbackAddress,
} from './server.js';

const SPIKE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROFILE_ROOT = path.join(SPIKE_DIR, '.profiles');
const RESULT_PATH = path.join(SPIKE_DIR, 'results.json');
const REQUEST_LOG_PATH = path.join(SPIKE_DIR, 'request-log.json');
const EVIDENCE_PATH = path.join(SPIKE_DIR, 'EVIDENCE.md');

const CHANNELS = [
  { id: 'chrome', label: 'Chrome', channel: 'chrome' },
  { id: 'msedge', label: 'Edge', channel: 'msedge' },
];

const CRITERIA = [
  [1, 'Loopback resolution'],
  [2, 'Secure context and service worker'],
  [3, 'Exact session-cookie contract'],
  [4, 'Cookie isolation'],
  [5, 'Cross-origin/port rejection'],
  [6, 'WebAuthn virtual platform authenticator'],
];

function outcome(pass, detail, observations = {}) {
  return { status: pass ? 'PASS' : 'FAIL', detail, observations };
}

async function capture(check) {
  try {
    return await check();
  } catch (error) {
    return outcome(false, error.message, {
      name: error.name,
      stack: error.stack,
    });
  }
}

function latestRequest(harness, predicate, since = 0) {
  for (let index = harness.requests.length - 1; index >= since; index -= 1) {
    if (predicate(harness.requests[index])) return harness.requests[index];
  }
  return null;
}

function requestJson({ port = PRIMARY_PORT, path: requestPath = '/health', headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: '127.0.0.1',
      port,
      path: requestPath,
      method: 'GET',
      headers,
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        try {
          resolve({ status: response.statusCode, body: JSON.parse(body) });
        } catch {
          resolve({ status: response.statusCode, body });
        }
      });
    });
    request.once('error', reject);
    request.end();
  });
}

function manualProtectedRequest() {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: '127.0.0.1',
      port: PRIMARY_PORT,
      path: '/protected?case=manual-cookie-wrong-origin',
      method: 'POST',
      headers: {
        Host: BRIDGE_HOST,
        Origin: `http://bridge.localhost:${SECONDARY_PORT}`,
        Cookie: SESSION_COOKIE,
      },
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({
        status: response.statusCode,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
    });
    request.once('error', reject);
    request.end();
  });
}

function spawnNode(scriptName) {
  const child = spawn(process.execPath, [path.join(SPIKE_DIR, scriptName)], {
    cwd: SPIKE_DIR,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdoutText = '';
  child.stderrText = '';
  child.stdout.on('data', (chunk) => { child.stdoutText += chunk.toString('utf8'); });
  child.stderr.on('data', (chunk) => { child.stderrText += chunk.toString('utf8'); });
  return child;
}

async function waitForOutput(child, text, timeoutMs = 8_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (child.stdoutText.includes(text) || child.stderrText.includes(text)) return;
    if (child.exitCode !== null) {
      throw new Error(`Child exited before ${text}: stdout=${child.stdoutText} stderr=${child.stderrText}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for ${text}: stdout=${child.stdoutText} stderr=${child.stderrText}`);
}

async function waitForExit(child, timeoutMs = 8_000) {
  if (child.exitCode !== null) return { code: child.exitCode, signal: child.signalCode };
  const timeout = new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('Timed out waiting for child exit')), timeoutMs);
    timer.unref();
  });
  const [code, signal] = await Promise.race([once(child, 'exit'), timeout]);
  return { code, signal };
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  child.kill('SIGTERM');
  try {
    await waitForExit(child, 5_000);
  } catch {
    child.kill('SIGKILL');
    await waitForExit(child, 5_000);
  }
}

async function proveHarnessAcquiresPort() {
  const child = spawnNode('server.js');
  await waitForOutput(child, 'HARNESS_READY');
  const health = await requestJson({ headers: { Host: BRIDGE_HOST } });
  assert.equal(health.status, 200, 'started harness did not serve health');
  assert.equal(health.body.product, 'bridge-localhost-adr-0009-spike');
  const readyLine = child.stdoutText.split(/\r?\n/).find((line) => line.startsWith('HARNESS_READY '));
  await stopChild(child);
  return { health, readyLine };
}

async function runPortBehavior() {
  let dummy;
  let conflictingHarness;
  try {
    dummy = spawnNode('dummy-port-owner.js');
    await waitForOutput(dummy, 'DUMMY_READY');
    const dummyPid = dummy.pid;

    conflictingHarness = spawnNode('server.js');
    const conflictExit = await waitForExit(conflictingHarness);
    assert.equal(conflictExit.code, 2, 'harness did not refuse the occupied port with exit code 2');
    assert.match(conflictingHarness.stderrText, /PORT_CONFLICT: Port 17871 is already in use/);
    assert.match(conflictingHarness.stderrText, /Stop the unrelated process.*then retry/);
    assert.match(conflictingHarness.stderrText, /did not terminate the owner/);

    assert.equal(dummy.exitCode, null, 'the conflicting harness terminated the unrelated dummy owner');
    const stillOwned = await requestJson();
    assert.equal(stillOwned.status, 200);
    assert.equal(stillOwned.body.owner, 'unrelated-dummy-process');
    assert.equal(stillOwned.body.pid, dummyPid);

    await stopChild(dummy);
    dummy = null;

    const firstStart = await proveHarnessAcquiresPort();
    const restart = await proveHarnessAcquiresPort();
    return outcome(true,
      'Conflict was detected with actionable remediation; the unrelated owner remained alive. After release, the harness acquired 17871 and reacquired it on restart.',
      {
        dummyPid,
        conflictExit,
        diagnostic: conflictingHarness.stderrText.trim(),
        unrelatedOwnerAfterConflict: stillOwned,
        firstStart,
        restart,
        databaseFilesCreated: false,
      });
  } finally {
    await stopChild(conflictingHarness);
    await stopChild(dummy);
  }
}

function bytesToBase64Url(value) {
  return Buffer.from(value).toString('base64url');
}

function fromBase64(value) {
  return Buffer.from(value, 'base64');
}

function decodeCbor(buffer) {
  let offset = 0;

  function lengthFor(additional) {
    if (additional < 24) return additional;
    if (additional === 24) return buffer[offset++];
    if (additional === 25) {
      const value = buffer.readUInt16BE(offset);
      offset += 2;
      return value;
    }
    if (additional === 26) {
      const value = buffer.readUInt32BE(offset);
      offset += 4;
      return value;
    }
    if (additional === 27) {
      const value = Number(buffer.readBigUInt64BE(offset));
      offset += 8;
      return value;
    }
    throw new Error(`Unsupported CBOR additional information ${additional}`);
  }

  function item() {
    const initial = buffer[offset++];
    const major = initial >> 5;
    const additional = initial & 0x1f;
    const length = lengthFor(additional);
    if (major === 0) return length;
    if (major === 1) return -1 - length;
    if (major === 2) {
      const value = buffer.subarray(offset, offset + length);
      offset += length;
      return value;
    }
    if (major === 3) {
      const value = buffer.subarray(offset, offset + length).toString('utf8');
      offset += length;
      return value;
    }
    if (major === 4) return Array.from({ length }, () => item());
    if (major === 5) {
      const value = {};
      for (let index = 0; index < length; index += 1) value[item()] = item();
      return value;
    }
    if (major === 6) return { tag: length, value: item() };
    if (major === 7) {
      if (additional === 20) return false;
      if (additional === 21) return true;
      if (additional === 22) return null;
      return length;
    }
    throw new Error(`Unsupported CBOR major type ${major}`);
  }

  return item();
}

async function checkLoopbackResolution(page, harness, tag, since) {
  const bridgePath = `/?criterion=loopback&tag=${encodeURIComponent(tag)}`;
  const otherPath = `/echo?criterion=loopback-subdomain&tag=${encodeURIComponent(tag)}`;
  const bridgeResponse = await page.goto(`${BRIDGE_ORIGIN}${bridgePath}`, { waitUntil: 'domcontentloaded' });
  assert.equal(bridgeResponse?.status(), 200);
  const otherResponse = await page.goto(`http://other.bridge.localhost:${PRIMARY_PORT}${otherPath}`, { waitUntil: 'domcontentloaded' });
  assert.equal(otherResponse?.status(), 200);

  const bridgeRequest = latestRequest(harness,
    (entry) => entry.host === BRIDGE_HOST && entry.url === bridgePath,
    since);
  const otherRequest = latestRequest(harness,
    (entry) => entry.host === `other.bridge.localhost:${PRIMARY_PORT}` && entry.url === otherPath,
    since);
  assert.ok(bridgeRequest, 'server did not observe bridge.localhost request');
  assert.ok(otherRequest, 'server did not observe other.bridge.localhost request');
  assert.ok(isLoopbackAddress(bridgeRequest.remoteAddress), `bridge.localhost connected from non-loopback ${bridgeRequest.remoteAddress}`);
  assert.ok(isLoopbackAddress(otherRequest.remoteAddress), `other.bridge.localhost connected from non-loopback ${otherRequest.remoteAddress}`);

  return outcome(true,
    `bridge.localhost and other.bridge.localhost loaded without DNS/hosts configuration; observed peers ${bridgeRequest.remoteAddress} and ${otherRequest.remoteAddress}, both loopback.`,
    { bridgeRequest, otherRequest, dnsOrHostsConfigurationPerformed: false });
}

async function checkSecureContext(page, tag) {
  await page.goto(`${BRIDGE_ORIGIN}/?criterion=secure-context&tag=${encodeURIComponent(tag)}`, { waitUntil: 'domcontentloaded' });
  const secure = await page.evaluate(() => window.isSecureContext);
  assert.equal(secure, true, 'window.isSecureContext was false');
  const serviceWorker = await page.evaluate(async () => {
    try {
      const registration = await navigator.serviceWorker.register('/sw.js');
      await Promise.race([
        navigator.serviceWorker.ready,
        new Promise((_, reject) => setTimeout(() => reject(new Error('service-worker ready timeout')), 5_000)),
      ]);
      return { ok: true, scope: registration.scope };
    } catch (error) {
      return { ok: false, name: error.name, message: error.message };
    }
  });
  assert.equal(serviceWorker.ok, true, `${serviceWorker.name}: ${serviceWorker.message}`);
  assert.equal(serviceWorker.scope, `${BRIDGE_ORIGIN}/`);
  return outcome(true,
    `window.isSecureContext=true; service-worker registration succeeded with scope ${serviceWorker.scope}.`,
    { isSecureContext: secure, serviceWorker });
}

async function checkCookieContract(page, context, harness, tag, since) {
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.clearBrowserCookies');
  await page.goto(`${BRIDGE_ORIGIN}/?criterion=cookie&tag=${encodeURIComponent(tag)}`, { waitUntil: 'domcontentloaded' });
  const setStatus = await page.evaluate(async () => {
    const response = await fetch('/set-cookie', { credentials: 'include' });
    return response.status;
  });
  assert.equal(setStatus, 204);

  const cookieResult = await cdp.send('Network.getCookies', { urls: [BRIDGE_ORIGIN] });
  const cookie = cookieResult.cookies.find((candidate) => candidate.name === '__Host-bridge_session');
  assert.ok(cookie, '__Host-bridge_session was not accepted by the browser');
  assert.equal(cookie.value, SESSION_VALUE);
  assert.equal(cookie.domain, 'bridge.localhost');
  assert.equal(cookie.path, '/');
  assert.equal(cookie.secure, true);
  assert.equal(cookie.httpOnly, true);
  assert.equal(cookie.sameSite, 'Strict');

  const scriptCookie = await page.evaluate(() => document.cookie);
  assert.equal(scriptCookie.includes('__Host-bridge_session'), false, 'HttpOnly cookie was visible to script');
  const protectedResponse = await page.evaluate(async () => {
    const response = await fetch('/protected?case=same-origin', {
      method: 'POST',
      credentials: 'include',
    });
    return { status: response.status, body: await response.json() };
  });
  assert.equal(protectedResponse.status, 200);
  assert.equal(protectedResponse.body.accepted, true);
  const protectedRequest = latestRequest(harness,
    (entry) => entry.url === '/protected?case=same-origin',
    since);
  assert.ok(protectedRequest?.cookie?.includes(SESSION_COOKIE), 'cookie was not sent on the same-origin request');
  assert.equal(protectedRequest.origin, BRIDGE_ORIGIN);

  await cdp.detach();
  return outcome(true,
    'CDP reported the accepted host-only cookie as Secure, HttpOnly, SameSite=Strict, Path=/; document.cookie could not read it and the same-origin protected request received it.',
    { setCookieHeader: SET_COOKIE, cdpCookie: cookie, scriptCookie, protectedRequest });
}

async function checkCookieIsolation(page, harness, tag, since) {
  const cases = [
    ['localhost', `http://localhost:${PRIMARY_PORT}`],
    ['127.0.0.1', `http://127.0.0.1:${PRIMARY_PORT}`],
    ['other.localhost', `http://other.localhost:${PRIMARY_PORT}`],
  ];
  const observed = [];
  for (const [name, origin] of cases) {
    const requestPath = `/echo?criterion=cookie-isolation&case=${encodeURIComponent(name)}&tag=${encodeURIComponent(tag)}`;
    const response = await page.goto(`${origin}${requestPath}`, { waitUntil: 'domcontentloaded' });
    assert.equal(response?.status(), 200, `${name} did not load`);
    const request = latestRequest(harness, (entry) => entry.url === requestPath, since);
    assert.ok(request, `server did not observe ${name}`);
    assert.equal(String(request.cookie || '').includes(SESSION_COOKIE), false, `cookie leaked to ${name}`);
    observed.push({ name, request });
  }
  return outcome(true,
    'No __Host-bridge_session cookie was sent to localhost, 127.0.0.1, or other.localhost.',
    { cases: observed });
}

async function checkCrossOriginAndPort(page, harness, tag, since) {
  const portScopePath = `/echo?criterion=cookie-port-scope&tag=${encodeURIComponent(tag)}`;
  const portResponse = await page.goto(`http://bridge.localhost:${SECONDARY_PORT}${portScopePath}`, { waitUntil: 'domcontentloaded' });
  assert.equal(portResponse?.status(), 200);
  const portScopeRequest = latestRequest(harness, (entry) => entry.url === portScopePath, since);
  assert.ok(portScopeRequest, 'secondary-port request was not observed');
  assert.ok(portScopeRequest.cookie?.includes(SESSION_COOKIE), 'cookie was not sent to the same host on a different port');

  await page.goto(`http://bridge.localhost:${SECONDARY_PORT}/attacker?tag=${encodeURIComponent(tag)}`, { waitUntil: 'domcontentloaded' });
  const crossPortFetch = await page.evaluate(async (target) => {
    try {
      const response = await fetch(`${target}/protected?case=cross-port-browser`, {
        method: 'POST',
        credentials: 'include',
      });
      return { fetchResolved: true, status: response.status };
    } catch (error) {
      return { fetchResolved: false, name: error.name, message: error.message };
    }
  }, BRIDGE_ORIGIN);
  const crossPortRequest = latestRequest(harness,
    (entry) => entry.url === '/protected?case=cross-port-browser',
    since);
  assert.ok(crossPortRequest, 'cross-port browser request was not observed');
  assert.equal(crossPortRequest.origin, `http://bridge.localhost:${SECONDARY_PORT}`);
  assert.ok(crossPortRequest.cookie?.includes(SESSION_COOKIE), 'cross-port browser request did not demonstrate cookie port scope');
  assert.equal(crossPortRequest.validation.accepted, false);
  assert.equal(crossPortRequest.responseStatus, 403);

  const forgedFetch = await page.evaluate(async ({ target, cookie }) => {
    try {
      const response = await fetch(`${target}/protected?case=browser-forged-host`, {
        method: 'POST',
        credentials: 'include',
        headers: { Host: 'bridge.localhost:17871', Cookie: cookie },
      });
      return { fetchResolved: true, status: response.status };
    } catch (error) {
      return { fetchResolved: false, name: error.name, message: error.message };
    }
  }, { target: `http://127.0.0.1:${PRIMARY_PORT}`, cookie: SESSION_COOKIE });
  const forgedRequest = latestRequest(harness,
    (entry) => entry.url === '/protected?case=browser-forged-host',
    since);
  assert.ok(forgedRequest, 'browser forged-Host request was not observed');
  assert.equal(forgedRequest.host, `127.0.0.1:${PRIMARY_PORT}`, 'browser allowed script to forge Host');
  assert.equal(forgedRequest.cookie?.includes(SESSION_COOKIE) ?? false, false, 'browser allowed script to attach Cookie manually');
  assert.equal(forgedRequest.validation.accepted, false);
  assert.equal(forgedRequest.responseStatus, 403);

  const manual = await manualProtectedRequest();
  assert.equal(manual.status, 403, 'manual cookie with wrong Origin was accepted');
  const manualRequest = latestRequest(harness,
    (entry) => entry.url === '/protected?case=manual-cookie-wrong-origin',
    since);
  assert.ok(manualRequest?.cookie?.includes(SESSION_COOKIE));
  assert.equal(manualRequest.host, BRIDGE_HOST);
  assert.equal(manualRequest.origin, `http://bridge.localhost:${SECONDARY_PORT}`);
  assert.equal(manualRequest.validation.accepted, false);

  return outcome(true,
    'The cookie was observed on bridge.localhost:17872 (cookies are not port-scoped), but 17872-to-17871 use was rejected with 403. Browser-forged Host/Cookie headers were forbidden, and a raw request with exact Host plus a manually attached cookie was still rejected for wrong Origin.',
    { portScopeRequest, crossPortFetch, crossPortRequest, forgedFetch, forgedRequest, manual, manualRequest });
}

async function checkWebAuthn(page, context, tag) {
  await page.goto(`${BRIDGE_ORIGIN}/?criterion=webauthn&tag=${encodeURIComponent(tag)}`, { waitUntil: 'domcontentloaded' });
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });

  try {
    const createChallenge = crypto.randomBytes(32);
    const userId = crypto.randomBytes(16);
    const created = await page.evaluate(async ({ challenge, user }) => {
      const toBytes = (value) => Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
      const toBase64 = (value) => btoa(String.fromCharCode(...new Uint8Array(value)));
      try {
        const credential = await navigator.credentials.create({
          publicKey: {
            challenge: toBytes(challenge),
            rp: { id: 'bridge.localhost', name: 'Bridge localhost spike' },
            user: { id: toBytes(user), name: 'spike-user', displayName: 'Spike User' },
            pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
            timeout: 5_000,
            attestation: 'none',
            authenticatorSelection: {
              authenticatorAttachment: 'platform',
              residentKey: 'required',
              userVerification: 'required',
            },
          },
        });
        return {
          ok: true,
          id: credential.id,
          rawId: toBase64(credential.rawId),
          type: credential.type,
          authenticatorAttachment: credential.authenticatorAttachment,
          response: {
            clientDataJSON: toBase64(credential.response.clientDataJSON),
            attestationObject: toBase64(credential.response.attestationObject),
            transports: credential.response.getTransports?.() ?? [],
          },
        };
      } catch (error) {
        return { ok: false, name: error.name, message: error.message };
      }
    }, { challenge: createChallenge.toString('base64'), user: userId.toString('base64') });
    assert.equal(created.ok, true, `${created.name}: ${created.message}`);

    const createClientData = JSON.parse(fromBase64(created.response.clientDataJSON).toString('utf8'));
    assert.equal(createClientData.type, 'webauthn.create');
    assert.equal(createClientData.origin, BRIDGE_ORIGIN);
    assert.equal(createClientData.challenge, bytesToBase64Url(createChallenge));
    const attestation = decodeCbor(fromBase64(created.response.attestationObject));
    const createAuthData = attestation.authData;
    assert.ok(Buffer.isBuffer(createAuthData), 'attestation authData was absent');
    const expectedRpIdHash = crypto.createHash('sha256').update('bridge.localhost').digest();
    assert.equal(createAuthData.subarray(0, 32).equals(expectedRpIdHash), true, 'create RP ID hash mismatch');
    assert.ok((createAuthData[32] & 0x01) !== 0, 'create response lacked user-presence flag');
    assert.ok((createAuthData[32] & 0x04) !== 0, 'create response lacked user-verification flag');

    const cdpCredentials = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
    const cdpCredential = cdpCredentials.credentials.find((credential) => credential.rpId === 'bridge.localhost');
    assert.ok(cdpCredential, 'virtual authenticator did not store the bridge.localhost credential');

    const getChallenge = crypto.randomBytes(32);
    const getAttempt = async ({ rpId, userVerification = 'required', timeout = 3_000 }) => page.evaluate(async (options) => {
      const toBytes = (value) => Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
      const toBase64 = (value) => btoa(String.fromCharCode(...new Uint8Array(value)));
      try {
        const credential = await navigator.credentials.get({
          publicKey: {
            challenge: toBytes(options.challenge),
            rpId: options.rpId,
            allowCredentials: [{
              type: 'public-key',
              id: toBytes(options.rawId),
              transports: ['internal'],
            }],
            timeout: options.timeout,
            userVerification: options.userVerification,
          },
        });
        return {
          ok: true,
          id: credential.id,
          response: {
            clientDataJSON: toBase64(credential.response.clientDataJSON),
            authenticatorData: toBase64(credential.response.authenticatorData),
            signature: toBase64(credential.response.signature),
            userHandle: credential.response.userHandle ? toBase64(credential.response.userHandle) : null,
          },
        };
      } catch (error) {
        return { ok: false, name: error.name, message: error.message };
      }
    }, {
      challenge: getChallenge.toString('base64'),
      rawId: created.rawId,
      rpId,
      timeout,
      userVerification,
    });

    const assertion = await getAttempt({ rpId: 'bridge.localhost' });
    assert.equal(assertion.ok, true, `${assertion.name}: ${assertion.message}`);
    const getClientData = JSON.parse(fromBase64(assertion.response.clientDataJSON).toString('utf8'));
    assert.equal(getClientData.type, 'webauthn.get');
    assert.equal(getClientData.origin, BRIDGE_ORIGIN);
    assert.equal(getClientData.challenge, bytesToBase64Url(getChallenge));
    const getAuthData = fromBase64(assertion.response.authenticatorData);
    assert.equal(getAuthData.subarray(0, 32).equals(expectedRpIdHash), true, 'get RP ID hash mismatch');
    assert.ok((getAuthData[32] & 0x01) !== 0, 'assertion lacked user-presence flag');
    assert.ok((getAuthData[32] & 0x04) !== 0, 'assertion lacked user-verification flag');

    const localhostAttempt = await getAttempt({ rpId: 'localhost', timeout: 1_500 });
    assert.equal(localhostAttempt.ok, false, 'credential was usable with RP ID localhost');
    const wrongRpAttempt = await getAttempt({ rpId: 'wrong.localhost', timeout: 1_500 });
    assert.equal(wrongRpAttempt.ok, false, 'credential was usable with wrong.localhost');

    await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: false });
    const noUvAttempt = await getAttempt({ rpId: 'bridge.localhost', timeout: 1_500 });
    assert.equal(noUvAttempt.ok, false, 'userVerification=required succeeded while virtual user verification was false');
    await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: true });

    return outcome(true,
      `CTAP2 internal virtual-authenticator create/get succeeded for bridge.localhost with UV flags set; localhost failed (${localhostAttempt.name}), wrong.localhost failed (${wrongRpAttempt.name}), and required UV failed when isUserVerified=false (${noUvAttempt.name}).`,
      {
        authenticatorOptions: {
          protocol: 'ctap2',
          transport: 'internal',
          hasUserVerification: true,
          isUserVerified: true,
        },
        create: {
          id: created.id,
          authenticatorAttachment: created.authenticatorAttachment,
          transports: created.response.transports,
          clientData: createClientData,
          flags: createAuthData[32],
          rpIdHash: createAuthData.subarray(0, 32).toString('hex'),
        },
        get: {
          clientData: getClientData,
          flags: getAuthData[32],
          rpIdHash: getAuthData.subarray(0, 32).toString('hex'),
        },
        localhostAttempt,
        wrongRpAttempt,
        noUvAttempt,
      });
  } finally {
    await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId }).catch(() => {});
    await cdp.detach().catch(() => {});
  }
}

async function runProfilePhase(browserConfig, phase, profileDirectory, harness, headless = true) {
  const phaseStart = harness.requests.length;
  const context = await chromium.launchPersistentContext(profileDirectory, {
    channel: browserConfig.channel,
    headless,
    args: ['--no-proxy-server'],
  });
  const page = context.pages()[0] ?? await context.newPage();
  page.setDefaultTimeout(10_000);
  const browser = context.browser();
  const cdp = await context.newCDPSession(page);
  const browserProduct = await cdp.send('Browser.getVersion');
  await cdp.detach();
  const tag = `${browserConfig.id}-${phase}`;
  const checks = {};
  try {
    checks['1'] = await capture(() => checkLoopbackResolution(page, harness, tag, phaseStart));
    checks['2'] = await capture(() => checkSecureContext(page, tag));
    checks['3'] = await capture(() => checkCookieContract(page, context, harness, tag, phaseStart));
    checks['4'] = await capture(() => checkCookieIsolation(page, harness, tag, phaseStart));
    checks['5'] = await capture(() => checkCrossOriginAndPort(page, harness, tag, phaseStart));
    checks['6'] = await capture(() => checkWebAuthn(page, context, tag));
    return {
      phase,
      profileKind: phase === 'fresh' ? 'new throwaway persistent profile' : 'same throwaway profile reopened',
      headless,
      version: browser?.version() ?? browserProduct.product,
      browserProduct,
      checks,
      requestRange: { start: phaseStart + 1, end: harness.requests.length },
    };
  } finally {
    await context.close();
  }
}

async function runHeadedWebAuthnFallback(browserConfig, profileDirectory, harness, phase) {
  const context = await chromium.launchPersistentContext(profileDirectory, {
    channel: browserConfig.channel,
    headless: false,
    args: ['--no-proxy-server'],
  });
  try {
    const page = context.pages()[0] ?? await context.newPage();
    page.setDefaultTimeout(10_000);
    return await capture(() => checkWebAuthn(page, context, `${browserConfig.id}-${phase}-headed-fallback`));
  } finally {
    await context.close();
  }
}

async function runBrowserChannel(browserConfig, harness) {
  const profileDirectory = path.join(PROFILE_ROOT, browserConfig.id);
  await fs.rm(profileDirectory, { recursive: true, force: true });
  await fs.mkdir(profileDirectory, { recursive: true });
  const phases = [];
  for (const phase of ['fresh', 'reused']) {
    const phaseResult = await runProfilePhase(browserConfig, phase, profileDirectory, harness, true);
    if (phaseResult.checks['6'].status === 'FAIL') {
      const fallback = await runHeadedWebAuthnFallback(browserConfig, profileDirectory, harness, phase);
      phaseResult.webAuthnHeadedFallback = fallback;
      if (fallback.status === 'PASS') {
        phaseResult.checks['6'] = outcome(true,
          `Headless attempt failed (${phaseResult.checks['6'].detail}); headed fallback passed: ${fallback.detail}`,
          { headlessFailure: phaseResult.checks['6'], headedFallback: fallback });
      }
    }
    phases.push(phaseResult);
  }

  const checks = {};
  for (const [criterion] of CRITERIA) {
    const perPhase = phases.map((phase) => phase.checks[String(criterion)]);
    const passed = perPhase.every((entry) => entry.status === 'PASS');
    checks[String(criterion)] = outcome(passed,
      perPhase.map((entry, index) => `${phases[index].phase}: ${entry.detail}`).join(' Reused-profile check: '),
      { phases: perPhase });
  }
  return {
    id: browserConfig.id,
    label: browserConfig.label,
    channel: browserConfig.channel,
    version: phases[0].version,
    product: phases[0].browserProduct.product,
    userAgent: phases[0].browserProduct.userAgent,
    phases,
    checks,
  };
}

function failedBrowserChannel(browserConfig, error) {
  const checks = Object.fromEntries(CRITERIA.map(([criterion]) => [String(criterion), outcome(false, error.message, {
    name: error.name,
    stack: error.stack,
  })]));
  return {
    id: browserConfig.id,
    label: browserConfig.label,
    channel: browserConfig.channel,
    version: 'unavailable',
    product: 'launch failed',
    phases: [],
    checks,
  };
}

function markdownCell(value) {
  return String(value).replaceAll('|', '\\|').replaceAll('\r', ' ').replaceAll('\n', ' ');
}

function browserCell(browser, criterion) {
  const check = browser.checks[String(criterion)];
  return `${check.status} — ${check.detail}`;
}

function allBrowserChecksPass(results) {
  return results.browsers.every((browser) => CRITERIA.every(([criterion]) => browser.checks[String(criterion)].status === 'PASS'));
}

function generateEvidence(results) {
  const chrome = results.browsers.find((browser) => browser.id === 'chrome');
  const edge = results.browsers.find((browser) => browser.id === 'msedge');
  const browserPass = allBrowserChecksPass(results);
  const gatePass = browserPass && results.portBehavior.status === 'PASS';
  const matrixStatus = browserPass ? 'PASS' : 'FAIL';
  const verdict = gatePass
    ? '**PASS — the browser host passes the ADR 0009 implementation-spike gate on this machine.** Current stable Chrome and Edge passed every automated browser criterion at the exact production candidate origin and RP ID, so these results do not mandate the signed desktop-shell fallback. Cutover still requires manual confirmation with the real Windows Hello platform authenticator and installer/launcher/updater origin-invariance evidence; neither caveat permits weakening the cookie or authentication contract.'
    : '**FAIL — the browser host does not pass the ADR 0009 implementation-spike gate on this machine.** At least one required automated criterion failed, so ADR 0009 mandates the signed desktop-shell fallback unless a corrected spike later passes without weakening the cookie or authentication contract.';

  const rows = CRITERIA.map(([criterion, name]) => {
    const common = criterion === 5
      ? 'Cookies were explicitly observed as host-scoped but not port-scoped; exact Origin/Host validation is the compensating control within the accepted local-OS-compromise posture.'
      : criterion === 6
        ? 'The CDP authenticator simulates a platform authenticator; real Windows Hello confirmation is deferred to cutover E2E.'
        : 'Both a new throwaway persistent profile and the same profile reopened were exercised.';
    return `| ${criterion} | ${name} | ${markdownCell(browserCell(chrome, criterion))} | ${markdownCell(browserCell(edge, criterion))} | ${markdownCell(common)} |`;
  });
  rows.push(`| 7 | Port conflict, refusal, release, and reacquire | ${results.portBehavior.status} — shared harness check | ${results.portBehavior.status} — shared harness check | ${markdownCell(results.portBehavior.detail)} Installer/launcher/updater lifecycle origin invariance is **DEFERRED** to the installer phase because those components do not exist in this isolated browser spike. |`);
  rows.push(`| 8 | Both stable browser channels and profile states | ${matrixStatus} — Chrome ${markdownCell(chrome.version)}, fresh + reused profiles | ${matrixStatus} — Edge ${markdownCell(edge.version)}, fresh + reused profiles | All browser checks ran headless unless an individual WebAuthn result explicitly records a headed fallback. |`);

  return `# ADR 0009 browser-host implementation-spike evidence

> Machine-generated by \`npm run spike\` at ${results.generatedAt}. Do not hand-edit observed results.

## Verdict

${verdict}

## Criterion table

| # | Automated criterion | Chrome | Edge | Observed detail / deferred boundary |
| ---: | --- | --- | --- | --- |
${rows.join('\n')}

## Environment and exact versions

- OS: ${results.environment.os.type} ${results.environment.os.release}; ${results.environment.os.version}; ${results.environment.os.arch}
- Node: ${results.environment.node}
- Playwright: ${results.environment.playwright}
- Chrome channel \`chrome\`: ${chrome.product} (Playwright-reported version ${chrome.version})
- Edge channel \`msedge\`: ${edge.product} (Playwright-reported version ${edge.version})
- Candidate origin: \`${BRIDGE_ORIGIN}\`
- WebAuthn RP ID: \`bridge.localhost\`
- Listener bindings: ${results.listenerBindings.map((binding) => `\`${binding.host}:${binding.port}${binding.unavailable ? ` (${binding.code})` : ''}\``).join(', ')}
- Browser source: system-installed stable channels only. The runner passed Playwright channel names and did not request a bundled executable.

## Automation boundary and caveats

- The harness set exactly \`${SET_COOKIE}\`. CDP \`Network.getCookies\`, rather than \`document.cookie\`, supplied the acceptance and attribute evidence. The raw observations are in \`results.json\`; every received Host/Origin/Cookie tuple is in \`request-log.json\`.
- The CDP CTAP2/internal virtual authenticator had \`hasUserVerification=true\` and \`isUserVerified=true\`. It exercises the browser's WebAuthn path and response flags but only simulates Windows Hello. A manual enrollment/assertion with the real platform authenticator remains required for cutover E2E.
- Cookies are not port-scoped. The observed cookie delivery to \`bridge.localhost:${SECONDARY_PORT}\` is an accepted local-OS-compromise residual risk, mitigated at Brain by exact Host and Origin checks; it is not evidence of transport encryption.
- No hosts-file entry, DNS configuration, browser policy, or certificate was created. The browser connections observed by the server were loopback connections. This does not claim that application-level JavaScript can reveal Chromium's internal resolver path.
- Installer/launcher/updater origin invariance is deferred to the installer phase. The isolated spike can prove fixed-port conflict/refusal/reacquisition, but it cannot test components that do not yet exist.
- Playwright is a devDependency only of this spike directory. \`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1\` was used for installation; no bundled browser was downloaded or launched.
`;
}

async function playwrightVersion() {
  const packageJson = JSON.parse(await fs.readFile(path.join(SPIKE_DIR, 'node_modules', 'playwright', 'package.json'), 'utf8'));
  return packageJson.version;
}

async function main() {
  await fs.mkdir(PROFILE_ROOT, { recursive: true });
  const results = {
    generatedAt: new Date().toISOString(),
    environment: {
      os: {
        type: os.type(),
        release: os.release(),
        version: os.version(),
        arch: os.arch(),
      },
      node: process.version,
      playwright: await playwrightVersion(),
    },
    contract: {
      origin: BRIDGE_ORIGIN,
      rpId: 'bridge.localhost',
      setCookie: SET_COOKIE,
      primaryPort: PRIMARY_PORT,
      secondaryPortForPortScopeEvidence: SECONDARY_PORT,
    },
    portBehavior: await capture(runPortBehavior),
    listenerBindings: [],
    browsers: [],
  };

  const harness = new BridgeSpikeHarness({ includeSecondary: true });
  try {
    await harness.start();
    results.listenerBindings = harness.bindings;
    for (const browserConfig of CHANNELS) {
      process.stdout.write(`Running ${browserConfig.label} (${browserConfig.channel}) fresh and reused profiles...\n`);
      try {
        results.browsers.push(await runBrowserChannel(browserConfig, harness));
      } catch (error) {
        results.browsers.push(failedBrowserChannel(browserConfig, error));
      }
    }
  } finally {
    await harness.stop();
    await fs.writeFile(REQUEST_LOG_PATH, `${JSON.stringify(harness.requests, null, 2)}\n`, 'utf8');
  }

  results.generatedAt = new Date().toISOString();
  await fs.writeFile(RESULT_PATH, `${JSON.stringify(results, null, 2)}\n`, 'utf8');
  await fs.writeFile(EVIDENCE_PATH, generateEvidence(results), 'utf8');

  const passed = allBrowserChecksPass(results) && results.portBehavior.status === 'PASS';
  process.stdout.write(`Evidence written to ${EVIDENCE_PATH}\n`);
  process.stdout.write(`Overall spike result: ${passed ? 'PASS' : 'FAIL'}\n`);
  process.exitCode = passed ? 0 : 1;
}

await main();
