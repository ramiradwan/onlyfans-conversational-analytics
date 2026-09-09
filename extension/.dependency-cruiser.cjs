/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: 'rule-agent-capture-isolation',
      comment:
        'Rule A: Capture modules must produce observations only and must not import transport implementations or runtime control implementations (consent-controller, legal-activation-controller, legal-consent-authorization).',
      severity: 'error',
      from: {
        path: '^(?:extension/)?capture/',
      },
      to: {
        path: [
          '^(?:extension/)?transport/',
          '^(?:extension/)?runtime/consent-controller\\.mjs$',
          '^(?:extension/)?runtime/legal-activation-controller\\.mjs$',
          '^(?:extension/)?runtime/legal-consent-authorization\\.mjs$',
        ],
      },
    },
    {
      name: 'rule-agent-protected-acyclic',
      comment:
        'Rule B: Reject circular dependency paths passing through protocol, transport, or runtime. Phase-one structural protection; does not prove correct dependency direction.',
      severity: 'error',
      from: {},
      to: {
        circular: true,
        via: '^(?:extension/)?(?:protocol|transport|runtime)/',
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
    moduleSystems: ['es6', 'cjs'],
  },
};
