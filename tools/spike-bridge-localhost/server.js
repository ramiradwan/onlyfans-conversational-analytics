import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const PRIMARY_PORT = 17871;
export const SECONDARY_PORT = 17872;
export const BRIDGE_HOST = `bridge.localhost:${PRIMARY_PORT}`;
export const BRIDGE_ORIGIN = `http://${BRIDGE_HOST}`;
export const SESSION_VALUE = 'adr-0009-spike-session';
export const SESSION_COOKIE = `__Host-bridge_session=${SESSION_VALUE}`;
export const SET_COOKIE = `${SESSION_COOKIE}; Secure; HttpOnly; SameSite=Strict; Path=/`;

export function isLoopbackAddress(address) {
  if (!address) return false;
  const normalized = address.toLowerCase();
  return normalized === '::1'
    || normalized.startsWith('127.')
    || normalized.startsWith('::ffff:127.');
}

function cookieContainsSession(cookieHeader) {
  return String(cookieHeader || '')
    .split(';')
    .map((part) => part.trim())
    .includes(SESSION_COOKIE);
}

function sendJson(response, statusCode, value, extraHeaders = {}) {
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...extraHeaders,
  });
  response.end(`${JSON.stringify(value)}\n`);
}

function sendHtml(response, title) {
  response.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'self'; connect-src http://bridge.localhost:17871 http://bridge.localhost:17872 http://127.0.0.1:17871; script-src 'none'; worker-src 'self'; object-src 'none'; base-uri 'none'",
  });
  response.end(`<!doctype html><meta charset="utf-8"><title>${title}</title><h1>${title}</h1>`);
}

function listen(server, host, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off('listening', onListening);
      reject(error);
    };
    const onListening = () => {
      server.off('error', onError);
      resolve();
    };
    server.once('error', onError);
    server.once('listening', onListening);
    server.listen({ host, port, exclusive: true });
  });
}

function close(server) {
  return new Promise((resolve) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close(() => resolve());
    server.closeAllConnections?.();
  });
}

function isIpv6Unavailable(error) {
  return ['EADDRNOTAVAIL', 'EAFNOSUPPORT', 'EPROTONOSUPPORT'].includes(error?.code);
}

export class BridgeSpikeHarness {
  constructor({ includeSecondary = true } = {}) {
    this.includeSecondary = includeSecondary;
    this.requests = [];
    this.servers = [];
    this.bindings = [];
  }

  record(request) {
    const entry = {
      sequence: this.requests.length + 1,
      timestamp: new Date().toISOString(),
      method: request.method,
      url: request.url,
      host: request.headers.host ?? null,
      origin: request.headers.origin ?? null,
      cookie: request.headers.cookie ?? null,
      userAgent: request.headers['user-agent'] ?? null,
      remoteAddress: request.socket.remoteAddress ?? null,
      remotePort: request.socket.remotePort ?? null,
      localAddress: request.socket.localAddress ?? null,
      localPort: request.socket.localPort ?? null,
    };
    this.requests.push(entry);
    return entry;
  }

  handle(request, response) {
    const observed = this.record(request);
    const requestUrl = new URL(request.url, 'http://spike.invalid');

    if (requestUrl.pathname === '/sw.js') {
      response.writeHead(200, {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Cache-Control': 'no-store',
        'Service-Worker-Allowed': '/',
      });
      response.end("self.addEventListener('install', () => self.skipWaiting()); self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));");
      return;
    }

    if (requestUrl.pathname === '/set-cookie') {
      response.writeHead(204, {
        'Cache-Control': 'no-store',
        'Set-Cookie': SET_COOKIE,
      });
      response.end();
      return;
    }

    if (requestUrl.pathname === '/protected') {
      const hostValid = request.headers.host === BRIDGE_HOST;
      const originValid = request.headers.origin === BRIDGE_ORIGIN;
      const cookiePresent = cookieContainsSession(request.headers.cookie);
      const methodValid = request.method === 'POST';
      const accepted = hostValid && originValid && cookiePresent && methodValid;
      observed.validation = {
        accepted,
        hostValid,
        originValid,
        cookiePresent,
        methodValid,
      };
      observed.responseStatus = accepted ? 200 : 403;
      sendJson(response, observed.responseStatus, {
        accepted,
        reason: accepted ? 'exact-host-origin-cookie' : 'rejected-by-exact-host-origin-validation',
      }, accepted ? {
        'Access-Control-Allow-Origin': BRIDGE_ORIGIN,
        'Access-Control-Allow-Credentials': 'true',
      } : {});
      return;
    }

    if (requestUrl.pathname === '/echo' || requestUrl.pathname === '/health') {
      sendJson(response, 200, {
        product: 'bridge-localhost-adr-0009-spike',
        host: observed.host,
        origin: observed.origin,
        cookie: observed.cookie,
        remoteAddress: observed.remoteAddress,
        localAddress: observed.localAddress,
        localPort: observed.localPort,
      });
      return;
    }

    if (requestUrl.pathname === '/' || requestUrl.pathname === '/attacker') {
      sendHtml(response, requestUrl.pathname === '/' ? 'Bridge localhost spike' : 'Cross-origin test page');
      return;
    }

    sendJson(response, 404, { error: 'not-found' });
  }

  async bindPort(port) {
    for (const host of ['127.0.0.1', '::1']) {
      const server = http.createServer((request, response) => this.handle(request, response));
      try {
        await listen(server, host, port);
        this.servers.push(server);
        this.bindings.push({ host, port });
      } catch (error) {
        await close(server);
        if (host === '::1' && isIpv6Unavailable(error)) {
          this.bindings.push({ host, port, unavailable: true, code: error.code });
          continue;
        }
        throw error;
      }
    }
  }

  async start() {
    try {
      await this.bindPort(PRIMARY_PORT);
      if (this.includeSecondary) await this.bindPort(SECONDARY_PORT);
      return this;
    } catch (error) {
      await this.stop();
      if (error?.code === 'EADDRINUSE') {
        const conflict = new Error(
          `PORT_CONFLICT: Port ${PRIMARY_PORT} is already in use. Stop the unrelated process that owns ${PRIMARY_PORT}, then retry. The harness refused dynamic fallback and did not terminate the owner.`,
        );
        conflict.code = 'PORT_CONFLICT';
        conflict.cause = error;
        throw conflict;
      }
      throw error;
    }
  }

  async stop() {
    const servers = this.servers.splice(0);
    await Promise.allSettled(servers.map((server) => close(server)));
  }
}

async function runAsService() {
  const harness = new BridgeSpikeHarness({ includeSecondary: false });
  try {
    await harness.start();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = error.code === 'PORT_CONFLICT' ? 2 : 1;
    return;
  }

  process.stdout.write(`HARNESS_READY ${JSON.stringify({ pid: process.pid, bindings: harness.bindings })}\n`);
  const shutdown = async () => {
    await harness.stop();
    process.exit(0);
  };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  await runAsService();
}
