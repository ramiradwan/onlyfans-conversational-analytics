// Build-time checks for the currently reserved financial family and build-only code.
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

const buildModules = ['generate-theme', 'intent-contracts', 'color-validation', 'token-consumers'];
const financialCss = /--(?:bridge-palette|dipsy-intent|dipsy-color)-financial(?:-|\b)/;

export function validateConsumerSource(source: string, filename: string): void {
  const fail = (message: string): never => { throw new Error(filename + ': ' + message); };
  if (financialCss.test(source)) fail('financial color tokens are reserved');
  if (/color-mix\(\s*in\s+srgb\b/i.test(source)) fail('governed color mixing must use OKLCH');
  if (!/\.[cm]?[jt]sx?$/.test(filename)) return;
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, /[jt]sx$/.test(filename) ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  function visit(node: ts.Node): void {
    if (ts.isIdentifier(node) && node.text === 'augmentColor') fail('Bridge owns explicit palette tones; augmentColor is not allowed');
    if (ts.isPropertyAccessExpression(node) && node.name.text === 'financial') fail('financial color tokens are reserved');
    if (ts.isElementAccessExpression(node) && ts.isStringLiteralLike(node.argumentExpression) && node.argumentExpression.text === 'financial') fail('financial color tokens are reserved');
    if (ts.isBindingElement(node) && (node.propertyName ?? node.name).getText(file).replace(/['"]/g, '') === 'financial') fail('financial color tokens are reserved');
    if (ts.isStringLiteralLike(node) || ts.isTemplateHead(node)) {
      if (node.text === 'financial' || node.text.startsWith('financial.')) fail('financial color tokens are reserved');
      if (node.text === 'colorjs.io' || node.text.startsWith('colorjs.io/')) fail('Color.js is build-only');
      if (/(?:^|\/)tokens\.json$/.test(node.text)) fail('authored token source is build-only');
      if (buildModules.some((name) => new RegExp('(?:^|/)' + name + '(?:\\.[cm]?[jt]s)?$').test(node.text))) fail('theme validation modules are build-only');
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
}

export function validateRepositoryConsumers(repositoryRoot: string): void {
  const buildFiles = new Set(buildModules.map((name) => path.join(repositoryRoot, 'frontend/src/theme', name + '.ts')));
  function walk(directory: string): void {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        if (!['generated', 'node_modules', 'dist'].includes(entry.name)) walk(filename);
      } else if (entry.isFile() && /\.(ts|tsx|js|jsx|css)$/.test(filename) && !buildFiles.has(filename)) {
        validateConsumerSource(fs.readFileSync(filename, 'utf8'), filename);
      }
    }
  }
  walk(path.join(repositoryRoot, 'frontend/src'));
  walk(path.join(repositoryRoot, 'frontend/.design-sync'));
  for (const relative of ['extension/popup.css', 'app/provisioning/provisioning.html']) {
    const filename = path.join(repositoryRoot, relative);
    validateConsumerSource(fs.readFileSync(filename, 'utf8'), filename);
  }
}
