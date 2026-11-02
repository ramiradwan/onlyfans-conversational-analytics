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

for(const [suffix,type] of [['catalog.jws','application/jose'],['profiles/'+'d'.repeat(64),'application/jose'],
  ['data/'+'e'.repeat(64),'application/octet-stream']]) {
  const key=prefix+'nlp/'+suffix;
  test('NLP qualification bytes use exact MIME and only the qualification bucket: '+suffix,async()=>{
    const qualification=bucket();
    const env={QUALIFICATION:qualification,get RELEASES(){assert.fail('production bucket selected');}};
    const result=await serve(new Request(base+key,{headers:{'Accept-Encoding':'gzip, br'}}),env);
    assert.equal(result.status,200);assert.equal(result.headers.get('Content-Type'),type);
    assert.equal(result.headers.get('Content-Encoding'),'identity');
    assert.equal(result.headers.get('Content-Length'),'4');
    assert.equal(result.headers.get('Cache-Control'),'no-store, no-transform');
    assert.equal(result.headers.get('X-Content-Type-Options'),'nosniff');
    assert.equal(result.headers.get('Accept-Ranges'),'bytes');
    assert.deepEqual([...new Uint8Array(await result.arrayBuffer())],[1,2,3,4]);
    assert.deepEqual(qualification.calls,[['head',key],['get',key,{onlyIf:{etagMatches:'fixture'}}]]);
  });
  test('NLP HEAD, Range and conditionals retain the ordinary read contract: '+suffix,async()=>{
    const qualification=bucket(),env={QUALIFICATION:qualification};
    const head=await serve(new Request(base+key,{method:'HEAD',headers:{Range:'bytes=0-1'}}),env);
    assert.equal(head.status,200);assert.equal(head.headers.get('Content-Type'),type);
    assert.equal(head.headers.get('Content-Length'),'4');assert.equal(head.headers.get('Content-Range'),null);
    assert.equal((await head.arrayBuffer()).byteLength,0);
    assert.deepEqual(qualification.calls,[['head',key]]);
    const range=await serve(new Request(base+key,{headers:{Range:'bytes=0-1','If-Range':'"fixture"'}}),env);
    assert.equal(range.status,206);assert.equal(range.headers.get('Content-Type'),type);
    assert.equal(range.headers.get('Content-Range'),'bytes 0-1/4');
    assert.equal(range.headers.get('Cache-Control'),'no-store, no-transform');
    assert.deepEqual([...new Uint8Array(await range.arrayBuffer())],[1,2]);
    assert.deepEqual(qualification.calls.at(-1),['get',key,{onlyIf:{etagMatches:'fixture'},range:{offset:0,length:2}}]);
    for(const [headers,status] of [[{'If-None-Match':'"fixture"'},304],[{'If-Match':'"other"'},412],
      [{Range:'bytes=4-'},416]]) {
      qualification.calls.length=0;
      const result=await serve(new Request(base+key,{headers}),env);
      assert.equal(result.status,status);assert.deepEqual(qualification.calls,[['head',key]]);
    }
  });
  test('NLP missing qualification storage cannot fall back to production: '+suffix,async()=>{
    const release=bucket();
    assert.equal((await serve(new Request(base+key),{RELEASES:release})).status,503);
    assert.equal((await serve(new Request(base+key),{RELEASES:release,QUALIFICATION:{head:async()=>null}})).status,404);
    assert.equal(release.calls.length,0);
  });
}

for(const suffix of ['catalog.jws','profiles/'+'d'.repeat(64),'data/'+'e'.repeat(64)]) {
  test('NLP object replacement cannot mix inspected headers and body: '+suffix,async()=>{
    const key=prefix+'nlp/'+suffix,calls=[];
    const env={QUALIFICATION:{
      async head(){const n=calls.length/2+1;calls.push('head');return {size:4,httpEtag:`"v${n}"`,etag:`v${n}`,uploaded:new Date('2026-01-01Z')};},
      async get(name,options){calls.push('get');assert.equal(name,key);assert.equal(options.onlyIf.etagMatches,`v${calls.length/2}`);return {};},
    }};
    const result=await serve(new Request(base+key),env);
    assert.equal(result.status,503);assert.deepEqual(calls,['head','get','head','get']);
  });
}

const nlpPrefix=prefix+'nlp/';
for(const key of ['nlp/catalog.jws','nlp/profiles/'+'d'.repeat(64),'nlp/data/'+'e'.repeat(64),
  prefix+'catalog.jws',prefix+'profiles/'+'d'.repeat(64),nlpPrefix+'catalog.json',nlpPrefix+'catalog.jws/extra',
  nlpPrefix+'profiles/'+'d'.repeat(63),nlpPrefix+'data/'+'e'.repeat(65),nlpPrefix+'data/'+'E'.repeat(64),
  prefix.replace('c'.repeat(32),'C'.repeat(32))+'nlp/catalog.jws',prefix.replace('c'.repeat(32),'c'.repeat(31))+'nlp/catalog.jws',
  prefix+'NLP/catalog.jws',nlpPrefix+'profile/'+'d'.repeat(64),nlpPrefix+'data/'+'e'.repeat(64)+'/extra',
  nlpPrefix+'data/%65'+'e'.repeat(63),nlpPrefix+'profiles%2f'+'d'.repeat(64),nlpPrefix+'catalog.jws?download=1',
  nlpPrefix+'data//'+ 'e'.repeat(64),prefix+'nlp/../unknown',
  '../signing-parameters/v1/catalog.jws']) {
  test('unsafe or out-of-scope NLP path is refused before storage: '+key,async()=>{
    const release=bucket(),qualification=bucket();
    const result=await serve(new Request(base+key),{RELEASES:release,QUALIFICATION:qualification});
    assert.ok([400,404].includes(result.status));assert.equal(release.calls.length+qualification.calls.length,0);
  });
}

for(const method of ['PUT','POST','DELETE','PATCH','OPTIONS']) {
  test('NLP methods cannot reach storage: '+method,async()=>{
    const release=bucket(),qualification=bucket();
    const result=await serve(new Request(base+nlpPrefix+'catalog.jws',{method}),{RELEASES:release,QUALIFICATION:qualification});
    assert.equal(result.status,405);assert.equal(result.headers.get('Allow'),'GET, HEAD');
    assert.equal(release.calls.length+qualification.calls.length,0);
  });
}
