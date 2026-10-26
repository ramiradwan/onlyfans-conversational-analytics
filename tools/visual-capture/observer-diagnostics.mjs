// Registered observer identities only; browser strings never enter retained metadata.
export const OBSERVER_REGIONS = Object.freeze(Object.fromEntries(Object.entries({
  home: ['freshness-status', 'issue-band', 'dashboard-notice', 'dashboard-overview', 'dashboard-basis', 'dashboard-recent', 'dashboard-setup'],
  analytics: ['freshness-status', 'issue-band', 'analytics-content'],
  inbox: ['freshness-status', 'issue-band', 'inbox-notice', 'inbox-content'],
  settings: ['freshness-status', 'issue-band', 'settings-browser', 'settings-history', 'settings-activation', 'settings-vault'],
  graph: ['freshness-status', 'issue-band', 'graph-notice'],
  passkey: ['passkey-card', 'passkey-feedback'],
  'production-boot': ['passkey-card', 'passkey-feedback'],
  popup: ['popup-content', 'popup-status', 'surface-feedback'],
  setup: ['extension-stage', 'surface-feedback'],
  options: ['options-notice', 'options-capture', 'options-connection', 'options-data', 'surface-feedback'],
}).map(([view, regions]) => [view, Object.freeze(regions)])));

const PHASES = Object.freeze({
  required_region_missing_at_first_surface: 'first_surface',
  observer_read_failed: 'observer_read',
  observer_unclassified: 'observer_inspection',
});
const entry = (code, region = null) => ({ code, phase: PHASES[code], region });
const regionsFor = view => typeof view === 'string' && Object.hasOwn(OBSERVER_REGIONS, view) ? OBSERVER_REGIONS[view] : null;

export function collectObserverDiagnostics(view, failures, readFailed = false) {
  const regions = regionsFor(view);
  if (!regions || typeof readFailed !== 'boolean') throw new Error('visual_ci_invalid_observer_diagnostics');
  if (readFailed) return [entry('observer_read_failed')];
  if (!Array.isArray(failures)) return [entry('observer_unclassified')];
  const missing = regions.filter(region => failures.includes(`${region}: missing on first surface frame`));
  const known = new Set(missing.map(region => `${region}: missing on first surface frame`));
  return [...missing.map(region => entry('required_region_missing_at_first_surface', region)),
    ...(failures.some(value => !known.has(value)) ? [entry('observer_unclassified')] : [])];
}

export function validateObserverDiagnostics(value, configuration, outcome) {
  const view = configuration.view ?? configuration.workspace ?? configuration.surface;
  const regions = regionsFor(view);
  const invalid = () => { throw new Error('visual_ci_invalid_observer_diagnostics'); };
  if (!Array.isArray(value)) invalid();
  if (!value.length) return [];
  if (!regions || outcome !== 'failed' || value.length > regions.length + 2) invalid();
  const seen = new Set();
  return value.map(row => {
    if (!row || Object.getPrototypeOf(row) !== Object.prototype
        || Object.keys(row).sort().join(',') !== 'code,phase,region'
        || typeof row.code !== 'string' || typeof row.phase !== 'string'
        || !Object.hasOwn(PHASES, row.code) || row.phase !== PHASES[row.code]
        || (row.code === 'required_region_missing_at_first_surface' ? !regions.includes(row.region) : row.region !== null)) invalid();
    const key = JSON.stringify([row.code, row.phase, row.region]);
    if (seen.has(key)) invalid();
    seen.add(key);
    return { code: row.code, phase: row.phase, region: row.region };
  });
}
