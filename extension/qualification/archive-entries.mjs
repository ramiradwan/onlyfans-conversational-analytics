import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { unzipSync } from 'fflate';

export function readArchiveEntries(bytes, allowedNames = null) {
  assert.ok(bytes.length <= 32 * 1024 * 1024, 'archive exceeds release size limit');
  const names = new Set();
  let total = 0;
  const entries = unzipSync(bytes, { filter(entry) {
    const name = entry.name;
    assert.match(name, /^[a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)*$/, 'unsafe archive path');
    assert.ok(name.split('/').every((part) => part !== '.' && part !== '..'), 'unsafe archive path');
    assert.ok(!names.has(name), `duplicate archive entry: ${name}`);
    if (allowedNames) assert.ok(allowedNames.includes(name), `unexpected archive entry: ${name}`);
    names.add(name);
    total += entry.originalSize;
    assert.ok(names.size <= 64 && total <= 64 * 1024 * 1024, 'expanded archive exceeds release limits');
    return true;
  } });
  assert.deepEqual(Object.keys(entries).sort(), [...names].sort(), 'archive decoder omitted an entry');
  if (allowedNames) assert.deepEqual([...names].sort(), [...allowedNames].sort());
  return entries;
}

export async function extractAuditedArchive(bytes, destination, allowedNames) {
  const entries = readArchiveEntries(bytes, allowedNames);
  const root = path.resolve(destination);
  await mkdir(root); // Require a fresh destination, including no symlink alias.
  for (const [name, content] of Object.entries(entries)) {
    const filename = path.resolve(root, ...name.split('/'));
    assert.ok(filename.startsWith(`${root}${path.sep}`), 'archive escaped extraction root');
    await mkdir(path.dirname(filename), { recursive: true });
    await writeFile(filename, content, { flag: 'wx' });
  }
}
