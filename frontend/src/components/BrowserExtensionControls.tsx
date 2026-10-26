import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import { Box, Button, Stack } from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { SettingRow } from './ui';

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
export function BrowserExtensionControls({ api, browser, canManage, connection, onboarding = desktopOnboardingApi, onFeedback }: {
  api: BrowserControlApi;
  browser: BrowserSurfacePayload | null;
  canManage: boolean;
  /** Ingestion link state, used when no browser surface is available. */
  connection: ExtensionConnection;
  onboarding?: DesktopOnboardingApi;
  /** Settings combines its control, pairing and connection-list feedback. */
  onFeedback?: (text: string | null) => void;
}) {
  useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
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
    if (!canManage || browser === null || (action === 'resume' && browser.legal_review_required)) return;
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
  };
  const openInExtension = (step: BrowserDestination, label: string) => (controlAvailable && canManage ? (
    <Button onClick={() => void open(step)} size="small" variant="outlined">{label}</Button>
  ) : undefined);

  const feedbackText = browser === null || notice === null ? null : notice === 'unreachable' ? 'The browser extension is not connected right now.'
            : notice === 'no_response' ? "The browser extension didn't confirm the change. Try again."
              : 'The change was not confirmed.';
  useEffect(() => { onFeedback?.(feedbackText); }, [feedbackText, onFeedback]);
  useEffect(() => () => onFeedback?.(null), [onFeedback]);
  const feedback = onFeedback ? null : <StatusLine id="browser-feedback" tone="error" text={feedbackText} />;
  // Keep room for two description lines and the action through pushed changes.
  // The enclosing section owns one shared feedback region.
  const row = { minHeight: { xs: '6.5rem', sm: '4rem' }, alignItems: { xs: 'flex-start', sm: 'flex-start' }, '& > .MuiBox-root:last-of-type:not(:first-of-type)': { width: { sm: 200 }, '& button': { minHeight: 40 } } };
  const emptyAction = <Box aria-hidden sx={{ height: 40 }} />;
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
