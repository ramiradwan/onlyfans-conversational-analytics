import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const E2E_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const PRODUCT_ROOT = path.resolve(E2E_ROOT, '..', '..');
export const EXTENSION_ROOT = path.join(PRODUCT_ROOT, 'extension');
export const EXTENSION_DIST = path.join(EXTENSION_ROOT, 'dist');
export const SPA_MANIFEST_PATHS = [
  path.join(PRODUCT_ROOT, 'app', 'static', 'dist', 'manifest.json'),
  path.join(PRODUCT_ROOT, 'app', 'static', 'dist', '.vite', 'manifest.json'),
];

export function pythonExecutable() {
  if (process.env.OFCA_E2E_PYTHON) return process.env.OFCA_E2E_PYTHON;
  const virtualEnvironmentPython = process.platform === 'win32'
    ? path.join(PRODUCT_ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(PRODUCT_ROOT, '.venv', 'bin', 'python');
  return existsSync(virtualEnvironmentPython) ? virtualEnvironmentPython : 'python';
}

export function assertBuiltSpa() {
  if (!SPA_MANIFEST_PATHS.some((candidate) => existsSync(candidate))) {
    throw new Error(
      'The built Bridge is missing. Run `npm run build --prefix frontend` from the product root.',
    );
  }
}

export function assertBuiltExtension() {
  const required = [
    'background.js',
    'build-meta.json',
    'content.js',
    'manifest.json',
    'page-hook.js',
  ];
  const missing = required.filter((filename) => (
    !existsSync(path.join(EXTENSION_DIST, filename))
  ));
  if (missing.length > 0) {
    throw new Error(
      `The audited extension artifact is missing ${missing.join(', ')}. `
      + 'Run `npm run build --prefix extension` from the product root.',
    );
  }
}
