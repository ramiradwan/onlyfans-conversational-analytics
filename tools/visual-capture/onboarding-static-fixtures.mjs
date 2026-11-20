// Real-document static fixture builder for native desktop onboarding and
// browser task surfaces. Drives frontend/onboarding/view.js and
// frontend/browser-tasks/view.js against the real production HTML and styles.
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BROWSER_TASK_STATES, DESKTOP_ONBOARDING_STATES } from './onboarding-surface-states.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const frontend = join(root, 'frontend');
const { build } = createRequire(new URL('../../extension/package.json', import.meta.url))('esbuild');

const cache = new Map();

async function fontPreloadTag() {
  if (!cache.has('font-preload')) {
    const fontsCss = await readFile(join(frontend, 'src/theme/generated/static-fonts.css'), 'utf8');
    const match = /font-family:\s*'Inter Variable'[\s\S]*?src:\s*url\((data:font\/woff2;base64,[^)]+)\)/u.exec(fontsCss);
    if (!match) throw new Error('Inter Variable embedded font URL is missing from static-fonts.css');
    cache.set('font-preload', `<link rel="preload" as="font" type="font/woff2" crossorigin href="${match[1]}">`);
  }
  return cache.get('font-preload');
}

async function bundleCss(surface) {
  const key = `css:${surface}`;
  if (!cache.has(key)) {
    const contents = surface === 'desktop-onboarding'
      ? `@import './shared-setup/shell.css';\n@import './shared-setup/providers.css';\n`
      : `@import './shared-setup/shell.css';\n`;
    const result = await build({
      stdin: { contents, resolveDir: frontend, loader: 'css', sourcefile: `${surface}.css` },
      bundle: true,
      write: false,
      logLevel: 'silent',
      loader: { '.ttf': 'dataurl', '.woff2': 'dataurl', '.png': 'dataurl', '.svg': 'dataurl' },
    });
    cache.set(key, result.outputFiles[0].text);
  }
  return cache.get(key);
}

async function bundleScript(surface) {
  const key = `js:${surface}`;
  if (!cache.has(key)) {
    const contents = surface === 'desktop-onboarding'
      ? `import { applyTheme } from './shared-setup/presentation.js';
import { renderSetup } from './onboarding/view.js';
applyTheme(document);
renderSetup(document, window.__staticSurfaceFixture);
document.querySelector('main').dataset.ready = 'true';
`
      : `import { applyTheme } from './shared-setup/presentation.js';
import { renderBrowserTask } from './browser-tasks/view.js';
applyTheme(document);
renderBrowserTask(document, window.__staticSurfaceFixture);
document.querySelector('main').dataset.ready = 'true';
`;
    const result = await build({
      stdin: { contents, resolveDir: frontend, loader: 'js', sourcefile: `${surface}.js` },
      bundle: true,
      format: 'iife',
      write: false,
      logLevel: 'silent',
    });
    cache.set(key, result.outputFiles[0].text);
  }
  return cache.get(key);
}

export async function onboardingDocument(surface) {
  const key = `html:${surface}`;
  if (!cache.has(key)) {
    const sourcePath = surface === 'desktop-onboarding'
      ? join(frontend, 'onboarding/index.html')
      : join(frontend, 'browser-tasks/index.html');
    const source = await readFile(sourcePath, 'utf8');
    const preload = await fontPreloadTag();
    const headInsert = `${preload}\n    <link rel="stylesheet" href="/${surface}.css">\n  </head>`;
    const html = source
      .replace('</head>', headInsert)
      .replace('<script type="module" src="./main.js"></script>', `<script src="/${surface}.js"></script>`);
    if (html === source) throw new Error(`Failed to wire static harness assets into ${surface} document`);
    cache.set(key, html);
  }
  return cache.get(key);
}

export async function onboardingStaticAssets() {
  if (!cache.has('assets')) {
    const [setupThemeInit, googlePng, microsoftSvg, onboardingCss, browserTaskCss] = await Promise.all([
      readFile(join(frontend, 'public/setup-theme-init.js'), 'utf8'),
      readFile(join(frontend, 'shared-setup/providers/google.png')),
      readFile(join(frontend, 'shared-setup/providers/microsoft.svg'), 'utf8'),
      bundleCss('desktop-onboarding'),
      bundleCss('browser-task'),
    ]);
    cache.set('assets', {
      '/setup-theme-init.js': { contentType: 'text/javascript', body: setupThemeInit },
      '/shared-setup/providers/google.png': { contentType: 'image/png', body: googlePng },
      '/shared-setup/providers/microsoft.svg': { contentType: 'image/svg+xml', body: microsoftSvg },
      '/desktop-onboarding.css': { contentType: 'text/css', body: onboardingCss },
      '/browser-task.css': { contentType: 'text/css', body: browserTaskCss },
    });
  }
  return cache.get('assets');
}

export async function onboardingStaticFixtures() {
  const [onboardingHtml, onboardingScript, browserTaskHtml, browserTaskScript] = await Promise.all([
    onboardingDocument('desktop-onboarding'),
    bundleScript('desktop-onboarding'),
    onboardingDocument('browser-task'),
    bundleScript('browser-task'),
  ]);
  const fixtures = [];
  for (const [name, state] of Object.entries(DESKTOP_ONBOARDING_STATES)) {
    fixtures.push({
      surface: 'desktop-onboarding',
      name,
      state,
      urlPath: '/onboarding/index.html',
      html: onboardingHtml,
      script: onboardingScript,
      widths: [1440, 390],
      injectedSurface: true,
    });
  }
  for (const [name, state] of Object.entries(BROWSER_TASK_STATES)) {
    fixtures.push({
      surface: 'browser-task',
      name,
      state,
      urlPath: '/browser-tasks/index.html',
      html: browserTaskHtml,
      script: browserTaskScript,
      widths: [1440, 390],
      injectedSurface: true,
    });
  }
  return fixtures;
}
