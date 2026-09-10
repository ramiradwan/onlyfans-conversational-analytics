// Synthetic fixture: runtime imports transport sender, closing cycle
import { sendEvent } from '../transport/sender.mjs';

export function handleRuntimeEvent(evt) {
  return sendEvent(evt);
}
