// Synthetic fixture: protocol frameB imports frameA, closing cycle
import { frameA } from './frame-a.mjs';

export function frameB() {
  return frameA();
}
