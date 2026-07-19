import { spawn } from 'node:child_process';
import net from 'node:net';

import { PRODUCT_ROOT, pythonExecutable } from './paths.mjs';

const BRAIN_HOST = '127.0.0.1';
export const BRAIN_PORT = 17_871;
export const BRAIN_ORIGIN = `http://bridge.localhost:${BRAIN_PORT}`;
export const BRAIN_LOOPBACK_URL = `http://${BRAIN_HOST}:${BRAIN_PORT}`;
export const BRAIN_HTTP_URL = BRAIN_ORIGIN;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function portAcceptsConnections(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: BRAIN_HOST, port });
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

export class BrainProcess {
  constructor({ canonicalDatabasePath, extensionId, projectionDatabasePath }) {
    this.canonicalDatabasePath = canonicalDatabasePath;
    this.extensionId = extensionId;
    this.projectionDatabasePath = projectionDatabasePath;
    this.child = null;
    this.output = [];
  }

  async start() {
    if (this.child !== null) throw new Error('Brain is already running.');
    if (await portAcceptsConnections(BRAIN_PORT)) {
      throw new Error(`Port ${BRAIN_PORT} is already in use; refusing to reuse or stop another process.`);
    }

    const child = spawn(
      pythonExecutable(),
      [
        '-m',
        'uvicorn',
        'app.main:app',
        '--host',
        BRAIN_HOST,
        '--port',
        String(BRAIN_PORT),
        '--workers',
        '1',
        '--no-access-log',
        '--log-level',
        'warning',
      ],
      {
        cwd: PRODUCT_ROOT,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          BROADCAST_URL: 'memory://',
          BRIDGE_ORIGIN: BRAIN_ORIGIN,
          CANONICAL_PERSISTENCE_BACKEND: 'sqlite',
          CANONICAL_DATABASE_PATH: this.canonicalDatabasePath,
          PROJECTION_DATABASE_PATH: this.projectionDatabasePath,
          EXTENSION_ID: this.extensionId,
          WEBSOCKET_AUTH_MODE: 'development_stub',
          WEBSOCKET_BIND_HOST: BRAIN_HOST,
          AGENT_HEARTBEAT_INTERVAL_SECONDS: '1',
          AGENT_LEASE_TIMEOUT_SECONDS: '3',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      },
    );
    this.child = child;
    const remember = (chunk) => {
      this.output.push(String(chunk));
      if (this.output.length > 80) this.output.splice(0, this.output.length - 80);
    };
    child.stdout.on('data', remember);
    child.stderr.on('data', remember);

    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) {
        this.child = null;
        throw new Error(`Brain exited during startup.\n${this.recentOutput()}`);
      }
      try {
        const response = await fetch(`${BRAIN_LOOPBACK_URL}/health`, { cache: 'no-store' });
        if (response.ok) return;
      } catch {
        // The listener is not ready yet.
      }
      await delay(100);
    }
    await this.stop();
    throw new Error(`Brain did not become healthy.\n${this.recentOutput()}`);
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
      throw new Error(`Tracked Brain process stopped but port ${BRAIN_PORT} remains occupied.`);
    }
  }

  recentOutput() {
    return this.output.join('').trim();
  }
}
