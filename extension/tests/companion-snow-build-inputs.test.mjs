import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeNotice, sourcePathMappings } from '../crypto/snow/build-inputs.mjs';

test('complete source remapping produces the same paths on Windows and Linux', () => {
  const windows = sourcePathMappings('C:\\Cargo Home\\snow-0.10.0', 'snow', '0.10.0', [
    'C:\\Cargo Home\\snow-0.10.0\\src\\params\\patterns.rs',
    'C:\\Cargo Home\\snow-0.10.0\\src\\builder.rs',
  ]);
  const linux = sourcePathMappings('/home/runner/.cargo/snow-0.10.0', 'snow', '0.10.0', [
    '/home/runner/.cargo/snow-0.10.0/src/params/patterns.rs',
    '/home/runner/.cargo/snow-0.10.0/src/builder.rs',
  ]);
  const targets = (values) => [...new Set(values.map(([, value]) => value))].sort();
  assert.deepEqual(targets(windows), targets(linux));
  assert.deepEqual(targets(windows), ['/crate/snow-0.10.0/src/builder.rs', '/crate/snow-0.10.0/src/params/patterns.rs']);
  assert.ok(windows.every(([source, target]) => source.endsWith('.rs') && !target.includes('\\')));
});

test('local relative paths are remapped and response-file injection is refused', () => {
  const paths = new Map(sourcePathMappings('C:\\repo', 'wrapper', '1.0.0', ['C:\\repo\\src\\lib.rs'], { local: true }));
  assert.equal(paths.get('src/lib.rs'), '/crate/wrapper-1.0.0/src/lib.rs');
  assert.equal(paths.get('src\\lib.rs'), '/crate/wrapper-1.0.0/src/lib.rs');
  assert.throws(() => sourcePathMappings('/repo', 'wrapper', '1.0.0', ['/outside/lib.rs']));
  assert.throws(() => sourcePathMappings('/repo', 'wrapper', '1.0.0', ['/repo/line\nbreak.rs']));
});

test('upstream license line endings normalize before artifact hashing', () => {
  assert.equal(normalizeNotice('License\r\nFirst line\r\n\r\nSecond line\r\n'), 'License\nFirst line\n\nSecond line');
  assert.equal(normalizeNotice('License\nFirst line\n\nSecond line\n'), 'License\nFirst line\n\nSecond line');
});
