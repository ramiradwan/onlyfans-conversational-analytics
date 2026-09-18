// Build-only distribution of the pinned local faces to dependency-free HTML/CSS surfaces.
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const SOURCES = [
  { package: '@fontsource-variable/inter', stylesheet: 'opsz.css', file: 'inter-latin-opsz-normal.woff2' },
  { package: '@fontsource-variable/space-grotesk', stylesheet: 'wght.css', file: 'space-grotesk-latin-wght-normal.woff2' },
] as const;

/** Embed local font bytes so setup needs neither runtime assets nor a new network endpoint. */
export function generateStaticFontsCss(repositoryRoot: string): string {
  const sections = ['/* Generated from the pinned Fontsource packages and frontend/src/index.css. Do not edit. */'];
  for (const source of SOURCES) {
    const root = path.join(repositoryRoot, 'frontend/node_modules', source.package);
    const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')) as { version: string };
    const bytes = fs.readFileSync(path.join(root, 'files', source.file));
    if (bytes.subarray(0, 4).toString() !== 'wOF2') throw new Error('Expected a WOFF2 font: ' + source.file);
    const css = fs.readFileSync(path.join(root, source.stylesheet), 'utf8');
    const faces = [...css.matchAll(/@font-face\s*\{[^}]*\}/g)]
      .map(([face]) => face).filter((face) => face.includes('./files/' + source.file));
    if (faces.length !== 1) throw new Error('Expected one latin font face: ' + source.package);
    const license = fs.readFileSync(path.join(root, 'LICENSE'), 'utf8').trim().replaceAll('*/', '* /');
    const hash = createHash('sha256').update(bytes).digest('hex');
    sections.push(`/* ${source.package}@${manifest.version}; ${source.file}; sha256:${hash}\n${license}\n*/`);
    sections.push(faces[0].replace('./files/' + source.file, 'data:font/woff2;base64,' + bytes.toString('base64')));
  }
  // Reuse the owner's measured fallbacks verbatim instead of maintaining a second metric table.
  const indexCss = fs.readFileSync(path.join(repositoryRoot, 'frontend/src/index.css'), 'utf8');
  const fallbacks = [...indexCss.matchAll(/@font-face\s*\{[^}]*\}/g)].map(([face]) => face);
  if (fallbacks.length === 0 || fallbacks.some((face) => !face.includes('Variable Fallback'))) {
    throw new Error('Expected metric-matched fallback faces in frontend/src/index.css');
  }
  sections.push('/* Metric-matched local fallbacks copied from frontend/src/index.css. */', ...fallbacks);
  return sections.join('\n\n').replace(/\r\n/g, '\n') + '\n';
}

/** Start decoding the embedded first-paint faces before the browser lays out body text. */
export function replaceStaticFontPreloads(source: string, fontsCss: string, count = 2): string {
  const urls = [...fontsCss.matchAll(/url\((data:font\/woff2;base64,[^)]+)\)/g)].map((match) => match[1]);
  if (urls.length !== 2 || count < 1 || count > urls.length) throw new Error('Expected the pinned first-paint font URLs');
  const marker = /^([ \t]*)<!-- static-font-preloads:start -->[\s\S]*?<!-- static-font-preloads:end -->/gm;
  if ([...source.matchAll(marker)].length !== 1) throw new Error('Expected one static-font-preloads block');
  return source.replace(marker, (_, indent: string) => [
    indent + '<!-- static-font-preloads:start -->',
    ...urls.slice(0, count).map((href, index) => indent
      + `<link id="static-font-${index}" rel="preload" as="font" type="font/woff2" crossorigin href="${href}">`),
    indent + '<!-- static-font-preloads:end -->',
  ].join('\n'));
}
