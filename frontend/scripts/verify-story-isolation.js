import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const distDirectory = path.resolve(scriptDirectory, '../../app/static/dist');
const forbidden = [
  'STORY ONLY · synthetic values · not product data',
  'story-only-account',
  'Synthetic response showing the conversation bubble width.',
  'src/story-only',
  'VisualHarness.tsx',
];

function filesBelow(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(directory, entry.name);
    return entry.isDirectory() ? filesBelow(target) : [target];
  });
}

if (!fs.existsSync(distDirectory)) {
  throw new Error(`Production bundle directory is missing: ${distDirectory}`);
}

const findings = [];
for (const file of filesBelow(distDirectory)) {
  if (!/\.(?:css|html|js|json)$/i.test(file)) continue;
  const source = fs.readFileSync(file, 'utf8');
  for (const marker of forbidden) {
    if (source.includes(marker)) findings.push(`${path.basename(file)}: ${marker}`);
  }
}

if (findings.length > 0) {
  throw new Error(`Story-only content reached the production bundle:\n${findings.join('\n')}`);
}

console.log('Production bundle contains no story-only fixtures or entry markers');
