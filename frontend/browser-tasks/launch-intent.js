import { UUID } from '../../shared/browser-tasks/wire.mjs';

/** Spend native entry intent before startup; reloads and recovery need an explicit action. */
export function consumeLaunchIntent(generation, storage = sessionStorage, navigation = performance.getEntriesByType('navigation')) {
  try {
    const expected = storage.getItem('browser-task-launch');
    storage.removeItem('browser-task-launch');
    return UUID.test(generation) && expected === generation && navigation.length === 1 && navigation[0].type === 'navigate';
  } catch { return false; }
}
