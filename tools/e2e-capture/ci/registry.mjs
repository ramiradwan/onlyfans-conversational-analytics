import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const E2E_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const REGISTRY_PATH = path.join(E2E_ROOT, 'ci', 'registry.json');
export const LANES = ['core', 'catchup', 'legacy'];
export const EVENT_SCHEMA = 'browser-ci-events/v1';
export const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
export const exactKeys = (value, keys) => value && typeof value === 'object'
  && !Array.isArray(value) && Object.keys(value).sort().join('\0') === [...keys].sort().join('\0');

export function readRegistry(filename = REGISTRY_PATH) {
  const bytes = readFileSync(filename);
  const registry = JSON.parse(bytes.toString('utf8'));
  if (!exactKeys(registry, ['schema', 'playwright_version', 'project', 'repeat_index', 'tests'])
      || registry.schema !== 'browser-ci-registry/v1' || registry.playwright_version !== '1.63.0'
      || registry.project !== '' || registry.repeat_index !== 0 || !Array.isArray(registry.tests)
      || registry.tests.length === 0) throw new Error('browser_ci_invalid_registry');
  const ids = new Set(); const identities = new Set(); const files = new Map();
  for (const item of registry.tests) {
    if (!exactKeys(item, ['id', 'file', 'title_path', 'shard', 'retry_override'])
        || !/^[a-z][a-z0-9-]{0,79}$/.test(item.id)
        || !/^tests\/[a-z0-9-]+\.spec\.mjs$/.test(item.file)
        || !Array.isArray(item.title_path) || item.title_path.length === 0
        || item.title_path.some((value) => typeof value !== 'string' || !value || value.length > 500)
        || !['core', 'catchup'].includes(item.shard)
        || ![null, 0].includes(item.retry_override)) throw new Error('browser_ci_invalid_registry');
    const identity = JSON.stringify([item.file, item.title_path]);
    if (ids.has(item.id) || identities.has(identity)
        || (files.has(item.file) && files.get(item.file) !== item.shard)) {
      throw new Error('browser_ci_duplicate_or_split_identity');
    }
    ids.add(item.id); identities.add(identity); files.set(item.file, item.shard);
  }
  return { registry, digest: sha256(bytes) };
}

export function laneTests(registry, lane, mode = 'execution') {
  if (!LANES.includes(lane) || !['inventory', 'execution'].includes(mode)) {
    throw new Error('browser_ci_invalid_selection');
  }
  return registry.tests.filter((test) => mode === 'inventory' || lane === 'legacy' || test.shard === lane);
}

// Only checked-in identities may leave this lookup. Unknown titles/paths never
// enter the report or its error messages.
export function identifyTest(test, registry, root = E2E_ROOT) {
  const file = path.relative(root, test.location.file).split(path.sep).join('/');
  const titles = [test.title];
  let parent = test.parent;
  while (parent && parent.type !== 'file') {
    if (parent.type === 'describe') titles.unshift(parent.title);
    parent = parent.parent;
  }
  const project = test.parent.project();
  if (!parent || !project || project.name !== registry.project
      || test.repeatEachIndex !== registry.repeat_index) return null;
  return registry.tests.find((item) => item.file === file
    && JSON.stringify(item.title_path) === JSON.stringify(titles)) ?? null;
}
