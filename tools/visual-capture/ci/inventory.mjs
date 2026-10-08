import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createScreens, FIXED_NOW, MODES, VIEWPORTS } from '../screen-matrix.mjs';
import { dynamicCases, dynamicSequence } from '../dynamic-matrix.mjs';
import { FRESHNESS_TRANSITIONS, FRESHNESS_VERTICES, freshnessCases } from '../freshness-matrix.mjs';
import { staticCases } from '../static-fixture-matrix.mjs';
import { VISION_TYPES } from '../vision-diagnostics.mjs';

export function ordinaryCases(screens = createScreens()) {
  return VIEWPORTS.flatMap(viewport => MODES.flatMap(mode => screens
    .filter(screen => (!viewport.targetedOnly || screen.viewports?.includes(viewport.name))
      && (!screen.viewports || screen.viewports.includes(viewport.name)) && (!screen.modes || screen.modes.includes(mode)))
    .map(screen => ({ viewport, mode, screen }))));
}
export const ordinaryName = ({ screen, mode, viewport }) => `${screen.workspace}-${screen.state}${screen.variant ? '-' + screen.variant : ''}-${mode}-${viewport.name}`;
export const dynamicName = ({ view, width, mode, fontScale, motion }) => [view, width, mode, fontScale, motion].join('-');
export const freshnessName = ({ width, mode, fontScale, motion }) => ['freshness', width, mode, fontScale, motion].join('-');
export function staticConfiguration({ mode, fixture, width }) {
  const viewport = fixture.pairing ? { width: 400, height: 488 }
    : { width, height: fixture.surface === 'popup' ? 600 : width > 600 ? 900 : 844 };
  return { surface: fixture.surface, state: fixture.name, mode, viewport };
}
export const staticName = value => {
  const config = staticConfiguration(value);
  return `${config.surface}-${config.state}-${config.mode}-${config.viewport.width}`;
};

export function reviewCases() {
  return MODES.flatMap(mode => [
    { key: 'rail', name: `${mode}: rail geometry, state hierarchy, tooltip and focus`, images: [`rail-hover-${mode}.png`, `rail-focus-${mode}.png`] },
    { key: 'brand', name: `${mode}: popup and app brand pixels`, images: [] },
    { key: 'navigation', name: `${mode}: narrow navigation and restored keyboard focus`, images: [`mobile-drawer-${mode}.png`, `mobile-focus-${mode}.png`] },
    ...[1440, 820, 390].map(width => ({ key: `analytics-${width}`, name: `${mode}: analytics typography and tracks at ${width}px`, images: [] })),
    { key: 'passkey', name: `${mode}: recent rows, status and passkey`, images: [1440, 390].flatMap(width => [`passkey-layout-${mode}-${width}.png`, `passkey-error-${mode}-${width}.png`]) },
    { key: 'motion', name: `${mode}: bounded motion and reduced-motion suppression`, images: [] },
  ].map(value => ({ ...value, mode })));
}

export function generateInventory() {
  const cases = [];
  for (const value of ordinaryCases()) {
    const { screen, mode, viewport } = value; const name = ordinaryName(value);
    const report = `${name}-geometry.json`;
    const vision = screen.workspace === 'analytics' && screen.state === 'model' && !screen.variant ? VISION_TYPES : [];
    cases.push({ id: `ordinary-${name}`, group: 'remaining', kind: 'ordinary',
      configuration: { workspace: screen.workspace, state: screen.state, variant: screen.variant ?? null, mode,
        viewport: viewport.name, width: viewport.width, height: viewport.height },
      observations: ['fold', 'full', ...vision.map(type => `vision:${type}`)], report,
      files: [report, `${screen.workspace}/${name}-fold.png`, `${screen.workspace}/${name}-full.png`, ...vision.map(type => `diagnostics/vision/${name}-${type}.png`)] });
  }
  for (const value of dynamicCases()) {
    const report = `transitions/${dynamicName(value)}.json`;
    cases.push({ id: `dynamic-${dynamicName(value)}`, group: 'dynamic', kind: 'dynamic', configuration: value,
      observations: dynamicSequence(value.view, value.width), report, files: [report] });
  }
  for (const value of freshnessCases()) {
    const report = `${freshnessName(value)}.json`;
    cases.push({ id: freshnessName(value), group: 'remaining', kind: 'freshness', configuration: value,
      observations: [...FRESHNESS_TRANSITIONS.map(({ from, to }) => `edge:${from.id}:${to.id}`), ...FRESHNESS_VERTICES.map(frame => `overlay:${frame.id}`)],
      report, files: [report] });
  }
  for (const value of reviewCases()) cases.push({ id: `review-${value.mode}-${value.key}`, group: 'remaining', kind: 'review',
    configuration: { mode: value.mode, name: value.name }, observations: [value.name], report: 'review/acceptance.json',
    files: ['review/acceptance.json', ...value.images.map(file => `review/${file}`)] });
  for (const value of staticCases()) {
    const name = staticName(value);
    cases.push({ id: `static-${name}`, group: 'remaining', kind: 'static', configuration: staticConfiguration(value),
      observations: [`check:${name}`, 'fold', 'full'], report: 'static-surfaces/acceptance.json',
      files: ['static-surfaces/acceptance.json', `static-surfaces/${name}-geometry.json`, `static-surfaces/${name}-fold.png`, `static-surfaces/${name}-full.png`] });
  }
  if (new Set(cases.map(value => value.id)).size !== cases.length) throw new Error('visual_ci_duplicate_inventory_identity');
  return { schema: 'visual-ci-inventory/v1', workers: 4, fixed_now: FIXED_NOW,
    shared_files: { dynamic: ['manifest.json', 'transitions/manifest.json'], remaining: ['manifest.json', 'freshness-transitions.json'] }, cases };
}

export function inventoryBytes() { return JSON.stringify(generateInventory(), null, 2) + '\n'; }
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== '--output' || !args[1])) throw new Error('visual_ci_invalid_inventory_arguments');
  const bytes = inventoryBytes();
  if (args.length) { await mkdir(dirname(resolve(args[1])), { recursive: true }); await writeFile(args[1], bytes); }
  else process.stdout.write(bytes);
}
