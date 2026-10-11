import { useEffect, useRef, useState } from 'react';

import { OnboardingPasskeyTask } from '../components/OnboardingPasskeyTask';
import { EnrollmentOutcomeError, LoginOutcomeError, webauthnApi, type WebAuthnApi } from '../services/webauthnApi';

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
  const [checkAttempt, setCheckAttempt] = useState(0);
  const ceremony = useRef<AbortController | null>(null);
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
    setChecking(true); setEnrolled(null); setError(null);
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
  }, [api, checkAttempt]);
  useEffect(() => () => { ceremony.current?.abort(); ceremony.current = null; }, [api]);

  const authenticate = async (enroll: boolean) => {
    if (busy || checking || ceremony.current || api.state && enrolled === null) return;
    const controller = new AbortController();
    ceremony.current = controller;
    setBusy(true);
    setError(null);
    try {
      if (enroll) await api.enroll(controller.signal);
      else await api.login(controller.signal);
      if (!controller.signal.aborted) authenticated.current();
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (cause instanceof EnrollmentOutcomeError && cause.enrolled) setEnrolled(true);
      setError(cause instanceof EnrollmentOutcomeError && cause.enrolled
        ? 'Passkey created. Sign in to continue.'
        : cause instanceof LoginOutcomeError && cause.stage === 'begin'
          ? 'Sign-in could not start. Try again.' : failureMessage(enroll));
    } finally {
      if (ceremony.current === controller) ceremony.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return <OnboardingPasskeyTask statusUnavailable={Boolean(api.state) && enrolled === null && !checking}
    recheck={() => setCheckAttempt((value) => value + 1)} busy={busy} checking={checking} enrolled={enrolled} error={error} authenticate={authenticate} />;
}
