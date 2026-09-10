// Synthetic fixture: capture directly imports legal-consent-authorization
import { LegalConsentAuthorization } from '../runtime/legal-consent-authorization.mjs';

export function authorizeCapture(event) {
  return LegalConsentAuthorization.authorize(event);
}
