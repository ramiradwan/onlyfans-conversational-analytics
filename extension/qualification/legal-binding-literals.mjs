import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

// Approved customer-render sections; surrounding UI controls are separately qualified.
const ACTIVATION_DISCLOSURES = {
  preview: '92642f8a5c46c5026de0b6845dbfdb0c4adbedaa5612d2d634c4d796a02f4fa2',
  full: '5527bb2a4b92d4a3381c77ccbd944ac73379ec4f2b1d01c73e6538b9f7a38151',
  'separate-choices': 'd9b04f4a0f4e015e99092f3d89af1cce0af430fb823b4d97e10a497cbc833837',
};

export function auditActivationDisclosures(html) {
  for (const [name, digest] of Object.entries(ACTIVATION_DISCLOSURES)) {
    const sections = [...html.matchAll(new RegExp(`data-approved-copy="${name}">\\n([\\s\\S]*?)</div>`, 'g'))];
    assert.equal(sections.length, 1, `expected one ${name} disclosure`);
    assert.equal(createHash('sha256').update(sections[0][1]).digest('hex'), digest,
      `${name} disclosure differs from the approved rendering`);
  }
}

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
