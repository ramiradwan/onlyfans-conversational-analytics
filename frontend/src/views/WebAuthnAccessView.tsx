import { useEffect, useRef, useState } from 'react';

import {
  AccessFollower, EnrollmentOutcomeError, lapsedAccess, LoginOutcomeError, PASSKEY_STATUS_RETRY_DELAYS, WebAuthnStatusError, webauthnApi,
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
  const [recoveringStatus, setRecoveringStatus] = useState(false);
  const [readingStatus, setReadingStatus] = useState(false);
  const reading = useRef(false);
  const completed = useRef(false);
  const recovery = useRef(false);
  const recoveryWasEnrollment = useRef(false);
  const statusRetries = useRef(0);
  const ceremony = useRef<AbortController | null>(null);
  const authenticated = useRef(onAuthenticated);
  authenticated.current = onAuthenticated;
  const shownEnrolled = useRef(enrolled);
  shownEnrolled.current = enrolled;
  // A read asked for by a pushed change keeps the current screen until it answers.
  const quiet = useRef(false);
  const [follower] = useState(() => new AccessFollower(() => {
    if (completed.current) return;
    quiet.current = true;
    setCheckAttempt(value => value + 1);
  }));
  useEffect(() => {
    if (!api.state || completed.current) return;
    const controller = new AbortController();
    const silent = quiet.current;
    quiet.current = false;
    let mounted = true;
    let settled = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const finish = (outcome: PushedAccess | null) => { settled = true; follower.release(outcome); };
    const retryStatus = (message: string) => {
      const delay = PASSKEY_STATUS_RETRY_DELAYS[statusRetries.current];
      setRecoveringStatus(delay !== undefined);
      if (delay === undefined) { setError(message); return; }
      statusRetries.current += 1;
      setError(null);
      retry = setTimeout(() => setCheckAttempt(value => value + 1), delay);
    };
    const timeout = setTimeout(() => {
      controller.abort();
      if (!mounted) return;
      reading.current = false;
      setReadingStatus(false);
      setChecking(false); setEnrolled(null); setAccess(null);
      retryStatus('Dipsy can’t check access on this computer right now.');
      finish(null);
    }, 8000);
    follower.hold();
    reading.current = true;
    setReadingStatus(true);
    if (!silent) { setChecking(true); setEnrolled(null); setAccess(null); setError(null); }
    void api.state(controller.signal).then((state) => {
      if (controller.signal.aborted) return;
      statusRetries.current = 0;
      setRecoveringStatus(false);
      // Reconcile a possibly committed ceremony without starting a new one.
      if (recovery.current) {
        setError(state.authenticated ? null : recoveryWasEnrollment.current && state.enrolled
          ? 'Passkey created. Sign in to continue.' : failureMessage(recoveryWasEnrollment.current));
        recovery.current = false;
      } else if (shownEnrolled.current === null) setError(null);
      setEnrolled(state.enrolled); setAccess(null);
      if (state.authenticated) {
        completed.current = true;
        finish('available');
        authenticated.current();
      } else finish('available');
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      const lapsed = lapsedAccess(cause);
      setEnrolled(null); setAccess(lapsed);
      if (lapsed) { setRecoveringStatus(false); setError(null); finish(lapsed); return; }
      const kind = cause instanceof WebAuthnStatusError ? cause.kind : 'invalid';
      if (kind === 'network' || kind === 'unavailable') {
        retryStatus('Dipsy can’t check access on this computer right now.');
      } else {
        setRecoveringStatus(false);
        setError(kind === 'session' ? 'This app session is no longer authorized.'
          : kind === 'forbidden' ? 'Dipsy isn’t allowed to check access on this computer.'
            : 'Dipsy can’t confirm passkey status on this computer.');
        settled = true;
        follower.releaseRefused();
        return;
      }
      finish(cause instanceof WebAuthnStatusError ? cause.access : null);
    }).finally(() => {
      clearTimeout(timeout);
      if (!controller.signal.aborted) {
        reading.current = false;
        setReadingStatus(false);
        setChecking(false);
      }
    });
    return () => {
      mounted = false; reading.current = false; controller.abort(); clearTimeout(timeout);
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
  useEffect(() => () => {
    ceremony.current?.abort();
    ceremony.current = null;
    completed.current = false;
    recovery.current = false;
    statusRetries.current = 0;
  }, [api]);

  const authenticate = async (enroll: boolean) => {
    if (busy || checking || reading.current || recovery.current || completed.current || access || ceremony.current || api.state && enrolled === null) return;
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
      if (!controller.signal.aborted) { signedIn = true; completed.current = true; authenticated.current(); }
    } catch (cause) {
      if (controller.signal.aborted) return;
      const unknown = (cause instanceof EnrollmentOutcomeError && cause.enrolled === null)
        || (cause instanceof LoginOutcomeError && cause.outcomeUnknown);
      if (unknown && api.state) {
        recovery.current = true;
        recoveryWasEnrollment.current = enroll;
        statusRetries.current = 0;
        setEnrolled(null);
        setRecoveringStatus(true);
        setError(null);
        setCheckAttempt(value => value + 1);
      } else {
        if (cause instanceof EnrollmentOutcomeError) setEnrolled(cause.enrolled);
        setError(cause instanceof EnrollmentOutcomeError && cause.enrolled
          ? 'Passkey created. Sign in to continue.'
          : cause instanceof LoginOutcomeError && cause.stage === 'begin'
            ? 'Sign-in could not start.' : failureMessage(enroll));
      }
    } finally {
      if (ceremony.current === controller) ceremony.current = null;
      if (!controller.signal.aborted) setBusy(false);
      if (controller.signal.aborted) follower.abandon();
      else if (signedIn) follower.release('available');
      else follower.release();
    }
  };

  const checkingStatus = checking || recoveringStatus;
  return <OnboardingPasskeyTask statusUnavailable={Boolean(api.state) && enrolled === null && !checkingStatus && access === null}
    busy={busy} checking={checkingStatus} readingStatus={readingStatus} enrolled={enrolled}
    error={error} access={access} authenticate={authenticate} />;
}
