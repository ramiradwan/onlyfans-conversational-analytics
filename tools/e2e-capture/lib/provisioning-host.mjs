import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';

import { chromium } from '@playwright/test';

import { BRAIN_LOOPBACK_URL, BRAIN_ORIGIN, BRAIN_PORT } from './brain.mjs';
import { E2E_ROOT, PRODUCT_ROOT, pythonExecutable } from './paths.mjs';

const HOST = '127.0.0.1';
const PROVISIONING_HOST_HEADER = new URL(BRAIN_ORIGIN).host;
const GRANT_AUTHORITY = path.join(E2E_ROOT, 'helpers', 'provisioning_grant_authority.py');

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function portAcceptsConnections(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: HOST, port });
    socket.once('connect', () => {
      socket.destroy();
      resolve(true);
    });
    socket.once('error', () => resolve(false));
    socket.setTimeout(500, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

async function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return Promise.race([
    new Promise((resolve) => child.once('exit', () => resolve(true))),
    delay(timeoutMs).then(() => false),
  ]);
}

function requestProvisioning(pathname, { headers = {}, method = 'GET' } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      headers: { Host: PROVISIONING_HOST_HEADER, ...headers },
      host: HOST,
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

/**
 * Launch a browser with no extension loaded. The provisioning page reads a
 * creator account from the extension over external messaging, which needs a
 * signed-in platform session, so this stage supplies that one response from an
 * init script instead and drives every other control the shipped page owns.
 */
export async function launchProvisioningBrowser(userDataDir, { creatorAccountId }) {
  const executablePath = process.env.OFCA_E2E_BROWSER_EXECUTABLE;
  const context = await chromium.launchPersistentContext(userDataDir, {
    ...(executablePath ? { executablePath } : {}),
    headless: false,
    viewport: { width: 1280, height: 900 },
    args: [
      '--host-resolver-rules=MAP bridge.localhost 127.0.0.1',
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-default-apps',
      '--disable-sync',
      '--metrics-recording-only',
      '--lang=en-US',
      '--no-default-browser-check',
      '--no-first-run',
    ],
  });
  await context.addInitScript((accountId) => {
    const sendMessage = (_extensionId, message, callback) => {
      if (message?.type !== 'provisioning.identity.query') {
        callback(undefined);
        return;
      }
      callback({
        type: 'provisioning.identity.result',
        version: 1,
        authenticated_profile: { creator_account_id: accountId },
      });
    };
    // Chromium installs its own `chrome` object over anything this script
    // assigns, so the messaging response is reattached through the property
    // rather than written once.
    const withMessaging = (host) => {
      const target = host !== null && typeof host === 'object' ? host : {};
      target.runtime = { ...(target.runtime ?? {}), sendMessage };
      return target;
    };
    let installed = withMessaging(globalThis.chrome ?? null);
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'chrome');
    if (descriptor !== undefined && descriptor.configurable !== true) return;
    Object.defineProperty(globalThis, 'chrome', {
      configurable: true,
      get: () => installed,
      set: (value) => { installed = withMessaging(value); },
    });
  }, creatorAccountId);
  return context;
}

export class ProvisioningHost {
  constructor({ dataDirectory, extensionId }) {
    this.dataDirectory = dataDirectory;
    this.extensionId = extensionId;
    this.handoffToken = randomBytes(32).toString('base64url');
    this.descriptor = null;
    this.child = null;
    this.output = [];
  }

  async start() {
    if (this.child !== null) throw new Error('The provisioning host is already running.');
    if (await portAcceptsConnections(BRAIN_PORT)) {
      throw new Error(`Port ${BRAIN_PORT} is already in use; refusing to reuse or stop another process.`);
    }

    const child = spawn(
      pythonExecutable(),
      [
        GRANT_AUTHORITY,
        '--data-directory', this.dataDirectory,
        '--extension-id', this.extensionId,
        '--handoff-token', this.handoffToken,
      ],
      {
        cwd: PRODUCT_ROOT,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          // Key derivation reads this, and the runtime that later opens the
          // same store declares it too.
          ENVIRONMENT: 'test',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      },
    );
    this.child = child;
    let firstLine = '';
    let descriptorLine = null;
    const remember = (chunk) => {
      this.output.push(String(chunk));
      if (this.output.length > 80) this.output.splice(0, this.output.length - 80);
    };
    child.stdout.on('data', (chunk) => {
      remember(chunk);
      if (descriptorLine !== null) return;
      firstLine += String(chunk);
      const boundary = firstLine.indexOf('\n');
      if (boundary >= 0) descriptorLine = firstLine.slice(0, boundary).trim();
    });
    child.stderr.on('data', remember);

    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) {
        this.child = null;
        throw new Error(`The provisioning host exited during startup.\n${this.recentOutput()}`);
      }
      if (descriptorLine !== null) {
        try {
          const response = await fetch(`${BRAIN_LOOPBACK_URL}/health`, { cache: 'no-store' });
          if (response.ok) {
            this.descriptor = JSON.parse(descriptorLine);
            return this.descriptor;
          }
        } catch {
          // The listener is not ready yet.
        }
      }
      await delay(100);
    }
    await this.stop();
    throw new Error(`The provisioning host did not become healthy.\n${this.recentOutput()}`);
  }

  /** Exchange the launcher secret for one single-use browser handoff code. */
  async issueHandoffCode() {
    const response = await requestProvisioning('/api/v1/provisioning/handoff', {
      headers: { Authorization: `Provisioning ${this.handoffToken}` },
      method: 'POST',
    });
    if (response.status !== 200) {
      throw new Error(`Provisioning handoff was refused (${response.status}).`);
    }
    const document = JSON.parse(response.body);
    if (typeof document?.handoff_code !== 'string' || document.handoff_code.length === 0) {
      throw new Error('The provisioning host returned an invalid handoff code.');
    }
    return document.handoff_code;
  }

  async stop() {
    const child = this.child;
    this.child = null;
    if (child === null) return;
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    if (!(await waitForExit(child, 8_000))) {
      child.kill('SIGKILL');
      await waitForExit(child, 3_000);
    }
    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline && await portAcceptsConnections(BRAIN_PORT)) {
      await delay(100);
    }
    if (await portAcceptsConnections(BRAIN_PORT)) {
      throw new Error(`The provisioning host stopped but port ${BRAIN_PORT} remains occupied.`);
    }
  }

  recentOutput() {
    return this.output.join('').trim();
  }
}
