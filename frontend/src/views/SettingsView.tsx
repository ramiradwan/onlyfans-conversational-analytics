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
import { useEffect, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader, type SectionStatus } from '../components/ui';
import { usePermissions } from '../hooks/usePermissions';
import type { HistorySettings } from '../protocol';
import {
  historySettingsApi as defaultHistorySettingsApi,
  HistorySettingsApiError,
  type HistorySettingsApi,
} from '../services/historySettingsApi';
import { bridgeTransportStore } from '../store/transportStore';
import { coverageProgressLabel } from '../utils/dataReadiness';
import { extensionConnection } from '../utils/statusCopy';

const NUMBER_FORMAT = new Intl.NumberFormat('en-US');

interface SettingsViewProps {
  api?: HistorySettingsApi;
}

function changeFailure(cause: unknown, fallback: string): string {
  if (cause instanceof HistorySettingsApiError && (cause.status === 409 || cause.status === 412)) {
    return 'These settings changed in another window. Reload the page and try again.';
  }
  return fallback;
}

function waitingText(settings: HistorySettings): string {
  if (settings.desired_state === 'running') {
    return 'Waiting for the browser extension to start. If nothing changes, open the extension and choose Allow message history.';
  }
  if (settings.desired_state === 'paused') return 'Pausing when the browser extension next connects.';
  return 'Waiting for the browser extension to apply this change.';
}

/** Message history section of Settings: consent, progress, and pause or turn-off controls. */
export default function SettingsView({ api = defaultHistorySettingsApi }: SettingsViewProps) {
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

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void api.get(controller.signal).then(
      (next) => {
        setSettings(next);
        setError(null);
        setLoading(false);
      },
      () => {
        if (controller.signal.aborted) return;
        setError("Message history settings couldn't be loaded. Reload the page to try again.");
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [api]);

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
    } catch (cause) {
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
  const historyComplete = transport.coverage.status === 'complete';
  // Consent is asked for only once the extension that performs the sync is connected.
  const waitingForExtension =
    settings !== null && !hasConsent && extensionConnection(transport.agent) === 'offline';
  const status: SectionStatus | null = settings === null
    ? null
    : !hasConsent
      ? { label: 'Off', tone: 'default' }
      : paused
        ? { label: 'Paused', tone: 'warning' }
        : { label: 'On', tone: 'success' };

  return (
    <Panel>
      <SectionHeader
        status={status}
        summary={
          settings !== null && !hasConsent
            ? 'Add your older conversations so your numbers cover your whole history.'
            : undefined
        }
        title="Message history"
      />

      {error && <Alert severity="error" role="alert">{error}</Alert>}

      {loading ? (
        <Stack spacing={1} role="status" aria-label="Loading message history settings">
          <Skeleton width="60%" />
          <Skeleton width="80%" />
          <Skeleton height={40} width={200} variant="rounded" />
        </Stack>
      ) : settings !== null ? (
        <Stack spacing={2}>
          {waitingForExtension ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Available once the browser extension is connected.
            </Typography>
          ) : !hasConsent ? (
            <Box component="ul" sx={{ color: 'text.secondary', m: 0, pl: 2.5, typography: 'body2' }}>
              <li>Read-only: it never sends messages or changes your account.</li>
              <li>Everything stays on this computer.</li>
              <li>Pause or turn it off at any time.</li>
            </Box>
          ) : historyComplete ? (
            <Typography variant="body2">
              {discoveredConversations === null
                ? 'History synced.'
                : `All ${NUMBER_FORMAT.format(discoveredConversations)} conversations synced.`}
            </Typography>
          ) : (
            <Stack spacing={0.75}>
              <Typography variant="body2">{coverageProgressLabel(transport.coverage)}</Typography>
              {progress !== null && (
                <>
                  <LinearProgress
                    aria-label="Message history progress"
                    value={progress}
                    variant="determinate"
                  />
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                    {discoveredConversations === null
                      ? `${NUMBER_FORMAT.format(completeConversations)} conversations synced so far`
                      : `${NUMBER_FORMAT.format(completeConversations)} of ${NUMBER_FORMAT.format(discoveredConversations)} conversations`}
                  </Typography>
                </>
              )}
            </Stack>
          )}

          {hasConsent && settings.desired_state !== settings.effective_state && (
            <Alert severity="info" role="status">{waitingText(settings)}</Alert>
          )}

          {!canManageHistorySync && (
            <Alert severity="info">Only the account owner can change message history.</Alert>
          )}

          {!hasConsent && !waitingForExtension && canManageHistorySync && (
            <FormControlLabel
              control={(
                <Checkbox
                  checked={consentAccepted}
                  onChange={(event) => setConsentAccepted(event.target.checked)}
                />
              )}
              label="I allow read-only syncing of my older messages to this computer."
            />
          )}

          {canManageHistorySync && !waitingForExtension && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
              {!hasConsent ? (
                <Button
                  disabled={!consentAccepted || busy}
                  onClick={() => void update('running', true)}
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="contained"
                >
                  Turn on message history
                </Button>
              ) : paused ? (
                <Button
                  disabled={busy}
                  onClick={() => void update('running')}
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="contained"
                >
                  Resume
                </Button>
              ) : (
                <Button
                  disabled={busy}
                  onClick={() => void update('paused')}
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
                  sx={{ color: 'text.secondary' }}
                >
                  Turn off
                </Button>
              )}
            </Stack>
          )}
        </Stack>
      ) : null}

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
