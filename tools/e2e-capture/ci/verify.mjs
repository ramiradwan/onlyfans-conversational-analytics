// Local runner checks. The Python artifact verifier independently enforces the
// same contract at the required CI gate; this is not the acceptance authority.
import { EVENT_SCHEMA, exactKeys, laneTests } from './registry.mjs';

const BASE = ['schema', 'seq', 'event'];
const FIELDS = {
  begin: ['mode', 'lane', 'workers', 'default_retries', 'timeout_ms', 'playwright_version', 'registry_sha256'],
  collection: ['tests'], attempt_begin: ['id', 'retry', 'worker_index'],
  attempt_end: ['id', 'retry', 'status', 'expected_status', 'duration_ms'],
  outcome: ['id', 'outcome', 'expected_status', 'attempts'],
  end: ['status', 'total', 'completed', 'global_errors'], fault: ['code'],
};
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const fail = () => { throw new Error('browser_ci_invalid_evidence'); };

export function verifyEvents(bytes, { registry, digest, mode, lane, qualification = false }) {
  if (bytes.length > 1024 * 1024 || !bytes.endsWith('\n')) fail();
  let events;
  try {
    const lines = bytes.slice(0, -1).split('\n');
    events = lines.map((line) => {
      const value = JSON.parse(line);
      // Reporter output is canonical JSON. This also rejects duplicate keys.
      if (JSON.stringify(value) !== line) fail();
      return value;
    });
  } catch { fail(); }
  if (events.length < 3 || events.length > 512) fail();
  for (const [index, event] of events.entries()) {
    if (!event || !FIELDS[event.event] || event.schema !== EVENT_SCHEMA || event.seq !== index
        || !exactKeys(event, [...BASE, ...FIELDS[event.event]]) || event.event === 'fault') fail();
  }
  const [begin, collection] = events; const end = events.at(-1);
  if (begin.event !== 'begin' || collection.event !== 'collection' || end.event !== 'end'
      || begin.mode !== mode || begin.lane !== lane || begin.workers !== 1
      || ![0, 1].includes(begin.default_retries) || begin.timeout_ms !== 180_000
      || begin.playwright_version !== registry.playwright_version || begin.registry_sha256 !== digest
      || end.status !== 'passed' || end.global_errors !== 0) fail();
  const expected = new Map(laneTests(registry, lane, mode).map((item) => [item.id, item]));
  const tests = new Map();
  if (!Array.isArray(collection.tests)) fail();
  for (const test of collection.tests) {
    if (!exactKeys(test, ['id', 'retries', 'repeat_index', 'project']) || !expected.has(test.id)
        || tests.has(test.id) || test.project !== registry.project || test.repeat_index !== 0
        || test.retries !== (expected.get(test.id).retry_override ?? begin.default_retries)) fail();
    tests.set(test.id, { ...test, attempts: [], outcome: null });
  }
  if (tests.size !== expected.size || end.total !== expected.size) fail();
  if (mode === 'inventory') {
    if (events.length !== 3 || end.completed !== 0) fail();
    return { count: tests.size, completed: 0, retried: 0 };
  }
  let active = null; let outcomePhase = false;
  for (const event of events.slice(2, -1)) {
    const test = tests.get(event.id);
    if (!test) fail();
    if (event.event === 'attempt_begin') {
      if (outcomePhase || active || !integer(event.retry) || !integer(event.worker_index)
          || event.retry !== test.attempts.length || event.retry > test.retries
          || test.attempts.at(-1)?.status === 'passed') fail();
      active = { id: event.id, retry: event.retry };
    } else if (event.event === 'attempt_end') {
      if (outcomePhase || !active || active.id !== event.id || active.retry !== event.retry
          || !['passed', 'failed', 'timedOut'].includes(event.status)
          || event.expected_status !== 'passed' || !integer(event.duration_ms)) fail();
      test.attempts.push(event); active = null;
    } else if (event.event === 'outcome') {
      outcomePhase = true;
      if (active || test.outcome || event.expected_status !== 'passed'
          || event.attempts !== test.attempts.length || event.attempts === 0
          || test.attempts.at(-1).status !== 'passed') fail();
      const derived = test.attempts.length > 1 ? 'flaky' : 'expected';
      if (event.outcome !== derived || (qualification && test.attempts.length > 1)) fail();
      test.outcome = event;
    } else fail();
  }
  if (active || [...tests.values()].some((test) => !test.outcome) || end.completed !== tests.size) fail();
  return { count: tests.size, completed: tests.size,
    retried: [...tests.values()].filter((test) => test.attempts.length > 1).length };
}
