export const ACTIVATION_SCENARIO_REGISTRY = Object.freeze([
  Object.freeze({
    id: 'AE-01',
    test_name: 'Terms acceptance survives abandonment without fabricating selected_mode',
    result_file: 'scenario-results/AE-01.json',
    evidence_files: ['evidence/ae-01-terms-abandoned.json'],
  }),
  Object.freeze({
    id: 'AE-02',
    test_name: 'Terms and risk remain separate durable pre-mode records without a v2 envelope',
    result_file: 'scenario-results/AE-02.json',
    evidence_files: ['evidence/ae-02-terms-risk-abandoned.json'],
  }),
  Object.freeze({
    id: 'AE-03',
    test_name: 'Preview v2 preserves original Terms and risk timestamps',
    result_file: 'scenario-results/AE-03.json',
    evidence_files: ['evidence/ae-03-preview-envelope.json'],
  }),
  Object.freeze({
    id: 'AE-04',
    test_name: 'Full v2 uses affirmative authorization and original pre-mode timestamps',
    result_file: 'scenario-results/AE-04.json',
    evidence_files: ['evidence/ae-04-full-initial-authorization.json'],
    runtime_proof: true,
  }),
  Object.freeze({
    id: 'AE-05',
    test_name: 'Preview to Full creates a distinct later envelope without rewriting Preview',
    result_file: 'scenario-results/AE-05.json',
    evidence_files: ['evidence/ae-05-preview-to-full.json'],
    runtime_proof: true,
  }),
  Object.freeze({
    id: 'AE-06',
    test_name: 'Real revoke transition leaves Terms, risk, and Full evidence byte-for-byte unchanged',
    result_file: 'scenario-results/AE-06.json',
    evidence_files: ['evidence/ae-06-revocation.json'],
  }),
  Object.freeze({
    id: 'AE-07',
    test_name: 'Mode write and controller crash retry preserve the original evidence event',
    result_file: 'scenario-results/AE-07.json',
    evidence_files: ['evidence/ae-07-crash-retry.json'],
  }),
  Object.freeze({
    id: 'AE-08',
    test_name: 'Instrument changes require new acceptance rather than silent rebinding',
    result_file: 'scenario-results/AE-08.json',
    evidence_files: ['evidence/ae-08-instrument-binding-negative.json'],
  }),
  Object.freeze({
    id: 'AE-09',
    test_name: 'Exact schema-v2 lock rejects an envelope without selected_mode',
    result_file: 'scenario-results/AE-09.json',
    evidence_files: ['evidence/ae-09-schema-v2-lock.json'],
  }),
]);

export function scenarioById(id) {
  return ACTIVATION_SCENARIO_REGISTRY.find((scenario) => scenario.id === id) ?? null;
}
