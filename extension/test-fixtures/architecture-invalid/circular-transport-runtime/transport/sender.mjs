// Synthetic fixture: transport imports runtime handler
import { handleRuntimeEvent } from '../runtime/handler.mjs';

export function sendEvent(evt) {
  return handleRuntimeEvent(evt);
}
