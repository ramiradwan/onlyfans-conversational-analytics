import fs from 'fs';  
import path from 'path';  
import { fileURLToPath } from 'url';  
import stripJsonComments from 'strip-json-comments';  
import { tsPaths } from './aliases.config.js';  
  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
const API_URL = process.env.API_URL || 'http://localhost:8000/openapi.json';  
  
// --- Fetch OpenAPI spec ---  
const res = await fetch(API_URL);  
if (!res.ok) {  
  console.error(`[sync-frontend] ❌ Failed to fetch OpenAPI from ${API_URL}`);  
  process.exit(1);  
}  
const openapi = await res.json();  
  
// --- Helper to make constant names ---  
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
  
// --- Build REST endpoint constants ---  
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
  
// --- Static WS endpoints ---  
const wsEndpoints = [  
  { name: 'WS_EXTENSION', value: (userId) => `/ws/extension/${userId}` },  
  { name: 'WS_FRONTEND', value: (userId) => `/ws/frontend/${userId}` },  
  { name: 'WS_CHATWOOT', value: (userId) => `/ws/chatwoot/${userId}` }  
];  
  
// --- Generate endpoints.ts content ---  
let endpointsFile = `// ⚠️ AUTO-GENERATED FILE — DO NOT EDIT  
// Generated from ${API_URL} on ${new Date().toISOString()}  
  
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';  
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || 'ws://localhost:8000';  
  
// --- REST API Endpoints ---  
${restConsts.map(({ name, value }) => `export const ${name} = \`${value}\`;`).join('\n')}  
  
// --- WebSocket Endpoints ---  
${wsEndpoints.map(({ name, value }) => `export const ${name} = (userId: string) => \`${value('${userId}')}\`;`).join('\n')}  
`;  
  
// --- Write endpoints.ts ---  
const endpointsOutPath = path.resolve(__dirname, '../src/config/endpoints.ts');  
fs.writeFileSync(endpointsOutPath, endpointsFile, 'utf8');  
console.log(`[sync-frontend] ✅ Generated endpoints.ts with ${restConsts.length} REST + ${wsEndpoints.length} WS endpoints`);  
  
// --- Update tsconfig.json ---  
const tsconfigPath = path.resolve(__dirname, '../tsconfig.json');  
const tsconfig = JSON.parse(stripJsonComments(fs.readFileSync(tsconfigPath, 'utf-8')));  
  
tsconfig.compilerOptions = tsconfig.compilerOptions || {};  
tsconfig.compilerOptions.baseUrl = './src';  
tsconfig.compilerOptions.paths = tsPaths;  
  
if (!tsconfig.include) tsconfig.include = [];  
if (!tsconfig.include.includes('src/types/mui.d.ts')) {  
  tsconfig.include.unshift('src/types/mui.d.ts');  
}  
  
// --- Stringify with indentation ---  
let tsconfigString = JSON.stringify(tsconfig, null, 2);  
  
// --- Replace "paths" object with single-line arrays ---  
const oneLinePaths = Object.entries(tsconfig.compilerOptions.paths || {})  
  .map(([alias, arr]) => `    "${alias}": ["${arr[0]}"]`)  
  .join(',\n');  
  
tsconfigString = tsconfigString.replace(  
  /"paths": \{[\s\S]*?\}/m,  
  `"paths": {\n${oneLinePaths}\n  }`  
);  
  
// --- Write updated tsconfig.json ---  
fs.writeFileSync(tsconfigPath, tsconfigString + '\n');  
console.log('✅ tsconfig.json paths synced from aliases.config.js');  