// Release tooling will replace the null production binding with exact approved
// Legal version/rendered-SHA-256/public-route/locale metadata. The private
// service-worker global is an E2E seam only: no extension message exposes a
// setter and ordinary builds therefore fail closed while Legal is unbound.
export function legalReleaseBindings() {
  return globalThis.__OFCA_TEST_LEGAL_RELEASE_BINDINGS__ ?? null;
}
