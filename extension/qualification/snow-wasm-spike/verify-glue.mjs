import assert from 'node:assert/strict'; import { readFile, stat } from 'node:fs/promises';
const js=await readFile(new URL('./pkg/ofca_snow_wasm_spike.js',import.meta.url),'utf8');
for(const pattern of [/\beval\s*\(/u,/new\s+Function\b/u,/https?:\/\//u,/import\s*\(\s*[^'"`]/u,/\brequire\s*\(/u,/node:/u]) assert.doesNotMatch(js,pattern);
assert.match(js,/WebAssembly/u); assert.match(js,/new URL\(['"]ofca_snow_wasm_spike_bg\.wasm['"], import\.meta\.url\)/u);
const wasm=(await stat(new URL('./pkg/ofca_snow_wasm_spike_bg.wasm',import.meta.url))).size;
console.log(JSON.stringify({glue_bytes:Buffer.byteLength(js),wasm_bytes:wasm,remote_code:false,eval:false,local_wasm_url:true},null,2));
