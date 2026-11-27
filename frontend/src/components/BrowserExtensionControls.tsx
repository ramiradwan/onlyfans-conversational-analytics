import { Box, Button, Stack, Switch } from '@mui/material';
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { ReservedText, SettingRow } from './ui';

import { captureRecovery, recoveryClass } from '../../shared-setup/recovery.js';
import type { BrowserSurfacePayload } from '../protocol';
import {
  type BrowserControlApi,
  type CaptureAction,
  type BrowserDestination,
} from '../services/browserControlApi';
import { captureOperation } from '../services/captureOperation';
import { desktopInteractionFailure, desktopOnboardingApi, selectBrowserFeedback, selectCollectionContinuation, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import type { ExtensionConnection } from '../utils/statusCopy';
import { OnboardingAction, OnboardingCard, OnboardingSpot } from './ui/OnboardingCard';
import { StatusLine } from './ui/ReservedRegion';

type Notice = 'unreachable' | 'failed' | null;

// Each row reserves room for every description it can show, so these lists are the only copy source.
const captureCopy = {
  active: 'New messages are saved as they arrive.',
  pausing: 'Pausing…',
  resuming: 'Resuming…',
  review: 'Paused until you review the terms.',
  paused: 'Paused.',
  off: 'Turned off in the extension.',
  waiting: 'Waiting for extension settings.',
  disconnected: 'The browser extension is not connected.',
} as const;
const accessCopy = {
  granted: 'Allowed for OnlyFans.',
  reload: 'Site access needs attention.',
  approval: 'Needs your approval.',
  unchecked: "Site access hasn't been checked.",
} as const;
const captureDescriptions = Object.values(captureCopy);
const accessDescriptions = Object.values(accessCopy);
// End-aligned buttons keep the width of their longest label, so a pushed change never moves them.
const accessLabel = { reload: 'Open extension', approval: 'Allow in extension' } as const;
const accessLabels = Object.values(accessLabel);
const reviewLabel = 'Review terms';
const captureLabels = [reviewLabel];

/**
 * Bridge owns day-to-day extension controls while an extension session is open
 * (ADR 0045). State shown here is only what the extension pushed; a click never
 * changes it locally. Permission prompts open the extension's own page, because
 * the browser requires the click there.
 */
export function BrowserExtensionControls({ api, browser, canManage, connection, onboarding = desktopOnboardingApi, onFeedback, continuationCreatorId, creatorAccountId }: {
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
  creatorAccountId?: string;
}) {
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const continuation = continuationCreatorId !== undefined;
  const snapshot = desktop.snapshot;
  const { current: currentAuthority, reviewNeeded, paused: continuationPaused, reviewOperation, localReviewPending, canReview, canResume } =
    selectCollectionContinuation(desktop, continuationCreatorId ?? creatorAccountId, browser, canManage);
  const reviewFeedback = selectBrowserFeedback(desktop, reviewOperation?.operation_id);
  const currentPresentation = () => {
    const current = onboarding.getState();
    return current.connected && current.snapshot === snapshot && current.pending === desktop.pending
      && current.browserLaunchPending === desktop.browserLaunchPending;
  };
  const reviewInFlight = useRef(false);
  const [openingReview, setOpeningReview] = useState(false);
  const capture = useMemo(() => captureOperation(api, continuationCreatorId ?? creatorAccountId),
    [api, continuationCreatorId, creatorAccountId]);
  const pending = useSyncExternalStore(capture.subscribe, capture.getState, capture.getState);
  const captureBlocked = pending !== null || Boolean(browser?.capture_control_supported && browser.capture_control?.capture !== browser.capture);
  const [notice, setNotice] = useState<Notice>(null);
  const operation = useRef<AbortController | null>(null);
  const controlAvailable = browser !== null;

  useEffect(() => () => operation.current?.abort(), []);

  useEffect(() => { capture.observe(browser); }, [capture, browser]);

  const setCapture = async (action: CaptureAction) => {
    if (!canManage || browser === null || capture.getState() !== null
      || (action === 'resume' && browser.legal_review_required)) return;
    if (continuation && (!canResume || !currentPresentation() || action !== 'resume')) return;
    setNotice(null);
    await capture.submit(action, browser);
  };

  const open = async (destination: BrowserDestination) => {
    if (continuation && (!canReview || !currentPresentation() || reviewInFlight.current)) return;
    if (continuation) { reviewInFlight.current = true; setOpeningReview(true); }
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller;
    setNotice(null);
    try {
      if (destination === 'setup' || destination === 'access') {
        const result = await onboarding.begin('browser.access');
        if (!controller.signal.aborted && desktopInteractionFailure(result)) setNotice('failed');
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
  // Each control has its own key, so a different control replaces the previous one instead of reshaping it.
  const openInExtension = (step: BrowserDestination, label: string, reserve?: readonly string[]) => (controlAvailable && canManage ? (
    <Button key={step} onClick={() => void open(step)} size="small" variant="outlined">
      {reserve ? <ReservedText reserve={reserve} text={label} /> : label}
    </Button>
  ) : undefined);

  const captureFailure = recoveryClass(captureRecovery, pending?.status);
  const feedbackText = browser === null ? null
    : captureFailure === 'outcome_unknown' ? "Your change hasn't been confirmed."
      : captureFailure === 'authorization_refused' ? "Dipsy couldn't change collection of OnlyFans activity."
        : captureFailure === 'browser_disconnected' || notice === 'unreachable' ? 'The browser extension is not connected right now.'
          : reviewFeedback?.message ?? (notice === 'failed' ? 'The change was not confirmed.' : null);
  useEffect(() => { onFeedback?.(feedbackText); }, [feedbackText, onFeedback]);
  useEffect(() => () => onFeedback?.(null), [onFeedback]);
  const feedback = onFeedback ? null : <StatusLine id="browser-feedback" tone="error" text={feedbackText} />;
  // Extension-pushed changes swap text and controls in place. Each row keeps the height of its
  // tallest description and the width of its widest control. The enclosing section owns one shared feedback region.
  // An empty control cell holds the row's shape until a real control arrives.
  const emptyAction = <Box key="empty" aria-hidden component="span" />;
  if (continuation) {
    if (browser === null || !currentAuthority) return null;
    if (!reviewNeeded && !continuationPaused) return null;
    const title = browser.legal_review_required ? 'Allow OnlyFans activity collection'
      : browser.site_access !== 'granted' ? 'Allow site access'
        : browser.capture === 'off' ? 'OnlyFans activity collection is off' : 'OnlyFans activity collection is paused';
    const description = browser.legal_review_required ? 'Review how Dipsy uses your OnlyFans activity in your browser.'
      : browser.site_access !== 'granted' ? 'Your browser needs permission before Dipsy can collect OnlyFans activity.'
        : browser.capture === 'off' ? 'Review how to collect OnlyFans activity in your browser.' : 'Collection of new OnlyFans activity is paused.';
    const waiting = openingReview || desktop.browserLaunchPending || Boolean(reviewOperation) || localReviewPending;
    const progress = openingReview || desktop.browserLaunchPending ? 'Opening browser…'
      : reviewOperation?.state === 'outcome_unknown' || localReviewPending && desktop.error === 'operation_unconfirmed'
        ? 'The browser review has not been confirmed.'
        : reviewOperation ? 'Complete the review in your browser.'
          : localReviewPending ? 'Preparing browser review.' : null;
    return <Box sx={{ maxWidth: 760 }} data-journey-state="desktop.message_collection_review">
      <OnboardingCard title={title} body={description} spot={<OnboardingSpot icon="extension" />}
        actions={<OnboardingAction>
          {reviewNeeded && canReview && !waiting && <Button onClick={() => void open('setup')} variant="contained">Review in browser</Button>}
          {continuationPaused && canResume && !waiting && <Button onClick={() => void setCapture('resume')}
            disabled={captureBlocked} variant="contained">
            {pending?.status === 'waiting' && pending.action === 'resume' ? 'Resuming…' : 'Resume collecting'}</Button>}
        </OnboardingAction>}
        status={(progress || feedbackText) && <Box sx={{ mt: 2 }}>
          <StatusLine id="collection-review-feedback" essential text={feedbackText ?? progress} tone={feedbackText ? 'error' : 'secondary'} />
        </Box>} />
    </Box>;
  }
  if (browser === null) {
    return <Stack spacing={0} useFlexGap sx={{ minWidth: 0 }} data-browser-controls="waiting">
      <SettingRow title="Collect new messages" reserveActions={captureLabels} reserveDescriptions={captureDescriptions}
        description={connection === 'connected' ? captureCopy.waiting : captureCopy.disconnected} action={emptyAction} />
      <SettingRow title="Site access" reserveActions={accessLabels} reserveDescriptions={accessDescriptions}
        description={accessCopy.unchecked} action={emptyAction} />
      {feedback}
    </Stack>;
  }

  const collecting = browser.capture === 'active';
  const paused = browser.capture === 'paused';
  const captureAction: CaptureAction | null = collecting ? 'pause' : paused ? 'resume' : null;
  const resumeNeedsReview = paused && browser.legal_review_required;
  const waitingFor = pending?.status === 'waiting' ? pending.action : null;

  // The switch shows only what the extension confirmed. A change waits for it, with the switch held.
  const captureControl = resumeNeedsReview ? openInExtension('setup', reviewLabel) : (
    <Switch
      key="capture"
      checked={collecting}
      disabled={!canManage || captureAction === null || captureBlocked}
      onChange={() => { if (captureAction !== null) void setCapture(captureAction); }}
      slotProps={{ input: { 'aria-label': 'Collect new messages' } }}
    />
  );

  return (
    <Stack data-browser-controls="available" data-journey-state="desktop.browser_controls" spacing={0} useFlexGap sx={{ minWidth: 0 }}>
      <SettingRow
        title="Collect new messages"
        reserveActions={captureLabels}
        reserveDescriptions={captureDescriptions}
        description={waitingFor === 'pause' ? captureCopy.pausing
          : waitingFor === 'resume' ? captureCopy.resuming
            : collecting ? captureCopy.active
              : resumeNeedsReview ? captureCopy.review
                : paused ? captureCopy.paused : captureCopy.off}
        action={captureControl ?? emptyAction}
      />
      <SettingRow
        title="Site access"
        reserveActions={accessLabels}
        reserveDescriptions={accessDescriptions}
        description={browser.site_access === 'granted' ? accessCopy.granted
          : browser.site_access === 'reload_required' ? accessCopy.reload
            : accessCopy.approval}
        action={(browser.site_access === 'granted' ? undefined : openInExtension('access',
          browser.site_access === 'reload_required' ? accessLabel.reload : accessLabel.approval, accessLabels)) ?? emptyAction}
      />
      {feedback}
    </Stack>
  );
}
