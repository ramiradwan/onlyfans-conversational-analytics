import { useEffect, useRef, useState } from 'react';

import {
  AccessFollower, EnrollmentOutcomeError, lapsedAccess, LoginOutcomeError, WebAuthnStatusError, webauthnApi,
  type AccessState, type PushedAccess, type WebAuthnApi,
} from '../../shared-setup/webauthn';
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
  const [access, setAccess] = useState<AccessState | null>(null);
  const [checking, setChecking] = useState(Boolean(api.state));
  const [checkAttempt, setCheckAttempt] = useState(0);
  const statusRetries = useRef(0);
  const ceremony = useRef<AbortController | null>(null);
  const authenticated = useRef(onAuthenticated);
  authenticated.current = onAuthenticated;
  const shownEnrolled = useRef(enrolled);
  shownEnrolled.current = enrolled;
  // A read asked for by a pushed change keeps the current screen until it answers.
  const quiet = useRef(false);
  const [follower] = useState(() => new AccessFollower(() => { quiet.current = true; setCheckAttempt(value => value + 1); }));
  useEffect(() => {
    if (!api.state) return;
    const controller = new AbortController();
    const silent = quiet.current;
    quiet.current = false;
    let mounted = true;
    let settled = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const finish = (outcome: PushedAccess | null) => { settled = true; follower.release(outcome); };
    const retryStatus = () => {
      if (statusRetries.current < 2) retry = setTimeout(() => { statusRetries.current += 1; setCheckAttempt(value => value + 1); }, 1000 * 2 ** statusRetries.current);
    };
    const timeout = setTimeout(() => {
      controller.abort();
      if (!mounted) return;
      setChecking(false); setEnrolled(null); setAccess(null);
      setError('The local service did not answer the passkey status request.');
      retryStatus();
      finish(null);
    }, 8000);
    follower.hold();
    if (!silent) { setChecking(true); setEnrolled(null); setAccess(null); setError(null); }
    void api.state(controller.signal).then((state) => {
      if (controller.signal.aborted) return;
      statusRetries.current = 0;
      // Keep a ceremony's message when the read only confirms the shown state.
      if (shownEnrolled.current === null) setError(null);
      setEnrolled(state.enrolled); setAccess(null);
      if (state.authenticated) { settled = true; authenticated.current(); }
      else finish('available');
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      const lapsed = lapsedAccess(cause);
      setEnrolled(null); setAccess(lapsed);
      if (lapsed) { setError(null); finish(lapsed); return; }
      const kind = cause instanceof WebAuthnStatusError ? cause.kind : 'unavailable';
      setError(kind === 'network' ? 'The local service could not be reached to check passkey setup.'
        : kind === 'session' ? 'The passkey status request needs a current app session.'
          : kind === 'forbidden' ? 'The local service refused access to passkey status.'
            : kind === 'invalid' ? 'The local service returned an invalid passkey status.'
              : 'The local service could not check passkey setup.');
      if (kind === 'network' || kind === 'unavailable') retryStatus();
      finish(cause instanceof WebAuthnStatusError ? cause.access : null);
    }).finally(() => {
      clearTimeout(timeout);
      if (!controller.signal.aborted) setChecking(false);
    });
    return () => {
      mounted = false; controller.abort(); clearTimeout(timeout);
      if (retry !== null) clearTimeout(retry);
      if (!settled) follower.abandon();
    };
  }, [api, checkAttempt, follower]);
  useEffect(() => {
    if (!api.state || !api.watch) return;
    const controller = new AbortController();
    void api.watch(controller.signal, value => follower.push(value));
    return () => controller.abort();
  }, [api, follower]);
  useEffect(() => () => { ceremony.current?.abort(); ceremony.current = null; }, [api]);

  const authenticate = async (enroll: boolean) => {
    if (busy || checking || access || ceremony.current || api.state && enrolled === null) return;
    const controller = new AbortController();
    ceremony.current = controller;
    // A change pushed during the ceremony is read once it ends.
    follower.hold();
    let signedIn = false;
    setBusy(true);
    setError(null);
    try {
      if (enroll) await api.enroll(controller.signal);
      else await api.login(controller.signal);
      if (!controller.signal.aborted) { signedIn = true; authenticated.current(); }
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
      if (controller.signal.aborted) follower.abandon();
      else if (!signedIn) follower.release();
    }
  };

  return <OnboardingPasskeyTask statusUnavailable={Boolean(api.state) && enrolled === null && !checking && access === null}
    recheck={() => { statusRetries.current = 0; setCheckAttempt((value) => value + 1); }} busy={busy} checking={checking} enrolled={enrolled} error={error} access={access} authenticate={authenticate} />;
}
