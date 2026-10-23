// Pure R2 mocks: no account, native Workers runtime or deployment.
import test from 'node:test';
import assert from 'node:assert/strict';
import {serve} from './worker.mjs';

const base='https://updates.dipsy.fi/desktop/v1/';
const core='packages/'+'a'.repeat(64)+'/ConversationAnalytics_1.0.11.0_x64.msixbundle';
const engine='packages/'+'b'.repeat(64)+'/TextAnalysisEngine_1.0.42.0_x64.msixbundle';
const prefix='qualification/'+'c'.repeat(32)+'/';

function bucket() {
  const calls=[];
  return {calls,async head(key){calls.push(['head',key]);return {size:4,httpEtag:'"fixture"',etag:'fixture',uploaded:new Date('2026-01-01Z')};},
    async get(key,options){calls.push(['get',key,options]);return {body:new ReadableStream({start(c){c.enqueue(new Uint8Array(options.range?[1,2]:[1,2,3,4]));c.close();}})};}};
}

for(const key of [core,engine,prefix+core,prefix+engine]) {
  test('canonical bundle streams from its exact bucket: '+key,async()=>{
    const release=bucket(),qualification=bucket(),env={RELEASES:release,QUALIFICATION:qualification};
    const selected=key.startsWith(prefix)?qualification:release;
    const result=await serve(new Request(base+key),env);
    assert.equal(result.status,200);assert.equal(result.headers.get('Content-Type'),'application/msixbundle');
    assert.equal(result.headers.get('X-Content-Type-Options'),'nosniff');
    assert.equal(result.headers.get('Cache-Control'),key.startsWith(prefix)?'no-store, no-transform':'public, max-age=31536000, immutable, no-transform');
    assert.deepEqual([...new Uint8Array(await result.arrayBuffer())],[1,2,3,4]);
    assert.deepEqual(selected.calls[1],['get',key,{onlyIf:{etagMatches:'fixture'}}]);
    assert.equal((selected===release?qualification:release).calls.length,0);
  });
}

test('bundle HEAD and Range preserve inspected metadata and conditional reads',async()=>{
  const release=bucket(),env={RELEASES:release};
  const head=await serve(new Request(base+engine,{method:'HEAD',headers:{Range:'bytes=0-1'}}),env);
  assert.equal(head.status,200);assert.equal(head.headers.get('Content-Length'),'4');
  assert.equal((await head.arrayBuffer()).byteLength,0);assert.equal(release.calls.length,1);
  const range=await serve(new Request(base+engine,{headers:{Range:'bytes=0-1'}}),env);
  assert.equal(range.status,206);assert.equal(range.headers.get('Content-Range'),'bytes 0-1/4');
  assert.deepEqual([...new Uint8Array(await range.arrayBuffer())],[1,2]);
  assert.deepEqual(release.calls.at(-1)[2],{onlyIf:{etagMatches:'fixture'},range:{offset:0,length:2}});
});

for(const key of [engine.replace('TextAnalysisEngine_','engine_'),engine.replace('1.0.42.0','1.00.42.0'),
  engine.replace('.msixbundle','.msix'),engine.replace('b'.repeat(64),'B'.repeat(64)),
  engine.replace('b'.repeat(64),'b'.repeat(63)),prefix+engine+'?x=1',
  prefix.replace('c'.repeat(32),'c'.repeat(31))+engine,engine.replace('Engine_','Engine%5f')]) {
  test('noncanonical bundle path is refused before bucket access: '+key,async()=>{
    const release=bucket(),qualification=bucket();
    const result=await serve(new Request(base+key),{RELEASES:release,QUALIFICATION:qualification});
    assert.ok([400,404].includes(result.status));assert.equal(release.calls.length+qualification.calls.length,0);
  });
}

test('existing feed, standalone package and qualification paths remain available',async()=>{
  for(const key of ['windows/x64/stable/ConversationAnalytics.appinstaller',core.replace('.msixbundle','.msix'),
    prefix+'probe.msix',prefix+'probe.appinstaller',prefix+'bytes.bin']) {
    const result=await serve(new Request(base+key,{method:'HEAD'}),{RELEASES:bucket(),QUALIFICATION:bucket()});
    assert.equal(result.status,200);
  }
});
