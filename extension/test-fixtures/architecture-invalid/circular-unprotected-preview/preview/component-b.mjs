// Synthetic fixture: preview component-b imports component-a
import { componentA } from './component-a.mjs';

export function componentB() {
  return componentA();
}
