import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

// Read generated declarations strictly as data. Never evaluate a candidate bundle.
export function extractStringConstant(source, name) {
  assert.match(name, /^[A-Z][A-Z0-9_]+$/);
  const declarations = [...source.matchAll(new RegExp(`\\b(?:const|let|var)\\s+${name}\\s*=`, 'g'))];
  assert.equal(declarations.length, 1, `expected one ${name} declaration`);
  const literals = [...source.matchAll(new RegExp(`^[ \\t]*(?:export )?(?:const|let|var) ${name} = ("(?:[^"\\\\\\r\\n]|\\\\.)*");[ \\t]*$`, 'gm'))];
  assert.equal(literals.length, 1, `expected a literal ${name} declaration`);
  return JSON.parse(literals[0][1]);
}

export function auditLegalBindingLiterals(source, { canonical, digest }) {
  const encoded = extractStringConstant(source, 'LEGAL_RELEASE_BINDINGS_B64');
  const recorded = extractStringConstant(source, 'LEGAL_RELEASE_BINDINGS_SHA256');
  const bytes = Buffer.from(encoded, 'base64');
  assert.equal(bytes.toString('base64'), encoded, 'Legal binding encoding is not canonical');
  assert.equal(new TextDecoder('utf-8', { fatal: true }).decode(bytes), canonical, 'runtime Legal bindings differ from release input');
  assert.equal(createHash('sha256').update(bytes).digest('hex'), digest);
  assert.equal(recorded, digest, 'runtime Legal binding digest differs from release input');
}
