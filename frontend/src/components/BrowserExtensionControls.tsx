import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import { Alert, Button, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { SettingRow } from './ui';

import type { BrowserSurfacePayload } from '../protocol';
import {
  BrowserControlApiError,
  type BrowserControlApi,
  type CaptureAction,
} from '../services/browserControlApi';
import type { ExtensionPort, ExtensionStep } from '../services/extensionPort';
import type { ExtensionConnection } from '../utils/statusCopy';

// How long to wait for the extension's pushed state before reporting no response.
const RESPONSE_DEADLINE_MS = 10_000;

type Notice = 'unreachable' | 'failed' | 'no_response' | null;

/**
 * Bridge owns day-to-day extension controls while an extension session is open
 * (ADR 0045). State shown here is only what the extension pushed; a click never
 * changes it locally. Permission prompts open the extension's own page, because
 * the browser requires the click there.
 */
export function BrowserExtensionControls({ api, browser, canManage, connection, port }: {
  api: BrowserControlApi;
  browser: BrowserSurfacePayload | null;
  canManage: boolean;
  /** The link state; its issue text already explains an unreachable extension. */
  connection: ExtensionConnection;
  port: ExtensionPort;
}) {
  const extension = useSyncExternalStore(port.subscribe, port.getState, port.getState);
  const [pending, setPending] = useState<CaptureAction | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const operation = useRef<AbortController | null>(null);
  const sameBrowser = extension.status === 'connected';

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

  const openInExtension = (step: ExtensionStep, label: string) => (sameBrowser && canManage ? (
    <Button onClick={() => port.open(step)} size="small" variant="outlined">{label}</Button>
  ) : undefined);

  if (browser === null) {
    // A connection issue is already explained above; don't repeat it.
    if (connection !== 'connected') return null;
    return (
      <Typography data-browser-controls="waiting" variant="body2" sx={{ color: 'text.secondary' }}>
        Waiting for the browser extension to report its settings.
      </Typography>
    );
  }

  const paused = browser.capture === 'paused';
  const captureAction: CaptureAction | null = browser.capture === 'active' ? 'pause' : paused ? 'resume' : null;
  const resumeNeedsReview = paused && browser.legal_review_required;
  const needsBrowserAction = browser.site_access !== 'granted' || browser.history_permission !== 'granted'
    || resumeNeedsReview;

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
    <Stack data-browser-controls="available" data-journey-state="desktop.browser_controls" spacing={1.5}>
      <SettingRow
        title="New messages"
        description={browser.capture === 'active' ? 'Collecting in the browser.'
          : resumeNeedsReview ? 'Paused. Review the updated terms in the extension to resume.'
            : paused ? 'Paused. Nothing new is collected.' : 'Off in the browser.'}
        action={captureButton}
      />
      <SettingRow
        title="Site access"
        description={browser.site_access === 'granted' ? 'Allowed for OnlyFans.'
          : browser.site_access === 'reload_required' ? 'Reload your OnlyFans tabs to apply it.'
            : 'Needs your approval.'}
        action={browser.site_access === 'granted' ? undefined : openInExtension('access', 'Allow in extension')}
      />
      <SettingRow
        title="Message history access"
        description={browser.history_permission === 'granted' ? 'Allowed.' : 'Not allowed yet.'}
        action={browser.history_permission === 'granted' ? undefined : openInExtension('history', 'Allow in extension')}
      />
      {needsBrowserAction && !sameBrowser && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Change these in the browser where the extension is installed.
        </Typography>
      )}
      {notice !== null && (
        <Alert severity="warning" role="alert">
          {notice === 'unreachable' ? 'The browser extension is not connected right now. Open your browser and try again.'
            : notice === 'no_response' ? "The browser extension didn't confirm the change. Check it in your browser."
              : "The change couldn't be sent. Try again."}
        </Alert>
      )}
    </Stack>
  );
}
