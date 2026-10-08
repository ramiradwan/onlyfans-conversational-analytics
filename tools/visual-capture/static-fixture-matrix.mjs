import { SURFACE_STATES } from '../../extension/qualification/surface-states.mjs';

export const PROVISIONING_STATES = [
    ['connect', 'registration_required'], ['confirm', 'creator_confirmation_required'],
    ['approve', 'creator_approval_pending'], ['finish', 'finalization_ready'],
    ['invalid-code', 'registration_required'], ['link-unavailable', 'registration_required'],
    ['approval-unavailable', 'creator_approval_pending'], ['completed', null],
    ['recovery', 'recovery_required'],
    ['approval-pending', 'creator_approval_pending'], ['approval-offline', 'creator_approval_pending'],
    ['approval-unavailable-help', 'creator_approval_pending'],
  ];

export function staticFixtureDescriptors() {
  return [
    ...Object.entries(SURFACE_STATES).map(([name, state]) => ({ surface: state.surface, name, state, widths: state.surface === 'popup' ? [390, 320] : [1440, 390] })),
    ...PROVISIONING_STATES.map(([name, stage]) => ({ surface: 'provisioning', name, provisioning: { stage, name }, widths: [1440, 390] })),
  ];
}

export function staticCases(fixtures = staticFixtureDescriptors()) {
  const complete = [...fixtures, ...fixtures.filter(item => item.name === 'preview' || item.name === 'connect').map(item => ({ ...item, sourceName: item.name, name: item.name + '-cold-assets', deliveryDelay: 1500 }))];
  return ['light', 'dark'].flatMap(mode => complete.flatMap(fixture => fixture.widths.map(width => ({ mode, fixture, width }))));
}
