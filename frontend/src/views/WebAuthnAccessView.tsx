import { useEffect, useRef, useState } from 'react';

import { OnboardingPasskeyTask } from '../components/OnboardingPasskeyTask';
import { EnrollmentOutcomeError, webauthnApi, type WebAuthnApi } from '../services/webauthnApi';

function failureMessage(enroll: boolean): string {
  // NotAllowedError does not uniquely establish cancellation, expiry or why
  // the browser refused a ceremony. State only the observed incomplete step.
  return enroll
    ? 'Passkey setup could not be confirmed.'
    : 'Sign-in did not finish.';
}

interface WebAuthnAccessViewProps {
  api?: WebAuthnApi;
  onAuthenticated?: () => void;
}

export function WebAuthnAccessView({
  api = webauthnApi,
  onAuthenticated = () => window.location.reload(),
}: WebAuthnAccessViewProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [enrolled, setEnrolled] = useState<boolean | null>(null);
  const [checking, setChecking] = useState(Boolean(api.state));
  const authenticated = useRef(onAuthenticated);
  authenticated.current = onAuthenticated;
  useEffect(() => {
    if (!api.state) return;
    const controller = new AbortController();
    let mounted = true;
    const timeout = setTimeout(() => {
      controller.abort();
      if (mounted) { setChecking(false); setError('Passkey status could not be checked.'); }
    }, 8000);
    setChecking(true); setEnrolled(null);
    void api.state(controller.signal).then((state) => {
      if (controller.signal.aborted) return;
      setEnrolled(state.enrolled);
      if (state.authenticated) authenticated.current();
    }).catch(() => {
      if (!controller.signal.aborted) setError('Passkey status could not be checked.');
    }).finally(() => {
      clearTimeout(timeout);
      if (!controller.signal.aborted) setChecking(false);
    });
    return () => { mounted = false; controller.abort(); clearTimeout(timeout); };
  }, [api]);

  const authenticate = async (enroll: boolean) => {
    if (busy || checking) return;
    setBusy(true);
    setError(null);
    try {
      if (enroll) await api.enroll();
      else await api.login();
      onAuthenticated();
    } catch (cause) {
      setError(cause instanceof EnrollmentOutcomeError && cause.enrolled
        ? 'Passkey created. Sign in to continue.' : failureMessage(enroll));
    } finally {
      setBusy(false);
    }
  };

  return <OnboardingPasskeyTask busy={busy} checking={checking} enrolled={enrolled} error={error} authenticate={authenticate} />;
}
