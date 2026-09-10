// Synthetic fixture: capture directly imports consent-controller
import { ConsentController } from '../runtime/consent-controller.mjs';

export function observeWithConsent(event) {
  return ConsentController.check(event);
}
