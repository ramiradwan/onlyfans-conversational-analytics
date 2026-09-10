/** @type {import(\'dependency-cruiser\').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: 'rule-bridge-protected-acyclic',
      comment:
        'Reject circular dependency chains passing through protocol, store, or services. Phase-one structural protection; does not prove correct dependency direction.',
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
