import { appendFileSync, writeFileSync } from 'node:fs';
import { E2E_ROOT, EVENT_SCHEMA, LANES, identifyTest, readRegistry } from './registry.mjs';

const STATUSES = ['passed', 'failed', 'timedOut', 'skipped', 'interrupted'];
const OUTCOMES = ['expected', 'unexpected', 'flaky', 'skipped'];
const TERMINALS = ['passed', 'failed', 'timedout', 'interrupted'];
const integer = (value) => Number.isSafeInteger(value) && value >= 0;

export default class RestrictedReporter {
  constructor(options) {
    this.options = options;
    if (!['inventory', 'execution'].includes(options.mode) || !LANES.includes(options.lane)) {
      throw new Error('browser_ci_invalid_reporter_options');
    }
    let registry; let digest;
    try { ({ registry, digest } = readRegistry(options.registryPath)); }
    catch { throw new Error('browser_ci_reporter_initialization_failed'); }
    this.registry = registry; this.digest = digest; this.seq = 0;
    this.identities = new Map(); this.tests = []; this.attempts = new Map();
    this.globalErrors = 0; this.faults = 0;
    // Never replace evidence from an earlier execution or retry of the job.
    try { writeFileSync(options.file, '', { flag: 'wx', encoding: 'utf8' }); }
    catch { throw new Error('browser_ci_reporter_initialization_failed'); }
  }

  // Suppress Playwright's automatic list reporter during the new inventory pass.
  // Execution still has the unchanged configured line/list reporter alongside us.
  printsToStdio() { return this.options.mode === 'inventory'; }

  emit(event, fields = {}) {
    // Callers construct closed records; no Playwright object is serialized.
    try {
      appendFileSync(this.options.file, JSON.stringify({
        schema: EVENT_SCHEMA, seq: this.seq++, event, ...fields,
      }) + '\n', 'utf8');
    } catch { throw new Error('browser_ci_reporter_write_failed'); }
  }

  fault(code) { this.faults += 1; this.emit('fault', { code }); }

  onBegin(config, suite) {
    const project = config.projects[0];
    if (config.workers !== 1 || config.projects.length !== 1
        || ![0, 1].includes(project.retries) || project.timeout !== 180_000
        || config.version !== this.registry.playwright_version) {
      this.fault('invalid_config');
      return;
    }
    this.emit('begin', {
      mode: this.options.mode, lane: this.options.lane, workers: 1,
      default_retries: project.retries, timeout_ms: project.timeout,
      playwright_version: config.version, registry_sha256: this.digest,
    });
    const seen = new Set(); const selected = [];
    this.tests = suite.allTests();
    for (const test of this.tests) {
      const entry = identifyTest(test, this.registry, this.options.root ?? E2E_ROOT);
      if (!entry) { this.fault('unknown_identity'); continue; }
      if (seen.has(entry.id)) { this.fault('duplicate_identity'); continue; }
      if (!integer(test.retries)) { this.fault('invalid_config'); continue; }
      seen.add(entry.id); this.identities.set(test, entry.id);
      selected.push({ id: entry.id, retries: test.retries, repeat_index: 0, project: '' });
    }
    this.emit('collection', { tests: selected });
  }

  onTestBegin(test, result) {
    const id = this.identities.get(test);
    if (!id || !integer(result.retry) || !integer(result.workerIndex)) {
      this.fault('invalid_callback'); return;
    }
    this.emit('attempt_begin', { id, retry: result.retry, worker_index: result.workerIndex });
  }

  onTestEnd(test, result) {
    const id = this.identities.get(test);
    if (!id || !integer(result.retry) || !Number.isFinite(result.duration) || result.duration < 0) {
      this.fault('invalid_callback'); return;
    }
    if (!STATUSES.includes(result.status) || !STATUSES.includes(test.expectedStatus)) {
      this.fault('invalid_status'); return;
    }
    this.attempts.set(id, (this.attempts.get(id) ?? 0) + 1);
    this.emit('attempt_end', { id, retry: result.retry, status: result.status,
      expected_status: test.expectedStatus, duration_ms: Math.round(result.duration) });
  }

  onError() { this.globalErrors += 1; this.fault('global_error'); }

  onEnd(result) {
    if (!TERMINALS.includes(result.status)) { this.fault('invalid_status'); return; }
    let completed = 0;
    if (this.options.mode === 'execution') {
      for (const test of this.tests) {
        const id = this.identities.get(test);
        if (!id) continue;
        const outcome = test.outcome();
        if (!OUTCOMES.includes(outcome) || !STATUSES.includes(test.expectedStatus)) {
          this.fault('invalid_status'); continue;
        }
        completed += 1;
        this.emit('outcome', { id, outcome, expected_status: test.expectedStatus,
          attempts: this.attempts.get(id) ?? 0 });
      }
    }
    this.emit('end', { status: result.status, total: this.identities.size,
      completed, global_errors: this.globalErrors });
  }
}
