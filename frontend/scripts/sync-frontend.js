/**  
 * sync-frontend.js  
 *  
 * - Fetch backend OpenAPI spec → generate `endpoints.ts`  
 * - Sync `tsconfig.json` paths from `aliases.config.js` (inline arrays formatting)  
 * - Patch only the `server.proxy` keys in `vite.config.ts`  
 */  
  
import fs from 'fs';  
import path from 'path';  
import { fileURLToPath } from 'url';  
import stripJsonComments from 'strip-json-comments';  
import { tsPaths } from './aliases.config.js';  
  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
// --- Config ---  
const API_URL = process.env.API_URL || 'http://localhost:8000/openapi.json';  
const viteConfigPath = path.resolve(__dirname, '../vite.config.ts');  
const endpointsOutPath = path.resolve(__dirname, '../src/config/endpoints.ts');  
const tsconfigPath = path.resolve(__dirname, '../tsconfig.json');  
  
// --- Helper: Build constant name from path ---  
function toConstName(str) {  
  let name = str  
    .replace(/^\/+/, '')  
    .replace(/[{}]/g, '')  
    .replace(/[-/]/g, '_')  
    .replace(/_+/g, '_')  
    .replace(/^_+|_+$/g, '')  
    .toUpperCase();  
  if (!name) name = 'ROOT';  
  if (/^\d/.test(name)) name = '_' + name;  
  return name;  
}  
  
// --- Step 1: Fetch OpenAPI spec ---  
console.log(`[sync-frontend] Fetching OpenAPI spec from ${API_URL}...`);  
const res = await fetch(API_URL);  
if (!res.ok) {  
  console.error(`[sync-frontend] ❌ Failed to fetch OpenAPI from ${API_URL} (${res.status})`);  
  process.exit(1);  
}  
const openapi = await res.json();  
  
// --- Step 2: Generate endpoints.ts ---  
const restConsts = Object.entries(openapi.paths || {}).map(([pathKey, methods]) => {  
  const firstMethod = Object.keys(methods)[0];  
  const op = methods[firstMethod] || {};  
  let name;  
  if (op.operationId) {  
    name = op.operationId.replace(/([a-z])([A-Z])/g, '$1_$2').toUpperCase();  
  } else {  
    name = toConstName(pathKey);  
  }  
  return { name, value: pathKey };  
});  
  
const wsEndpoints = [  
  { name: 'WS_EXTENSION', value: (userId) => `/ws/extension/${userId}` },  
  { name: 'WS_FRONTEND', value: (userId) => `/ws/frontend/${userId}` },  
  { name: 'WS_CHATWOOT', value: (userId) => `/ws/chatwoot/${userId}` },  
];  
  
const endpointsFile = `// ⚠️ AUTO-GENERATED FILE — DO NOT EDIT  
// Generated from ${API_URL} on ${new Date().toISOString()}  
  
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';  
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || 'ws://localhost:8000';  
  
// --- REST API Endpoints ---  
${restConsts.map(({ name, value }) => `export const ${name} = \`${value}\`;`).join('\n')}  
  
// --- WebSocket Endpoints ---  
${wsEndpoints.map(({ name, value }) => `export const ${name} = (userId: string) => \`${value('${userId}')}\`;`).join('\n')}  
`;  
  
fs.writeFileSync(endpointsOutPath, endpointsFile, 'utf8');  
console.log(`[sync-frontend] ✅ Generated endpoints.ts (${restConsts.length} REST + ${wsEndpoints.length} WS endpoints)`);  
  
// --- Step 3: Sync tsconfig.json paths ---  
console.log('[sync-frontend] Syncing tsconfig.json paths...');  
const tsconfig = JSON.parse(stripJsonComments(fs.readFileSync(tsconfigPath, 'utf-8')));  
tsconfig.compilerOptions = tsconfig.compilerOptions || {};  
tsconfig.compilerOptions.baseUrl = './src';  
tsconfig.compilerOptions.paths = tsPaths;  
  
if (!tsconfig.include) tsconfig.include = [];  
if (!tsconfig.include.includes('src/types/mui.d.ts')) {  
  tsconfig.include.unshift('src/types/mui.d.ts');  
}  
  
// Create a normal JSON string  
let tsconfigStr = JSON.stringify(tsconfig, null, 2);  
  
// Replace the "paths" block with inline arrays formatting  
const pathsInline = Object.entries(tsPaths)  
  .map(([key, arr]) => `    "${key}": ["${arr[0]}"]`)  
  .join(',\n');  
  
tsconfigStr = tsconfigStr.replace(  
  /"paths": \{[^}]+\}/m,  
  `"paths": {\n${pathsInline}\n  }`  
);  
  
fs.writeFileSync(tsconfigPath, tsconfigStr + '\n', 'utf8');  
console.log('✅ tsconfig.json paths synced (inline arrays)');  
  
// --- Step 4: Patch vite.config.ts proxy keys ---  
if (fs.existsSync(viteConfigPath)) {  
  console.log('[sync-frontend] Patching vite.config.ts proxy keys...');  
  let viteConfigContent = fs.readFileSync(viteConfigPath, 'utf8');  
  
  const wssMatch = endpointsFile.match(/export const GET_WSS_SCHEMA\s*=\s*`([^`]+)`/);  
  const wssPath = wssMatch ? wssMatch[1] : '/api/v1/schemas/wss';  
  
  viteConfigContent = viteConfigContent.replace(  
    /\$\s*GET_WSS_SCHEMA\s*$|'\/api\/v\d+\/schemas\/wss'/,  
    `'${wssPath}'`  
  );  
  
  fs.writeFileSync(viteConfigPath, viteConfigContent, 'utf8');  
  console.log(`✅ Patched vite.config.ts proxy key for GET_WSS_SCHEMA → '${wssPath}'`);  
} else {  
  console.warn('⚠️ vite.config.ts not found — skipping proxy patch.');  
}  
  
console.log('[sync-frontend] All tasks complete.');  