/**
 * Frontend module-graph rules. `rule-bridge-no-canonical-writes` is documented in
 * docs/architecture-boundaries.md and is not checked here: Bridge reaches Brain over HTTP
 * and WebSocket, so the module graph has no backend imports to inspect.
 *
 * @type {import('dependency-cruiser').IConfiguration}
 */
module.exports = {
  forbidden: [
    {
      name: 'rule-bridge-protected-acyclic',
      comment:
        'Reject circular dependency chains through protected protocol, store, or service modules. This rule enforces acyclicity; separate rules govern dependency direction.',
      severity: 'error',
      from: {},
      to: {
        circular: true,
        via: '^(?:frontend/)?(?:src/)?(?:protocol|store|services)/',
      },
    },
  ],
  options: {
    doNotFollow: {
      path: 'node_modules',
    },
    exclude: {
      path: ['^tests/', '^test-fixtures/', '^dist/'],
    },
    tsConfig: {
      fileName: 'tsconfig.json',
    },
    moduleSystems: ['es6', 'cjs'],
  },
};
