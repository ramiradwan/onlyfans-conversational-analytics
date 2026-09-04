// A build given --legal-release-bindings replaces this module with the verified
// document, so the null here is the unbound state. The private
// service-worker global is an E2E seam only: no extension message exposes a
// setter and ordinary builds therefore fail closed while Legal is unbound.
export function legalReleaseBindings() {
  return globalThis.__OFCA_TEST_LEGAL_RELEASE_BINDINGS__ ?? null;
}
