import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import { Box, Button, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SettingRow } from './ui';

import type { BrowserSurfacePayload } from '../protocol';
import {
  BrowserControlApiError,
  type BrowserControlApi,
  type CaptureAction,
  type BrowserDestination,
} from '../services/browserControlApi';
import { desktopOnboardingApi, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import type { ExtensionConnection } from '../utils/statusCopy';
import { StatusLine } from './ui/ReservedRegion';

// How long to wait for the extension's pushed state before reporting no response.
const RESPONSE_DEADLINE_MS = 10_000;

type Notice = 'unreachable' | 'failed' | 'no_response' | null;

/**
 * Bridge owns day-to-day extension controls while an extension session is open
 * (ADR 0045). State shown here is only what the extension pushed; a click never
 * changes it locally. Permission prompts open the extension's own page, because
 * the browser requires the click there.
 */
export function BrowserExtensionControls({ api, browser, canManage, connection, onboarding = desktopOnboardingApi, onFeedback, continuationCreatorId }: {
  api: BrowserControlApi;
  browser: BrowserSurfacePayload | null;
  canManage: boolean;
  /** Ingestion link state, used when no browser surface is available. */
  connection: ExtensionConnection;
  onboarding?: DesktopOnboardingApi;
  /** Settings combines its control, pairing and connection-list feedback. */
  onFeedback?: (text: string | null) => void;
  /** A dashboard continuation, scoped to the current authenticated creator. */
  continuationCreatorId?: string;
}) {
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const continuation = continuationCreatorId !== undefined;
  const snapshot = desktop.snapshot;
  const currentAuthority = desktop.connected && snapshot?.analysis_readiness.availability === 'current'
    && snapshot.analysis_readiness.creator_account_id === continuationCreatorId;
  const reviewOperation = snapshot?.pending_operations.find(value => value.action === 'browser.access');
  const canReview = canManage && currentAuthority && !reviewOperation && !desktop.pending
    && !desktop.browserLaunchPending && snapshot?.allowed_actions.includes('browser.access');
  const currentPresentation = () => {
    const current = onboarding.getState();
    return current.connected && current.snapshot === snapshot;
  };
  const reviewInFlight = useRef(false);
  const [openingReview, setOpeningReview] = useState(false);
  const [pending, setPending] = useState<CaptureAction | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const operation = useRef<AbortController | null>(null);
  const controlAvailable = browser !== null;

  useEffect(() => () => operation.current?.abort(), []);

  // The pushed state settles a pending request; nothing is inferred from delivery.
  useEffect(() => {
    if (pending === null || browser === null) return;
    if ((pending === 'pause' && browser.capture === 'paused')
      || (pending === 'resume' && browser.capture === 'active')) {
      setPending(null);
      setNotice(null);
    }
  }, [browser, pending]);

  useEffect(() => {
    if (pending === null) return;
    const timer = setTimeout(() => {
      setPending(null);
      setNotice('no_response');
    }, RESPONSE_DEADLINE_MS);
    return () => clearTimeout(timer);
  }, [pending]);

  const setCapture = async (action: CaptureAction) => {
    if (!canManage || browser === null || pending !== null || (action === 'resume' && browser.legal_review_required)) return;
    if (continuation && (!currentAuthority || !currentPresentation() || action !== 'resume'
      || browser.site_access !== 'granted' || browser.capture !== 'paused')) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setNotice(null);
    setPending(action);
    try {
      const delivery = await api.setCapture(action, controller.signal);
      if (controller.signal.aborted) return;
      if (delivery === 'unreachable') {
        setPending(null);
        setNotice('unreachable');
      }
    } catch (error) {
      if (controller.signal.aborted || (error instanceof BrowserControlApiError && error.code === 'cancelled')) return;
      setPending(null);
      setNotice('failed');
    }
  };

  const open = async (destination: BrowserDestination) => {
    if (continuation && (!canReview || !currentPresentation() || reviewInFlight.current)) return;
    if (continuation) { reviewInFlight.current = true; setOpeningReview(true); }
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller;
    setNotice(null);
    try {
      if (destination === 'setup' || destination === 'access') {
        await onboarding.begin('browser.access');
        return;
      }
      const result = await api.open(destination, controller.signal);
      if (!controller.signal.aborted && result === 'unreachable') setNotice('unreachable');
    } catch { if (!controller.signal.aborted) setNotice('failed'); }
    finally {
      if (continuation) {
        reviewInFlight.current = false;
        if (!controller.signal.aborted) setOpeningReview(false);
      }
    }
  };
  const openInExtension = (step: BrowserDestination, label: string) => (controlAvailable && canManage ? (
    <Button onClick={() => void open(step)} size="small" variant="outlined">{label}</Button>
  ) : undefined);

  const feedbackText = browser === null || notice === null ? null : notice === 'unreachable' ? 'The browser extension is not connected right now.'
            : notice === 'no_response' ? continuation ? "The browser extension didn't confirm the change." : "The browser extension didn't confirm the change. Try again."
              : 'The change was not confirmed.';
  useEffect(() => { onFeedback?.(feedbackText); }, [feedbackText, onFeedback]);
  useEffect(() => () => onFeedback?.(null), [onFeedback]);
  const feedback = onFeedback ? null : <StatusLine id="browser-feedback" tone="error" text={feedbackText} />;
  // Keep room for two description lines and the action through pushed changes.
  // The enclosing section owns one shared feedback region.
  const row = { minHeight: { xs: '6.5rem', sm: '4rem' }, alignItems: { xs: 'flex-start', sm: 'flex-start' }, '& > .MuiBox-root:last-of-type:not(:first-of-type)': { width: { sm: 200 }, '& button': { minHeight: 40 } } };
  const emptyAction = <Box aria-hidden sx={{ height: 40 }} />;
  if (continuation) {
    if (browser === null || !currentAuthority) return null;
    const reviewNeeded = browser.legal_review_required || browser.site_access !== 'granted' || browser.capture === 'off';
    const paused = browser.capture === 'paused' && !reviewNeeded;
    if (!reviewNeeded && !paused) return null;
    const title = browser.legal_review_required ? 'Allow message collection'
      : browser.site_access !== 'granted' ? 'Allow OnlyFans access'
        : browser.capture === 'off' ? 'Message collection is off' : 'Message collection is paused';
    const description = browser.legal_review_required ? 'Review how Dipsy uses your messages in Chrome.'
      : browser.site_access !== 'granted' ? 'Chrome needs your permission before Dipsy can collect messages.'
        : browser.capture === 'off' ? 'Review collection options in Chrome.' : 'New messages are not being collected.';
    const waiting = openingReview || desktop.browserLaunchPending || Boolean(reviewOperation);
    const progress = openingReview || desktop.browserLaunchPending ? 'Opening Chrome…'
      : reviewOperation?.state === 'outcome_unknown' ? 'The browser review has not been confirmed.'
        : reviewOperation ? 'Complete the review in Chrome.' : null;
    return <Panel component="section" emphasis="dominant" aria-labelledby="collection-review-title"
      data-journey-state="desktop.message_collection_review">
      <Typography component="h2" id="collection-review-title" variant="h5">{title}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{description}</Typography>
      {reviewNeeded && canReview && !waiting && <Button onClick={() => void open('setup')}
        variant="contained" sx={{ alignSelf: 'flex-start' }}>Review in Chrome</Button>}
      {paused && canManage && !waiting && <Button onClick={() => void setCapture('resume')}
        disabled={pending !== null} variant="contained" sx={{ alignSelf: 'flex-start' }}>
        {pending === 'resume' ? 'Resuming…' : 'Resume collecting'}</Button>}
      {(progress || feedbackText) && <StatusLine id="collection-review-feedback" essential
        text={feedbackText ?? progress} tone={feedbackText ? 'error' : 'secondary'} />}
    </Panel>;
  }
  if (browser === null) {
    return <Stack spacing={1.5} useFlexGap sx={{ minWidth: 0 }} data-browser-controls="waiting">
      <SettingRow sx={row} title="New messages" description={connection === 'connected'
        ? 'Waiting for extension settings.' : 'The browser extension is not connected.'} action={emptyAction} />
      <SettingRow sx={row} title="Site access" description="Site access hasn't been checked." action={emptyAction} />
      {feedback}
    </Stack>;
  }

  const paused = browser.capture === 'paused';
  const captureAction: CaptureAction | null = browser.capture === 'active' ? 'pause' : paused ? 'resume' : null;
  const resumeNeedsReview = paused && browser.legal_review_required;

  const captureButton = canManage && captureAction !== null && !resumeNeedsReview ? (
    <Button
      disabled={pending !== null}
      onClick={() => void setCapture(captureAction)}
      size="small"
      startIcon={captureAction === 'pause' ? <PauseCircleOutlinedIcon /> : <PlayCircleOutlinedIcon />}
      variant="outlined"
    >
      {pending === 'pause' ? 'Pausing…' : pending === 'resume' ? 'Resuming…'
        : captureAction === 'pause' ? 'Pause collecting' : 'Resume collecting'}
    </Button>
  ) : resumeNeedsReview ? openInExtension('setup', 'Review terms') : undefined;

  return (
    <Stack data-browser-controls="available" data-journey-state="desktop.browser_controls" spacing={1.5} useFlexGap sx={{ minWidth: 0 }}>
      <SettingRow
        sx={row}
        title="New messages"
        description={browser.capture === 'active' ? 'New message collection is enabled.'
          : resumeNeedsReview ? 'Paused. Terms need review.'
            : paused ? 'Paused. Nothing new is collected.' : 'Off in the browser.'}
        action={captureButton ?? emptyAction}
      />
      <SettingRow
        sx={row}
        title="Site access"
        description={browser.site_access === 'granted' ? 'Allowed for OnlyFans.'
          : browser.site_access === 'reload_required' ? 'Site access needs attention.'
            : 'Needs your approval.'}
        action={(browser.site_access === 'granted' ? undefined : openInExtension('access', browser.site_access === 'reload_required' ? 'Open extension' : 'Allow in extension')) ?? emptyAction}
      />
      {feedback}
    </Stack>
  );
}
