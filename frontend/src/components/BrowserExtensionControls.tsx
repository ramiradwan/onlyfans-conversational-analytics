import { Alert, Button, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';

import type { BrowserSurfacePayload } from '../protocol';
import {
  BrowserControlApiError,
  type BrowserControlApi,
  type CaptureAction,
} from '../services/browserControlApi';
import type { ExtensionPort, ExtensionStep } from '../services/extensionPort';

// How long to wait for the extension's pushed state before reporting no response.
const RESPONSE_DEADLINE_MS = 10_000;

type Notice = 'unreachable' | 'failed' | 'no_response' | null;

function Row({ action, detail, title }: { action?: ReactNode; detail: string; title: string }) {
  return (
    <Stack direction="row" spacing={2} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
      <Stack spacing={0.25}>
        <Typography variant="body2">{title}</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{detail}</Typography>
      </Stack>
      {action}
    </Stack>
  );
}

/**
 * Bridge owns day-to-day extension controls while an extension session is open
 * (ADR 0027). State shown here is only what the extension pushed; a click never
 * changes it locally. Permission prompts open the extension's own page, because
 * the browser requires the click there.
 */
export function BrowserExtensionControls({ api, browser, canManage, port }: {
  api: BrowserControlApi;
  browser: BrowserSurfacePayload | null;
  canManage: boolean;
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
    return (
      <Typography data-browser-controls="unavailable" variant="body2" sx={{ color: 'text.secondary' }}>
        Open the browser where the extension is installed to manage it from here.
      </Typography>
    );
  }

  const paused = browser.capture === 'paused';
  const captureAction: CaptureAction | null = browser.capture === 'active' ? 'pause' : paused ? 'resume' : null;
  const resumeNeedsReview = paused && browser.legal_review_required;
  const inBrowser = sameBrowser ? '' : ' Open the extension in its browser to change this.';

  return (
    <Stack data-browser-controls="available" data-journey-state="desktop.browser_controls" spacing={1.5}>
      <Row
        title="New messages"
        detail={browser.capture === 'active' ? 'Collecting in the browser.'
          : paused ? 'Paused. Nothing new is collected.' : 'Off in the browser.'}
        action={canManage && captureAction !== null && !resumeNeedsReview ? (
          <Button
            disabled={pending !== null}
            onClick={() => void setCapture(captureAction)}
            size="small"
            variant="outlined"
          >
            {pending === 'pause' ? 'Pausing…' : pending === 'resume' ? 'Resuming…'
              : captureAction === 'pause' ? 'Pause' : 'Resume'}
          </Button>
        ) : resumeNeedsReview ? openInExtension('setup', 'Review') : undefined}
      />
      {resumeNeedsReview && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Review the updated terms in the extension before resuming.{inBrowser}
        </Typography>
      )}
      <Row
        title="Site access"
        detail={browser.site_access === 'granted' ? 'Allowed for OnlyFans.'
          : browser.site_access === 'reload_required' ? 'Reload your OnlyFans tabs to apply it.'
            : `Needs your approval in the browser.${inBrowser}`}
        action={browser.site_access === 'granted' ? undefined : openInExtension('access', 'Allow in extension')}
      />
      <Row
        title="Message history access"
        detail={browser.history_permission === 'granted' ? 'Allowed.' : `Not allowed yet.${inBrowser}`}
        action={browser.history_permission === 'granted' ? undefined : openInExtension('history', 'Allow in extension')}
      />
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
