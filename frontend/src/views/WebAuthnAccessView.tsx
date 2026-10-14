import { useEffect, useRef, useState } from 'react';

import { EnrollmentOutcomeError, LoginOutcomeError, WebAuthnStatusError, webauthnApi, type WebAuthnApi } from '../../shared-setup/webauthn';
import { OnboardingPasskeyTask } from '../components/OnboardingPasskeyTask';

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
  const statusRetries = useRef(0);
  const ceremony = useRef<AbortController | null>(null);
  const authenticated = useRef(onAuthenticated);
  authenticated.current = onAuthenticated;
  useEffect(() => {
    if (!api.state) return;
    const controller = new AbortController();
    let mounted = true;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const retryStatus = () => {
      if (statusRetries.current < 2) retry = setTimeout(() => { statusRetries.current += 1; setCheckAttempt(value => value + 1); }, 1000 * 2 ** statusRetries.current);
    };
    const timeout = setTimeout(() => {
      controller.abort();
      if (mounted) { setChecking(false); setError('The local service did not answer the passkey status request.'); retryStatus(); }
    }, 8000);
    setChecking(true); setEnrolled(null); setError(null);
    void api.state(controller.signal).then((state) => {
      if (controller.signal.aborted) return;
      statusRetries.current = 0;
      setEnrolled(state.enrolled);
      if (state.authenticated) authenticated.current();
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      const kind = cause instanceof WebAuthnStatusError ? cause.kind : 'unavailable';
      setError(kind === 'network' ? 'The local service could not be reached to check passkey setup.'
        : kind === 'session' ? 'The passkey status request needs a current app session.'
          : kind === 'forbidden' ? 'The local service refused access to passkey status.'
            : kind === 'invalid' ? 'The local service returned an invalid passkey status.'
              : 'The local service could not check passkey setup.');
      if (kind === 'network' || kind === 'unavailable') retryStatus();
    }).finally(() => {
      clearTimeout(timeout);
      if (!controller.signal.aborted) setChecking(false);
    });
    return () => { mounted = false; controller.abort(); clearTimeout(timeout); if (retry !== null) clearTimeout(retry); };
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
      if (cause instanceof EnrollmentOutcomeError) setEnrolled(cause.enrolled);
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
    recheck={() => { statusRetries.current = 0; setCheckAttempt((value) => value + 1); }} busy={busy} checking={checking} enrolled={enrolled} error={error} authenticate={authenticate} />;
}
