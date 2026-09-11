import init, { SnowSession } from './pkg/ofca_snow_wasm_spike.js';
import { NoiseSession, connectInitiator } from './noise-session.mjs';
import { receiptBinding, spikePrologue, fromHex, toHex } from './noise-binding.mjs';
import { FIXED_CLAIMS, EXPECTED_BINDING_HEX, AGENT_PRIVATE_HEX, BRAIN_PUBLIC_HEX, APP_PAYLOAD, APP_REPLY } from './fixture.mjs';

// Qualification-only lifecycle listeners: register synchronously so Chrome can
// start/restart this actual MV3 service worker exactly like a shipping worker.
chrome.runtime.onInstalled.addListener(() => {});
chrome.runtime.onStartup.addListener(() => {});

const wasmReady = init();
function message(ws, timeout=2000){ return new Promise((resolve,reject)=>{ const t=setTimeout(()=>reject(new Error('deadline')),timeout); ws.addEventListener('message',e=>{clearTimeout(t);resolve(new Uint8Array(e.data));},{once:true}); ws.addEventListener('error',()=>{clearTimeout(t);reject(new Error('transport'));},{once:true}); }); }
async function makeSession(timeoutMs=2000){ await wasmReady; const binding=await receiptBinding(FIXED_CLAIMS); if(toHex(binding)!==EXPECTED_BINDING_HEX) throw new Error('binding_mismatch'); return new NoiseSession({SnowSession,initiator:true,localPrivateKey:fromHex(AGENT_PRIVATE_HEX),remotePublicKey:fromHex(BRAIN_PUBLIC_HEX),prologue:spikePrologue(binding),timeoutMs}); }
globalThis.__snowSpikeRun = async ({mode}) => {
  if(mode==='fresh'){ const a=await makeSession(); const first=toHex(a.writeHandshake()); a.close(); const b=await makeSession(); const second=toHex(b.writeHandshake()); b.close(); return {first,second,binding:EXPECTED_BINDING_HEX}; }
  const session=await makeSession();
  try {
    const ws=await connectInitiator({url:'ws://127.0.0.1:17871/session-spike',session});
    if(mode==='hostile') throw new Error('hostile_authenticated');
    ws.send(session.seal(APP_PAYLOAD)); const reply=session.open(await message(ws)); ws.close(); session.close();
    return {result:'passed', reply:new TextDecoder().decode(reply), expected:new TextDecoder().decode(APP_REPLY)};
  } catch(e) {
    session.close(); if(mode==='hostile') return {result:'refused',code:e?.code ?? 'transport_failed'}; throw e;
  }
};
