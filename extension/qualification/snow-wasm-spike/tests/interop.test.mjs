import test from 'node:test'; import assert from 'node:assert/strict'; import { spawn } from 'node:child_process'; import path from 'node:path'; import { fileURLToPath } from 'node:url';
import { loadWasm } from './helpers.mjs'; import { NoiseSession } from '../web/noise-session.mjs';
import { receiptBinding, spikePrologue, fromHex, toHex } from '../web/noise-binding.mjs';
import { FIXED_CLAIMS, AGENT_PRIVATE_HEX, AGENT_PUBLIC_HEX, BRAIN_PRIVATE_HEX, BRAIN_PUBLIC_HEX, APP_PAYLOAD, APP_REPLY } from '../web/fixture.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
function lineReader(stream) { let buf=''; const waiters=[]; stream.setEncoding('utf8'); stream.on('data', d => { buf += d; for (;;) { const i=buf.indexOf('\n'); if(i<0) break; const line=buf.slice(0,i); buf=buf.slice(i+1); waiters.shift()?.(line); } }); return () => new Promise(r => { const i=buf.indexOf('\n'); if(i>=0){const l=buf.slice(0,i); buf=buf.slice(i+1); r(l);} else waiters.push(r); }); }
function send(child, frame) { child.stdin.write(`${JSON.stringify({frame: toHex(frame)})}\n`); }
async function exchange(role) {
  const SnowSession = await loadWasm(); const binding = await receiptBinding(FIXED_CLAIMS); const prologue=spikePrologue(binding);
  const child=spawn(process.env.PYTHON ?? 'python3',[path.join(root,'python_peer.py'),'--binding',toHex(binding),'--role',role],{stdio:['pipe','pipe','inherit']}); const next=lineReader(child.stdout);
  const snowRole = role === 'responder' ? 'initiator' : 'responder';
  const session = new NoiseSession({SnowSession, initiator:snowRole==='initiator', localPrivateKey:fromHex(snowRole==='initiator'?AGENT_PRIVATE_HEX:BRAIN_PRIVATE_HEX), remotePublicKey:fromHex(snowRole==='initiator'?BRAIN_PUBLIC_HEX:AGENT_PUBLIC_HEX), prologue});
  if (snowRole==='initiator') { send(child,session.writeHandshake()); session.readHandshake(fromHex(JSON.parse(await next()).frame)); }
  else { session.readHandshake(fromHex(JSON.parse(await next()).frame)); send(child,session.writeHandshake()); }
  session.finishHandshake();
  if (snowRole==='initiator') { send(child,session.writeConfirmation()); session.readConfirmation(fromHex(JSON.parse(await next()).frame)); const c=session.seal(APP_PAYLOAD); send(child,c); assert.deepEqual(session.open(fromHex(JSON.parse(await next()).frame)), APP_REPLY); }
  else { session.readConfirmation(fromHex(JSON.parse(await next()).frame)); send(child,session.writeConfirmation()); const incoming=fromHex(JSON.parse(await next()).frame); assert.deepEqual(session.open(incoming), APP_PAYLOAD); send(child,session.seal(APP_REPLY)); }
  const code=await new Promise(r=>child.on('exit',r)); assert.equal(code,0); session.close();
}
test('snow/WASM initiator interoperates with Python responder',()=>exchange('responder'));
test('Python initiator interoperates with snow/WASM responder',()=>exchange('initiator'));
