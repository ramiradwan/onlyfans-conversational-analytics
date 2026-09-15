import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

import {
  EXTENSION_DIST,
  EXTENSION_ROOT,
  PRODUCT_ROOT,
} from './lib/paths.mjs';

const execFileAsync = promisify(execFile);

/**
 * Build a qualification-only extension artifact whose Legal release bindings
 * are embedded exactly like a release build. The ordinary extension/dist tree
 * remains untouched, and a restarted MV3 worker therefore sees the same Legal
 * authority before its startup reconciliation runs.
 */
export default async function globalSetup() {
  const bindings = path.join(
    EXTENSION_ROOT,
    'tests',
    'fixtures',
    'legal-instrument-bindings.synthetic.json',
  );
  await execFileAsync(process.execPath, [
    path.join(EXTENSION_ROOT, 'build.mjs'),
    `--outdir=${EXTENSION_DIST}`,
    `--legal-release-bindings=${bindings}`,
  ], {
    cwd: PRODUCT_ROOT,
    windowsHide: true,
    maxBuffer: 16 * 1024 * 1024,
  });
}
