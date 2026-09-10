// Synthetic fixture: protocol frameA imports frameB
import { frameB } from './frame-b.mjs';

export function frameA() {
  return frameB();
}
