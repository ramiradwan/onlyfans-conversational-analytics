/** Reject runtime or browser dependencies in every chunk reachable by desktop setup. */
export function assertSetupIsolation(bundle) {
  assertIsolation(bundle, 'onboarding', ['/frontend/browser-tasks/', '/frontend/setup-code/'], true);
}
export function assertBrowserTaskIsolation(bundle) {
  assertIsolation(bundle, 'browser-tasks', ['/frontend/onboarding/', '/frontend/setup-code/']);
}
export function assertReceivingIsolation(bundle) {
  assertIsolation(bundle, 'setup-code', ['/frontend/onboarding/', '/frontend/browser-tasks/'], true);
}
export function assertDashboardIsolation(bundle) {
  assertIsolation(bundle, '', ['/frontend/browser-tasks/', '/frontend/setup-code/'], true, false);
}
function assertIsolation(bundle, surface, forbidden, noChrome = false, restricted = true) {
  const entry = Object.values(bundle).find(item => item.type === 'chunk' && item.isEntry
    && item.facadeModuleId?.replaceAll('\\', '/').endsWith('/frontend/' + (surface ? surface + '/' : '') + 'index.html'));
  if (!entry) throw new Error('Desktop setup build entry is missing');
  const pending = [entry.fileName], seen = new Set();
  while (pending.length) {
    const name = pending.pop();
    if (seen.has(name)) continue;
    seen.add(name);
    const chunk = bundle[name];
    if (!chunk || chunk.type !== 'chunk') throw new Error('Desktop setup build dependency is missing');
    if (noChrome && /\b(?:chrome|browser)\s*(?:(?:\?\.|\.)\s*(?:runtime|extension)|(?:\?\.)?\[\s*['"](?:runtime|extension))/u.test(chunk.code ?? '')) {
      throw new Error('Native frontend cannot reference Chrome APIs: ' + name);
    }
    for (const id of Object.keys(chunk.modules)) {
      const path = id.replaceAll('\\', '/');
      if ((restricted && path.includes('/frontend/src/') && !path.includes('/frontend/src/theme/generated/'))
        || forbidden.some(prefix => path.includes(prefix))
        || (restricted && /\/node_modules\/(react|react-dom|@mui|zustand|ajv|ajv-standalone)\//u.test(path))) {
        throw new Error('Desktop setup imports a runtime or browser dependency: ' + path);
      }
    }
    pending.push(...chunk.imports, ...chunk.dynamicImports);
  }
}
