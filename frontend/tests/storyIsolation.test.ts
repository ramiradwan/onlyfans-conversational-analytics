import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const sourceRoot = path.resolve(process.cwd(), 'src');
const aliases = new Map([
  ['@components', path.join(sourceRoot, 'components')],
  ['@services', path.join(sourceRoot, 'services')],
  ['@routing', path.join(sourceRoot, 'routing')],
  ['@layouts', path.join(sourceRoot, 'layouts')],
  ['@config', path.join(sourceRoot, 'config')],
  ['@common', path.join(sourceRoot, 'common')],
  ['@assets', path.join(sourceRoot, 'assets')],
  ['@hooks', path.join(sourceRoot, 'hooks')],
  ['@store', path.join(sourceRoot, 'store')],
  ['@theme', path.join(sourceRoot, 'theme')],
  ['@types', path.join(sourceRoot, 'types')],
  ['@utils', path.join(sourceRoot, 'utils')],
  ['@views', path.join(sourceRoot, 'views')],
  ['@', sourceRoot],
]);
const extensions = ['', '.ts', '.tsx', '.js', '.jsx', '.css', '.json'];

function resolveSource(specifier: string, importer: string): string | null {
  let base: string | null = null;
  if (specifier.startsWith('.')) {
    base = path.resolve(path.dirname(importer), specifier);
  } else {
    for (const [alias, directory] of aliases) {
      if (specifier === alias || specifier.startsWith(alias + '/')) {
        base = path.join(directory, specifier.slice(alias.length));
        break;
      }
    }
  }
  if (base === null) return null;

  const candidates = extensions.flatMap((extension) => [
    base + extension,
    path.join(base, 'index' + extension),
  ]);
  const resolved = candidates.find(
    (candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile(),
  );
  if (!resolved) throw new Error(`Could not resolve ${specifier} from ${importer}`);
  return resolved;
}

function productionDependencyGraph(entry: string): Set<string> {
  const visited = new Set<string>();
  const visit = (file: string) => {
    const normalized = path.resolve(file);
    if (visited.has(normalized)) return;
    visited.add(normalized);
    const source = fs.readFileSync(normalized, 'utf8');
    const imports = ts.preProcessFile(source, true, true).importedFiles;
    for (const imported of imports) {
      const dependency = resolveSource(imported.fileName, normalized);
      if (dependency) visit(dependency);
    }
  };
  visit(entry);
  return visited;
}

describe('story-only production isolation', () => {
  it('keeps story modules and fixture strings out of the production entry graph', () => {
    const graph = productionDependencyGraph(path.join(sourceRoot, 'main.tsx'));
    const relativeFiles = [...graph].map((file) => path.relative(sourceRoot, file));
    const productionSource = [...graph]
      .map((file) => fs.readFileSync(file, 'utf8'))
      .join('\n');

    expect(relativeFiles.some((file) => file.startsWith(`story-only${path.sep}`))).toBe(false);
    expect(productionSource).not.toContain('STORY ONLY · synthetic values · not product data');
    expect(productionSource).not.toContain('story-only-account');
    expect(productionSource).not.toContain('Synthetic response showing the conversation bubble width.');

    const routeSource = fs.readFileSync(
      path.join(sourceRoot, 'routing', 'useAppRoutes.tsx'),
      'utf8',
    );
    expect(routeSource).not.toMatch(/story-only|visual-harness/i);
  });
});
