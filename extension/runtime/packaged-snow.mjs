import initialize, { SnowSession, generate_static_keypair } from '../vendor/companion-snow/ofca_snow_wasm.js';

let loading;
export function loadPackagedSnow() {
  loading ??= initialize({ module_or_path: chrome.runtime.getURL('ofca_snow_wasm_bg.wasm') })
    .then(() => Object.freeze({ SnowSession, generateStaticKeypair: generate_static_keypair }))
    .catch(() => { loading = null; throw new Error('companion_crypto_unavailable'); });
  return loading;
}
