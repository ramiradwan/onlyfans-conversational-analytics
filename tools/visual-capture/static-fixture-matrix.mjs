import { SURFACE_STATES } from '../../extension/qualification/surface-states.mjs';
import { BROWSER_TASK_STATES, DESKTOP_ONBOARDING_STATES } from './onboarding-surface-states.mjs';

export const desktopOnboardingWidths = Object.freeze([1440, 390, 320]);
const browserTaskWidths = Object.freeze([1440, 390, 320]);
export const SETUP_CODE_STATES = Object.freeze({
  setup_code_opening: Object.freeze({ state: 'setup_code.opening' }),
  setup_code_ready: Object.freeze({ state: 'setup_code.ready' }),
  setup_code_checking: Object.freeze({ state: 'setup_code.checking' }),
});

export function staticFixtureDescriptors() {
  return [
    ...Object.entries(SURFACE_STATES).map(([name, state]) => ({ surface: state.surface, name, state, widths: state.surface === 'popup' ? [390, 320] : [1440, 390] })),
    ...Object.entries(DESKTOP_ONBOARDING_STATES).map(([name, state]) => ({ surface: 'desktop-onboarding', name, state, widths: desktopOnboardingWidths })),
    ...Object.entries(BROWSER_TASK_STATES).map(([name, state]) => ({ surface: 'browser-task', name, state, widths: browserTaskWidths })),
    ...Object.entries(SETUP_CODE_STATES).map(([name, state]) => ({ surface: 'setup-code', name, state, widths: desktopOnboardingWidths })),
  ];
}

export function staticCases(fixtures = staticFixtureDescriptors()) {
  const complete = [...fixtures, ...fixtures.filter(item => item.name === 'preview').map(item => ({ ...item, sourceName: item.name, name: item.name + '-cold-assets', deliveryDelay: 1500 }))];
  return ['light', 'dark'].flatMap(mode => complete.flatMap(fixture => {
    const widths = ['browser-task', 'setup-code'].includes(fixture.surface) ? browserTaskWidths : fixture.widths;
    return widths.map(width => ({ mode, fixture, width }));
  }));
}
