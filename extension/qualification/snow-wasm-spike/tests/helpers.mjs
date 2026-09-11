import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import init, { SnowSession } from '../pkg/ofca_snow_wasm_spike.js';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let ready;
export async function loadWasm() {
  if (!ready) ready = readFile(path.join(root, 'pkg/ofca_snow_wasm_spike_bg.wasm')).then(bytes => init({module_or_path: bytes}));
  await ready; return SnowSession;
}
