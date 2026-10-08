import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { performance } from 'node:perf_hooks';
import { generateInventory, inventoryBytes } from './inventory.mjs';

export const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
export const GROUPS = ['all', 'dynamic', 'remaining'];
export function parseCaptureOptions(argv, env = process.env, defaultOutput = 'output') {
  let group = 'all', output = defaultOutput, explicit = false, positional = false;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--stage-group' && !explicit && GROUPS.includes(argv[index + 1])) {
      group = argv[++index]; explicit = true;
    } else if (!argv[index].startsWith('-') && !positional) { output = argv[index]; positional = true; }
    else throw new Error('visual_ci_invalid_arguments');
  }
  if (explicit && (env.VISUAL_CAPTURE_ONLY || env.STATIC_SURFACE_ONLY)) throw new Error('visual_ci_partial_filter_refused');
  return { group, output: resolve(output), explicit, partial: Boolean(env.VISUAL_CAPTURE_ONLY || env.STATIC_SURFACE_ONLY) };
}
export function sourceIdentity(env, cwd, readHead = () => execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
}).trim()) {
  let source;
  try { source = readHead(); } catch { throw new Error('visual_ci_source_unavailable'); }
  if (!/^[a-f0-9]{40}$/.test(source) || [env.PRODUCT_SHA, env.VISUAL_CAPTURE_REVISION].some(value => value !== undefined && value !== source)
      || (env.GITHUB_RUN_ID && !/^\d+$/.test(env.GITHUB_RUN_ID))
      || (env.GITHUB_RUN_ATTEMPT && !/^[1-9]\d*$/.test(env.GITHUB_RUN_ATTEMPT))
      || !Number.isSafeInteger(Number(env.GITHUB_RUN_ATTEMPT ?? 1))) throw new Error('visual_ci_invalid_provenance');
  return { source_commit: source, workflow_run_id: env.GITHUB_RUN_ID ?? 'local', run_attempt: Number(env.GITHUB_RUN_ATTEMPT ?? 1) };
}

export class CaptureRecorder {
  constructor({ group, source, partial = false, clock = () => performance.now() }) {
    if (!GROUPS.includes(group)) throw new Error('visual_ci_invalid_group');
    this.group = group; this.source = source; this.partial = partial; this.clock = clock;
    this.inventory = generateInventory(); this.bytes = inventoryBytes(); this.started = clock();
    this.startedAt = new Date().toISOString(); this.records = new Map();
    this.selected = new Map(this.inventory.cases.filter(value => group === 'all' || value.group === group).map(value => [value.id, value]));
  }
  begin(id, configuration) {
    const expected = this.selected.get(id);
    if (!expected || this.records.has(id) || !isDeepStrictEqual(configuration, expected.configuration)) throw new Error('visual_ci_invalid_case_start');
    const start = this.clock();
    const record = { id, configuration: structuredClone(configuration), observations: [], outcome: 'incomplete', files: [], duration_ms: 0 };
    this.records.set(id, record);
    let ended = false;
    return ({ observations = [], outcome, files = [] }) => {
      if (ended) throw new Error('visual_ci_duplicate_case_terminal');
      ended = true;
      const allowed = new Set(expected.observations);
      const safeObservations = Array.isArray(observations) ? observations.filter(value => allowed.has(value)) : [];
      const safeFiles = Array.isArray(files) ? files.filter(value => expected.files.includes(value)) : [];
      const exact = isDeepStrictEqual(observations, expected.observations)
        && isDeepStrictEqual([...new Set(files)].sort(), [...expected.files].sort());
      record.observations = safeObservations;
      record.files = safeFiles;
      record.outcome = outcome === 'passed' && exact ? 'passed' : 'failed';
      record.duration_ms = Math.max(0, Math.round(this.clock() - start));
    };
  }
  progress(phase) {
    if (!['prepare', 'frontend', 'dynamic', 'freshness', 'review', 'static', 'cleanup', 'manifest'].includes(phase)) throw new Error('visual_ci_invalid_phase');
    const completed = [...this.records.values()].filter(value => value.outcome !== 'incomplete').length;
    return `visual-ci progress group=${this.group} phase=${phase} elapsed_seconds=${Math.floor((this.clock() - this.started) / 1000)} selected=${this.selected.size} completed=${completed}`;
  }
  async writeInventory(directory) {
    try { await writeFile(resolve(directory, 'inventory.json'), this.bytes, { flag: 'wx' }); }
    catch { throw new Error('visual_ci_inventory_write_failed'); }
  }
  async finish(directory, { exitCode, phaseTimings }) {
    const fileHashes = {};
    const shared = [...new Set(Object.entries(this.inventory.shared_files).filter(([group]) => this.group === 'all' || this.group === group).flatMap(([, files]) => files))];
    const files = [...new Set([...this.records.values()].flatMap(value => value.files).concat(shared))].sort();
    let missing = false;
    for (const file of files) {
      try { fileHashes[file] = sha256(await readFile(resolve(directory, file))); }
      catch { missing = true; }
    }
    const complete = !this.partial && !missing && exitCode === 0 && this.records.size === this.selected.size
      && [...this.records.values()].every(value => value.outcome === 'passed');
    const receipt = { schema: 'visual-ci-receipt/v1', ...this.source,
      logical_job: this.group === 'all' ? 'visual-capture-serial-control' : `visual-capture-${this.group}`,
      group: this.group, platform: ({ linux: 'Linux', win32: 'Windows', darwin: 'Darwin' })[process.platform], workers: 4,
      inventory_sha256: sha256(this.bytes), complete, exit_code: complete || (this.partial && exitCode === 0) ? 0 : 1, retries: 0, skips: 0,
      cases: [...this.records.values()].sort((a, b) => a.id.localeCompare(b.id)), file_sha256: fileHashes,
      phase_timings: phaseTimings, started_at: this.startedAt, finished_at: new Date().toISOString(), duration_ms: Math.round(this.clock() - this.started) };
    try { await writeFile(resolve(directory, 'capture-ci.json'), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx' }); }
    catch { throw new Error('visual_ci_receipt_write_failed'); }
    return receipt;
  }
}
