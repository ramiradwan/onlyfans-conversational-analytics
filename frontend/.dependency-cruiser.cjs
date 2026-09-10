/** @type {import(\'dependency-cruiser\').IConfiguration} */
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
