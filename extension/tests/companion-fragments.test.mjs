import test from 'node:test';
import assert from 'node:assert/strict';
import { createFragmentReceiver, fragmentMessage } from '../transport/companion-fragments.mjs';

const encode = (value) => new TextEncoder().encode(JSON.stringify(value));
const parse = (value) => JSON.parse(new TextDecoder().decode(value));
const refused = { message: 'session_fragment_refused' };
const raw = (text) => encode({ type: 'fragment', id: crypto.randomUUID(), index: 0, final: true, data: Buffer.from(text).toString('base64url') });

test('production fragmentation roundtrips the 512 KiB maximum and preserves protocol numbers', () => {
  const document = { protocol_version: '2', value: 1.25, text: '' };
  document.text = 'x'.repeat(524288 - encode(document).length);
  const frames = fragmentMessage(document);
  assert.equal(encode(document).length, 524288);
  assert.ok(frames.length > 180 && frames.every((frame) => frame.length <= 4079));
  const receiver = createFragmentReceiver();
  for (const frame of frames.slice(0, -1)) assert.equal(receiver.receive(frame), null);
  assert.deepEqual(receiver.receive(frames.at(-1)), document);
  assert.equal(receiver.remainingMs, null);
  assert.throws(() => fragmentMessage({ ...document, text: document.text + 'x' }), refused);
});

test('partial message deadline is observable while the peer is silent and never extends', () => {
  let time = 100;
  const receiver = createFragmentReceiver({ now: () => time });
  const frames = fragmentMessage({ text: 'x'.repeat(9000) });
  receiver.receive(frames[0]);
  assert.equal(receiver.remainingMs, 10000);
  time += 4000;
  receiver.receive(frames[1]);
  assert.equal(receiver.remainingMs, 6000);
  time += 6000;
  assert.equal(receiver.remainingMs, 0);
  assert.throws(() => receiver.receive(frames[2]), refused);
  assert.equal(receiver.remainingMs, null);
});

test('fragment receiver refuses duplicate, reordered, interleaved and unknown fields', () => {
  const frames = fragmentMessage({ text: 'x'.repeat(4000) });
  for (const invalid of [frames[0], fragmentMessage({ text: 'other' })[0], encode({ ...parse(frames[1]), extra: 1 })]) {
    const receiver = createFragmentReceiver(); receiver.receive(frames[0]);
    assert.throws(() => receiver.receive(invalid), refused);
  }
  assert.throws(() => createFragmentReceiver().receive(frames[1]), refused);
  const duplicate = new TextEncoder().encode(new TextDecoder().decode(frames[0]).replace('"index":0', '"index":0,"index":0'));
  assert.throws(() => createFragmentReceiver().receive(duplicate), refused);
});

test('fragment receiver rejects ambiguous JSON, invalid UTF-8 and noncanonical base64', () => {
  for (const text of ['{"a":1,"a":2}', '{"a":1e999}', '{"a":"\\ud800"}', '[]']) {
    assert.throws(() => createFragmentReceiver().receive(raw(text)), refused);
  }
  const record = parse(raw('{}'));
  for (const data of [record.data + '=', 'wA', 'e31', '']) {
    assert.throws(() => createFragmentReceiver().receive(encode({ ...record, data })), refused);
  }
});
