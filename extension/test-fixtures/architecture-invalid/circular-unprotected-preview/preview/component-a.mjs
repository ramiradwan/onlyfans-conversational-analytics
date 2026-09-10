// Synthetic fixture: preview component-a imports component-b
import { componentB } from './component-b.mjs';

export function componentA() {
  return componentB();
}
