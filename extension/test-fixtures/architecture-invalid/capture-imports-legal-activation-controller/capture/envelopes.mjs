// Synthetic fixture: capture directly imports legal-activation-controller
import { LegalActivationController } from '../runtime/legal-activation-controller.mjs';

export function observeActive(event) {
  return LegalActivationController.isActive(event);
}
