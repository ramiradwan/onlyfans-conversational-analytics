import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { request as httpRequest } from 'node:http';
import net from 'node:net';
import path from 'node:path';

import { PRODUCT_ROOT, pythonExecutable } from './paths.mjs';
import { createSessionFailureCollector } from './stable-connection-diagnostic.mjs';
import { createStartupObservations } from './startup-diagnostics.mjs';

const BRAIN_HOST = '127.0.0.1';
export const BRAIN_PORT = 17_871;
export const BRAIN_ORIGIN = `http://bridge.localhost:${BRAIN_PORT}`;
export const BRAIN_LOOPBACK_URL = `http://${BRAIN_HOST}:${BRAIN_PORT}`;
export const BRAIN_HTTP_URL = BRAIN_ORIGIN;
const E2E_BRAIN_ENTRY = path.join(
  PRODUCT_ROOT,
  'tools',
  'e2e-capture',
  'helpers',
  'brain_entry.py',
);
const TEXT_ANALYSIS_FIXTURES = new Set(['joint', 'split-partial', 'empty', 'saved-only']);
const FIXTURE_SHUTDOWN_PHASES = new Set(['command_seen', 'control_validated', 'control_rejected',
  'server_shutdown_entered', 'application_shutdown_entered', 'application_shutdown_returned',
  'server_returned', 'owner_close_returned', 'fixture_coroutine_returning', 'event_loop_returned',
  'server_tasks_wait_entered', 'server_waiting_connections', 'server_waiting_tasks',
  'server_listener_wait_entered', 'server_listener_wait_returned', 'server_tasks_wait_returned']);

function bootstrapConfigRevision() {
  const source = readFileSync(
    path.join(PRODUCT_ROOT, 'app', 'services', 'agent_configuration.py'),
    'utf8',
  );
  const match = /^BOOTSTRAP_CONFIG_REVISION = "([^"]+)"$/m.exec(source);
  if (match === null) {
    throw new Error('Unable to read BOOTSTRAP_CONFIG_REVISION from the Brain source.');
  }
  return match[1];
}

// Read from the service rather than restated here, so the revision this suite
// waits for cannot drift from the one the service publishes.
export const BOOTSTRAP_CONFIG_REVISION = bootstrapConfigRevision();

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

export function brainHealthIsReady(observe, requestFactory = httpRequest) {
  return new Promise((resolve) => {
    let finished = false; let timedOut = false;
    const complete = (ready, outcome) => {
      if (finished) return;
      finished = true; observe(outcome); resolve(ready);
    };
    const request = requestFactory({
      host: BRAIN_HOST,
      port: BRAIN_PORT,
      path: '/health',
      method: 'GET',
      headers: { Host: new URL(BRAIN_ORIGIN).host },
    }, (response) => {
      response.resume();
      const ready = (response.statusCode ?? 0) >= 200 && (response.statusCode ?? 0) < 300;
      complete(ready, ready ? 'ready' : 'non_2xx');
    });
    request.once('error', error => complete(false, timedOut ? 'timeout'
      : error.code === 'ECONNREFUSED' ? 'connection_refused' : 'connection_error'));
    request.setTimeout(500, () => { timedOut = true; request.destroy(); });
    request.end();
  });
}

async function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return Promise.race([
    new Promise((resolve) => child.once('exit', () => resolve(true))),
    delay(timeoutMs).then(() => false),
  ]);
}

function fixtureShutdownFailure(output) {
  const keys = ['owner_drained', 'reader_alive', 'runtime_drained', 'schema', 'stop_valid'];
  let failure = null;
  for (const raw of output.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (line.length > 256 || !line.startsWith('{"schema":"browser-fixture-shutdown-failure.v1",')) continue;
    try {
      const value = JSON.parse(line);
      if (value !== null && typeof value === 'object' && !Array.isArray(value)
          && Object.keys(value).sort().join(',') === keys.join(',')
          && value.schema === 'browser-fixture-shutdown-failure.v1'
          && keys.filter(key => key !== 'schema').every(key => typeof value[key] === 'boolean')) {
        failure = value;
      }
    } catch { /* Unrecognized output is not diagnostic evidence. */ }
  }
  return failure;
}

export function lastFixtureShutdownPhase(output) {
  if (typeof output !== 'string') return null;
  let tail = output.slice(-65_536);
  if (tail.length !== output.length) {
    const end = tail.indexOf('\n');
    if (end === -1) return null;
    tail = tail.slice(end + 1);
  }
  let phase = null;
  for (const raw of tail.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (line.length > 256 || !line.startsWith('{"schema":"browser-fixture-shutdown-phase.v1",')) continue;
    try {
      const value = JSON.parse(line);
      if (value !== null && typeof value === 'object' && !Array.isArray(value)
          && Object.keys(value).sort().join(',') === 'phase,schema'
          && value.schema === 'browser-fixture-shutdown-phase.v1'
          && FIXTURE_SHUTDOWN_PHASES.has(value.phase)
          && line === JSON.stringify({ schema: value.schema, phase: value.phase })) phase = value.phase;
    } catch { /* Only a complete closed phase record is evidence. */ }
  }
  return phase;
}

export class BrainProcess {
  constructor({
    authDatabasePath,
    canonicalDatabasePath,
    extensionId,
    projectionDatabasePath,
    // Values a provisioned installation already decided. They replace the
    // harness defaults below so the runtime boots on the configuration
    // provisioning wrote rather than on one this class invents.
    environmentOverrides = {},
    textAnalysisFixture = null,
  }) {
    if (textAnalysisFixture !== null && !TEXT_ANALYSIS_FIXTURES.has(textAnalysisFixture)) {
      throw new Error('Unknown text analysis fixture.');
    }
    Object.defineProperty(this, 'textAnalysisFixture', { value: textAnalysisFixture });
    this.authDatabasePath = authDatabasePath;
    this.canonicalDatabasePath = canonicalDatabasePath;
    this.extensionId = extensionId;
    this.localSessionBootstrapToken = environmentOverrides.LOCAL_SESSION_BOOTSTRAP_TOKEN
      ?? randomBytes(32).toString('base64url');
    this.localPrincipalId = environmentOverrides.LOCAL_PRINCIPAL_ID ?? 'e2e-local-principal';
    this.projectionDatabasePath = projectionDatabasePath;
    this.environmentOverrides = environmentOverrides;
    this.child = null;
    this.output = [];
    this.failureCollector = createSessionFailureCollector();
  }

  async start() {
    if (this.child !== null) throw new Error('Brain is already running.');
    if (await portAcceptsConnections(BRAIN_PORT)) {
      throw new Error(`Port ${BRAIN_PORT} is already in use; refusing to reuse or stop another process.`);
    }

    const startup = createStartupObservations('brain');
    const child = spawn(
      pythonExecutable(),
      [E2E_BRAIN_ENTRY, ...(this.textAnalysisFixture === null ? []
        : ['--text-analysis-fixture', this.textAnalysisFixture])],
      {
        cwd: PRODUCT_ROOT,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          // The harness declares its runtime mode rather than inheriting one.
          // Key derivation reads this, so the proof reader declares it too.
          ENVIRONMENT: 'test',
          BROADCAST_URL: 'memory://',
          AUTH_DATABASE_PATH: this.authDatabasePath,
          BRIDGE_ORIGIN: BRAIN_ORIGIN,
          CANONICAL_PERSISTENCE_BACKEND: 'sqlite',
          CANONICAL_DATABASE_PATH: this.canonicalDatabasePath,
          PROJECTION_DATABASE_PATH: this.projectionDatabasePath,
          EXTENSION_ID: this.extensionId,
          WEBSOCKET_AUTH_MODE: 'local_session',
          WEBSOCKET_BIND_HOST: BRAIN_HOST,
          LOCAL_SESSION_BOOTSTRAP_TOKEN: this.localSessionBootstrapToken,
          LOCAL_PRINCIPAL_ID: this.localPrincipalId,
          LOCAL_BRIDGE_ROLE: 'creator',
          AGENT_HEARTBEAT_INTERVAL_SECONDS: '1',
          AGENT_LEASE_TIMEOUT_SECONDS: '3',
          ...(this.textAnalysisFixture === null ? {} : {
            IDENTITY_BINDING_SOURCE: 'verified_grants',
            ANALYTICS_PROJECTION_DATABASE_PATH: path.join(
              path.dirname(this.authDatabasePath), 'analytics-projections.sqlite3',
            ),
          }),
          ...this.environmentOverrides,
        },
        stdio: [this.textAnalysisFixture === null ? 'ignore' : 'pipe', 'pipe', 'pipe'],
        windowsHide: true,
      },
    );
    startup.bind(child);
    this.child = child;
    const remember = chunk => this.rememberOutput(chunk);
    child.stdout.on('data', remember);
    child.stderr.on('data', chunk => this.rememberOutput(chunk, false));

    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) {
        this.child = null;
        throw new Error(`Brain startup failed: ${JSON.stringify(startup.snapshot('startup_exit'))}`);
      }
      if (await brainHealthIsReady(startup.health)) return;
      await delay(100);
    }
    try {
      await this.stop();
    } catch (error) {
      if (this.textAnalysisFixture === null) throw error;
      throw new Error(`Brain startup failed: ${JSON.stringify(startup.snapshot('unhealthy'))}`, { cause: error });
    }
    throw new Error(`Brain startup failed: ${JSON.stringify(startup.snapshot('unhealthy'))}`);
  }

  async stop() {
    const child = this.child;
    if (child === null) return;
    if (this.textAnalysisFixture !== null) return this.stopTextAnalysisFixture(child);
    this.child = null;
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

  async stopTextAnalysisFixture(child) {
    const started = Date.now();
    let graceful = child.exitCode === null && child.signalCode === null
      && Boolean(child.stdin?.writable) && !child.stdin.destroyed;
    const controlEligible = graceful;
    let controlError = 'none';
    if (graceful) {
      child.stdin.once('error', error => {
        graceful = false;
        const code = error?.code;
        controlError = ['EPIPE', 'ECONNRESET', 'ERR_STREAM_DESTROYED', 'ERR_STREAM_WRITE_AFTER_END'].includes(code)
          ? code : 'other';
      });
      child.stdin.end('stop\n');
    }
    let exited = await waitForExit(child, 8_000);
    const timedOut = !exited;
    if (!exited) {
      graceful = false;
      child.kill('SIGKILL');
      exited = await waitForExit(child, 3_000);
    }
    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline && await portAcceptsConnections(BRAIN_PORT)) {
      await delay(100);
    }
    const occupied = await portAcceptsConnections(BRAIN_PORT);
    if (exited && !occupied) this.child = null;
    if (!exited || occupied || !graceful || child.exitCode !== 0 || child.signalCode !== null) {
      const code = child.exitCode, signal = child.signalCode;
      const failure = {
        schema: 'browser-fixture-owned-shutdown-failure.v1',
        elapsed_ms: Math.min(1_000_000, Math.max(0, Date.now() - started)),
        timed_out: timedOut, exited, occupied, control_eligible: controlEligible, graceful,
        exit_code: Number.isSafeInteger(code) && code >= -2_147_483_648 && code <= 4_294_967_295 ? code : null,
        signal: signal === null ? null
          : ['SIGTERM', 'SIGKILL', 'SIGINT', 'SIGBREAK', 'SIGHUP', 'SIGABRT'].includes(signal) ? signal : 'other',
        control_error: controlError,
        child_failure: fixtureShutdownFailure(this.recentOutput()),
        last_phase: lastFixtureShutdownPhase(this.output.join('')),
      };
      throw new Error(`Text analysis fixture did not complete its owned shutdown. ${JSON.stringify(failure)}`);
    }
  }

  recentOutput() {
    return this.output.join('').trim();
  }

  rememberOutput(chunk, sessionOutput = true) {
    this.output.push(String(chunk));
    if (this.output.length > 80) this.output.splice(0, this.output.length - 80);
    if (sessionOutput) this.failureCollector.append(chunk);
  }

  sessionFailures() {
    return this.failureCollector.snapshot();
  }
}
