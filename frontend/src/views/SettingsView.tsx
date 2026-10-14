import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControlLabel,
  LinearProgress,
  Skeleton,
  Stack,
  Typography,
} from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader, useRevealHold } from '../components/ui';
import { StatusLine } from '../components/ui/ReservedRegion';
import { usePermissions } from '../hooks/usePermissions';
import type { HistorySettings } from '../protocol';
import { browserControlApi, type BrowserControlApi } from '../services/browserControlApi';
import { desktopOnboardingApi, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import { focusDesktopWindow } from '../services/desktopWindow';
import {
  historySettingsApi as defaultHistorySettingsApi,
  HistorySettingsApiError,
  type HistorySettingsApi,
} from '../services/historySettingsApi';
import { bridgeTransportStore } from '../store/transportStore';
import { presentHistory } from '../utils/historyPresentation';
import { extensionConnection } from '../utils/statusCopy';

const NUMBER_FORMAT = new Intl.NumberFormat('en-US');

interface SettingsViewProps {
  api?: HistorySettingsApi;
  browserApi?: BrowserControlApi;
  desktopApi?: DesktopOnboardingApi;
}

interface PermissionReturn {
  creatorId: string;
  journeyId: string;
  generation: string;
  expires: number;
  delivered: boolean;
}

function changeFailure(cause: unknown, fallback: string): string {
  if (cause instanceof HistorySettingsApiError && (cause.status === 409 || cause.status === 412)) {
    return 'Message history settings changed. Review the current settings and try again.';
  }
  return fallback;
}

/** Message history section of Settings: consent, progress, and pause or turn-off controls. */
export default function SettingsView({ api = defaultHistorySettingsApi, browserApi = browserControlApi,
  desktopApi = desktopOnboardingApi }: SettingsViewProps) {
  const { canManageHistorySync } = usePermissions();
  const transport = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const [settings, setSettings] = useState<HistorySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [consentAccepted, setConsentAccepted] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [loadRevision, setLoadRevision] = useState(0);
  const desktop = useSyncExternalStore(desktopApi.subscribe, desktopApi.getState, desktopApi.getState);
  const [permissionReturn, setPermissionReturn] = useState<PermissionReturn | null>(null);
  const consumedPermissionReturn = useRef<PermissionReturn | null>(null);
  const permissionRequest = useRef<AbortController | null>(null);
  const [openingPermission, setOpeningPermission] = useState(false);
  useRevealHold(loading);

  useEffect(() => {
    setOpeningPermission(false);
    return () => {
      permissionRequest.current?.abort();
      permissionRequest.current = null;
    };
  }, [transport.creatorAccountId, desktop.connected, desktop.snapshot?.journey_id, desktop.snapshot?.generation]);

  useEffect(() => {
    if (!permissionReturn) return;
    if (performance.now() >= permissionReturn.expires || !desktop.connected
      || desktop.snapshot?.journey_id !== permissionReturn.journeyId
      || desktop.snapshot.generation !== permissionReturn.generation
      || transport.creatorAccountId !== permissionReturn.creatorId) {
      setPermissionReturn(null);
      return;
    }
    if (permissionReturn.delivered && transport.connection === 'connected'
      && transport.agent?.creator_account_id === permissionReturn.creatorId
      && transport.agent.status === 'connected'
      && transport.agent.browser?.history_permission === 'granted') {
      setPermissionReturn(null);
      if (consumedPermissionReturn.current !== permissionReturn) {
        consumedPermissionReturn.current = permissionReturn;
        void focusDesktopWindow().catch(() => { /* Window focus does not change permission state. */ });
      }
      return;
    }
    const timer = setTimeout(() => setPermissionReturn(null), permissionReturn.expires - performance.now());
    return () => clearTimeout(timer);
  }, [permissionReturn, desktop.connected, desktop.snapshot?.journey_id, desktop.snapshot?.generation,
    transport.creatorAccountId, transport.connection, transport.agent]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void api.get(controller.signal).then(
      (next) => {
        if (controller.signal.aborted) return;
        setSettings(next);
        setError(null);
        setLoading(false);
      },
      () => {
        if (controller.signal.aborted) return;
        setError("Message history settings couldn't be loaded.");
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [api, loadRevision, transport.creatorAccountId]);

  useEffect(() => {
    if (busy || settings === null) return;
    const ownerChanged = Math.max(transport.agent?.required_history_settings_revision ?? 0,
      transport.agent?.applied_history_settings_revision ?? 0) > settings.settings_revision;
    if (!ownerChanged && (settings.desired_state === 'not_started'
      || settings.desired_state === settings.effective_state)) return;
    const controller = new AbortController();
    let delay = ownerChanged ? 0 : 1000;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await api.get(controller.signal);
        if (controller.signal.aborted) return;
        setSettings((current) => current !== null
          && next.settings_revision >= current.settings_revision ? next : current);
      } catch {
        // A disconnected extension or temporary local API failure must not
        // leave a successful acknowledgement hidden until manual navigation.
      } finally {
        if (!controller.signal.aborted && ++attempts < 3) {
          delay = Math.min(Math.max(delay * 2, 1000), 15_000);
          timer = setTimeout(() => void refresh(), delay);
        }
      }
    };
    timer = setTimeout(() => void refresh(), delay);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, busy, settings?.settings_revision, settings?.desired_state, settings?.effective_state,
    transport.agent?.applied_history_settings_revision, transport.agent?.required_history_settings_revision]);

  const refreshAfterConflict = async (cause: unknown) => {
    if (!(cause instanceof HistorySettingsApiError) || ![409, 412].includes(cause.status ?? 0)) return;
    try {
      const next = await api.get();
      setSettings(current => current && current.creator_account_id === next.creator_account_id
        && next.settings_revision >= current.settings_revision ? next : current);
      setConsentAccepted(false);
    } catch { /* Preserve the failed command and its current settings for an explicit retry. */ }
  };

  const allowBrowserHistory = async () => {
    if (permissionRequest.current) return;
    const controller = new AbortController();
    permissionRequest.current = controller;
    setOpeningPermission(true);
    setError(null);
    const snapshot = desktop.snapshot;
    const intent: PermissionReturn | null = desktop.connected && snapshot?.journey_id
      && transport.creatorAccountId && transport.agent?.creator_account_id === transport.creatorAccountId
      && transport.agent.browser?.history_permission === 'missing'
      ? { creatorId: transport.creatorAccountId, journeyId: snapshot.journey_id,
        generation: snapshot.generation, expires: performance.now() + 90_000, delivered: false } : null;
    setPermissionReturn(intent);
    try {
      const result = await browserApi.open('history', controller.signal);
      if (controller.signal.aborted) return;
      setPermissionReturn(current => current && current === intent && result === 'delivered'
        ? { ...current, delivered: true } : current === intent ? null : current);
      if (result === 'unreachable') setError('The browser extension is not connected.');
    } catch {
      if (controller.signal.aborted) return;
      setPermissionReturn(current => current === intent ? null : current);
      setError('Chrome permission could not be opened. Try again.');
    } finally {
      if (permissionRequest.current === controller) {
        permissionRequest.current = null;
        setOpeningPermission(false);
      }
    }
  };

  const update = async (desiredState: 'running' | 'paused', acceptConsent = false) => {
    if (settings === null) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.update(settings.settings_revision, {
        desired_state: desiredState,
        consent_policy_version: acceptConsent ? settings.consent_policy_version : null,
        accept_consent: acceptConsent,
        recent_window_days: settings.recent_window_days,
        page_size: settings.page_size,
        pages_per_wake: settings.pages_per_wake,
        request_interval_ms: settings.request_interval_ms,
        retry_limit: settings.retry_limit,
      });
      setSettings(next);
      setConsentAccepted(false);
      if (desiredState === 'running' && transport.agent?.browser?.history_permission === 'missing') await allowBrowserHistory();
    } catch (cause) {
      await refreshAfterConflict(cause);
      setError(changeFailure(cause, "Your change couldn't be saved. Try again."));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (settings === null) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.revoke(settings.settings_revision);
      setSettings(next);
      bridgeTransportStore.clearMessageCache();
      setConfirmRevoke(false);
    } catch (cause) {
      await refreshAfterConflict(cause);
      setConfirmRevoke(false);
      setError(changeFailure(cause, "Message history couldn't be turned off. Try again."));
    } finally {
      setBusy(false);
    }
  };

  const hasConsent = settings !== null
    && settings.consent_revision !== null
    && settings.desired_state !== 'revoked';
  const paused = hasConsent && settings.desired_state === 'paused';
  const progress = transport.snapshotProgress.percentage;
  const { completeConversations, discoveredConversations } = transport.snapshotProgress;
  const agent = transport.connection === 'connected' ? transport.agent : null;
  const waitingForExtension = settings !== null && !hasConsent && extensionConnection(agent) === 'offline';
  const presentation = settings ? presentHistory(settings, agent, transport.coverage) : null;
  const permissionNeeded = hasConsent && !paused && agent?.browser?.history_permission === 'missing';

  return (
    <Panel sx={{ gap: 1.5 }}>
      <SectionHeader
        sx={{ minHeight: '1.5rem', display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'minmax(0, 1fr) 10rem' },
          gap: { xs: 1, sm: 2 }, '& > [aria-live]': { width: '100%', maxWidth: '10rem', minHeight: { xs: '2.75rem', sm: '1.5rem' }, margin: '0 !important',
            '& .MuiChip-root': { width: '100%', height: { xs: '2.75rem', sm: '1.5rem' }, alignItems: { xs: 'flex-start', sm: 'center' } },
            '& .MuiChip-icon': { mt: { xs: '0.5rem', sm: 0 } },
            '& .MuiChip-label': { minWidth: 0, width: '100%', textAlign: 'left', whiteSpace: 'normal', lineHeight: 1.35, pt: { xs: '0.125rem', sm: 0 } } } }}
        status={presentation}
        summary={
          settings !== null && !hasConsent
            ? 'Add your older conversations so your numbers cover your whole history.'
            : undefined
        }
        title="Message history"
      />

      <StatusLine id="history-feedback" tone={error ? 'error' : 'secondary'} text={error ?? presentation?.detail ?? null} />

      {loading ? (
        <Stack spacing={1} role="status" aria-label="Loading message history settings">
          <Skeleton width="60%" />
          <Skeleton width="80%" />
          <Skeleton height={40} width={200} variant="rounded" />
        </Stack>
      ) : settings !== null ? (
        <Stack data-journey-state="desktop.message_history" spacing={1.5}>
          {waitingForExtension ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Available once the browser extension is connected.
            </Typography>
          ) : !hasConsent ? (
            <Box component="ul" sx={{ color: 'text.secondary', m: 0, pl: 2.5, typography: 'body2' }}>
              <li>Read-only: it never sends messages or changes your account.</li>
              <li>Synced message history stays on this computer.</li>
              <li>Pause or turn it off at any time.</li>
            </Box>
          ) : (
            <Box sx={{ minHeight: { xs: '2.75rem', sm: '1.75rem' } }}>
              {progress !== null && transport.coverage.status !== 'complete' && (
                <>
                  <LinearProgress aria-label="Message history progress" value={progress} variant="determinate" />
                  <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                    {discoveredConversations === null
                      ? `${NUMBER_FORMAT.format(completeConversations)} conversations synced so far`
                      : `${NUMBER_FORMAT.format(completeConversations)} of ${NUMBER_FORMAT.format(discoveredConversations)} conversations`}
                  </Typography>
                </>
              )}
            </Box>
          )}

          {!canManageHistorySync && (
            <Alert severity="info">Only the account owner can change message history.</Alert>
          )}

          {!hasConsent && !waitingForExtension && canManageHistorySync && <Box>
            <FormControlLabel
              control={(
                <Checkbox
                  checked={consentAccepted}
                  onChange={(event) => setConsentAccepted(event.target.checked)}
                />
              )}
              label="I allow read-only syncing of my older messages to this computer."
            />
          </Box>}

          {canManageHistorySync && !waitingForExtension && (
            <Stack
              direction={{ xs: 'column', sm: 'row' }}
              spacing={1.5}
              sx={{ alignItems: { xs: 'flex-start', sm: 'center' }, '& > button': { height: '2.5rem', width: 'min(16rem, 100%)', justifyContent: 'flex-start' } }}
            >
              {!hasConsent ? (
                <Button
                  disabled={!consentAccepted || busy}
                  onClick={() => void update('running', true)}
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="contained"
                >
                  Turn on message history
                </Button>
              ) : permissionNeeded ? (
                <Button disabled={busy || openingPermission} onClick={() => void allowBrowserHistory()} variant="contained">Allow Chrome access</Button>
              ) : paused ? (
                <Button
                  disabled={busy}
                  onClick={() => void update('running')}
                  size="small"
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="outlined"
                >
                  Resume
                </Button>
              ) : (
                <Button
                  disabled={busy}
                  onClick={() => void update('paused')}
                  size="small"
                  startIcon={<PauseCircleOutlinedIcon />}
                  variant="outlined"
                >
                  Pause
                </Button>
              )}
              {hasConsent && (
                <Button
                  disabled={busy}
                  onClick={() => setConfirmRevoke(true)}
                  size="small"
                  sx={{ color: 'text.secondary' }}
                >
                  Turn off
                </Button>
              )}
            </Stack>
          )}
        </Stack>
      ) : error ? <Button onClick={() => setLoadRevision(value => value + 1)}>Try again</Button> : null}

      <Dialog open={confirmRevoke} onClose={() => !busy && setConfirmRevoke(false)}>
        <DialogTitle>Turn off message history?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Older messages stop syncing. Messages already synced stay on this computer.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => setConfirmRevoke(false)}>Cancel</Button>
          <Button color="error" disabled={busy} onClick={() => void revoke()} variant="contained">
            Turn off
          </Button>
        </DialogActions>
      </Dialog>
    </Panel>
  );
}
