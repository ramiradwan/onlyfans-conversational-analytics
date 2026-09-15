import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import http from 'node:http';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import { build } from 'esbuild';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const temporary = await mkdtemp(path.join(tmpdir(), 'ofca-companion-origin-'));
const extension = path.join(temporary, 'extension');
const sentinel = 'qualification-full-secret-must-stay-local';
const seen = [];
const peers = new Set();
let context;
const server = http.createServer((_request, response) => { response.writeHead(404); response.end(); });
server.on('upgrade', (request, socket) => {
  peers.add(socket);
  socket.on('close', () => peers.delete(socket));
  const entry = {
    path: request.url, host: request.headers.host,
    cookie: Boolean(request.headers.cookie), authorization: Boolean(request.headers.authorization),
    subprotocol: Boolean(request.headers['sec-websocket-protocol']), leaked: false, bytes: 0,
  };
  seen.push(entry);
  const accept = createHash('sha1').update(request.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let buffer = Buffer.alloc(0);
  socket.on('data', (part) => {
    buffer = Buffer.concat([buffer, part]);
    while (buffer.length >= 2) {
      const opcode = buffer[0] & 15;
      let size = buffer[1] & 127, offset = 2;
      if (size === 126) { if (buffer.length < 4) return; size = buffer.readUInt16BE(2); offset = 4; }
      if (size === 127 || size > 36_864) { socket.destroy(); return; }
      const masked = Boolean(buffer[1] & 128);
      if (!masked || buffer.length < offset + 4 + size) return;
      const mask = buffer.subarray(offset, offset + 4); offset += 4;
      const payload = Buffer.from(buffer.subarray(offset, offset + size));
      for (let index = 0; index < payload.length; index++) payload[index] ^= mask[index % 4];
      buffer = buffer.subarray(offset + size);
      if (opcode === 8) { socket.end(); return; }
      entry.bytes += payload.length;
      entry.leaked ||= payload.includes(sentinel);
      // A process holding the port can complete WebSocket admission, but this
      // unauthenticated application message must never admit a Noise session.
      socket.write(Buffer.from([0x81, 2, 123, 125]));
    }
  });
});

try {
  await cp(path.join(root, 'dist'), extension, { recursive: true });
  const originalManifest = await readFile(path.join(extension, 'manifest.json'));
  const manifest = JSON.parse(originalManifest);
  assert.deepEqual(manifest.optional_host_permissions, ['https://onlyfans.com/*']);
  assert.match(manifest.content_security_policy.extension_pages, /ws:\/\/127\.0\.0\.1:17871/);
  await build({
    stdin: {
      resolveDir: root,
      contents: `import {loadPackagedSnow} from './runtime/packaged-snow.mjs';
        import {openCompanionChannel,openLoopbackSocket} from './transport/companion-channel.mjs';
        globalThis.qualifyOrigin=async()=>{
          const snow=await loadPackagedSnow();
          let refused=false;
          const local=snow.generateStaticKeypair(), peer=snow.generateStaticKeypair();
          const store={onInvalidate:()=>()=>{},sessionMaterial:async()=>({
            identity:{creator_account_id:${JSON.stringify(sentinel)}},
            privateKey:local.slice(0,32),peerKey:peer.slice(32),pairingDigest:new Uint8Array(32),
            pairingId:'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',commit:null
          })};
          try { await openCompanionChannel({url:'ws://127.0.0.1:17871/ws/agent',store,
            SnowSession:snow.SnowSession,accountId:${JSON.stringify(sentinel)},trust:{}}); }
          catch { refused=true; }
          local.fill(0);peer.fill(0);
          const wire=await openLoopbackSocket('ws://127.0.0.1:17871/ws/agent/pairing',{text:true});
          await wire.send(JSON.stringify({type:'pair.request',public:true}));
          await wire.receive();wire.close();
          return {refused,wasm:true};
        }; chrome.runtime.onMessage.addListener(()=>{});`,
    }, bundle: true, format: 'esm', outfile: path.join(extension, 'origin-qualification.mjs'),
  });
  await writeFile(path.join(extension, 'manifest.json'), JSON.stringify({ ...manifest,
    background: { service_worker: 'origin-qualification.mjs', type: 'module' } }));
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(17871, '127.0.0.1', resolve); });
  const executable = process.argv[2];
  context = await chromium.launchPersistentContext(path.join(temporary, 'profile'), {
    ...(executable && executable !== 'playwright' ? { executablePath: executable } : { channel: 'chromium' }),
    headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
  });
  await context.addCookies([
    { name: 'bridge_session_probe', value: sentinel, url: 'http://bridge.localhost:17871/', httpOnly: true, secure: true, sameSite: 'Strict' },
    { name: 'other_loopback_probe', value: sentinel, url: 'http://127.0.0.1:17871/', httpOnly: true, secure: false, sameSite: 'Strict' },
  ]);
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker', { timeout: 15_000 });
  for (let attempt = 0; attempt < 100 && !await worker.evaluate(() => typeof globalThis.qualifyOrigin === 'function'); attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const result = await worker.evaluate(() => globalThis.qualifyOrigin());
  assert.equal(result.refused, true);
  assert.equal(seen.length, 2);
  assert.deepEqual(seen.map((entry) => entry.path).sort(), ['/ws/agent', '/ws/agent/pairing']);
  for (const entry of seen) {
    assert.equal(entry.host, '127.0.0.1:17871');
    assert.equal(entry.cookie || entry.authorization || entry.subprotocol || entry.leaked, false);
    assert.ok(entry.bytes > 0);
  }
  const resultDocument = { schema: 'ofca-companion-origin-qualification/v1',
    browser: context.browser().version(), manifest_sha256: createHash('sha256').update(originalManifest).digest('hex'),
    packaged_wasm: result.wasm, hostile_listener_refused: true, ambient_credentials_absent: true,
    full_secret_absent: true, paths: seen.map(({path: value}) => value),
  };
  if (process.argv[3]) await writeFile(process.argv[3], JSON.stringify(resultDocument, null, 2) + '\n');
  console.log(JSON.stringify(resultDocument));
} finally {
  await context?.close();
  for (const peer of peers) peer.destroy();
  await new Promise((resolve) => server.close(resolve));
  if (!path.resolve(temporary).startsWith(path.resolve(tmpdir()) + path.sep)) throw new Error('unsafe_temporary_path');
  await rm(temporary, { recursive: true, force: true });
}
