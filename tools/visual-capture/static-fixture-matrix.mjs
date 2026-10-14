import { SURFACE_STATES } from '../../extension/qualification/surface-states.mjs';

export function staticFixtureDescriptors() {
  return [
    ...Object.entries(SURFACE_STATES).map(([name, state]) => ({ surface: state.surface, name, state, widths: state.surface === 'popup' ? [390, 320] : [1440, 390] })),
  ];
}

export function staticCases(fixtures = staticFixtureDescriptors()) {
  const complete = [...fixtures, ...fixtures.filter(item => item.name === 'preview').map(item => ({ ...item, sourceName: item.name, name: item.name + '-cold-assets', deliveryDelay: 1500 }))];
  return ['light', 'dark'].flatMap(mode => complete.flatMap(fixture => fixture.widths.map(width => ({ mode, fixture, width }))));
}
