import { execFileSync } from 'node:child_process'; import { writeFile } from 'node:fs/promises';
const metadata=JSON.parse(execFileSync('cargo',['metadata','--format-version','1','--locked','--filter-platform','wasm32-unknown-unknown','--manifest-path','../../crypto/snow/Cargo.toml'],{encoding:'utf8'}));
const used=new Set(metadata.resolve.nodes.map(node=>node.id));
const packages=metadata.packages.filter(p=>used.has(p.id)).map(p=>({name:p.name,version:p.version,license:p.license,source:p.source,repository:p.repository})).sort((a,b)=>a.name.localeCompare(b.name));
await writeFile(new URL('./dependency-metadata.json',import.meta.url),JSON.stringify({generated_by:'cargo metadata',target:'wasm32-unknown-unknown',packages},null,2)+'\n');
console.log(JSON.stringify(packages,null,2));
