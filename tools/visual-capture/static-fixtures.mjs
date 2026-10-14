import { SURFACE_STATES, surfaceDocument, surfaceBundle } from '../../extension/qualification/surface-fixtures.mjs';

export async function staticFixtures() {
  const fixtures = [];
  for (const [name, input] of Object.entries(SURFACE_STATES)) {
    const state = ['software_activation', 'software_activation_ready'].includes(name) ? { ...input, hash: 'full' } : input;
    fixtures.push({ surface: state.surface, name, state,
      html: await surfaceDocument(state), script: await surfaceBundle(state.surface),
      widths: state.surface === 'popup' ? [390, 320] : [1440, 390] });
  }
  return fixtures;
}
