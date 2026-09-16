import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
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

import { Disclosure, Panel } from '../components/ui';
import { usePermissions } from '../hooks/usePermissions';
import type { HistorySettings } from '../protocol';
import {
  historySettingsApi as defaultHistorySettingsApi,
  HistorySettingsApiError,
  type HistorySettingsApi,
} from '../services/historySettingsApi';
import { bridgeTransportStore } from '../store/transportStore';
import { coverageProgressLabel } from '../utils/dataReadiness';

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

  return (
    <Panel>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
        <Typography component="h2" variant="h6">Message history</Typography>
        {settings && (
          <Chip
            color={!hasConsent ? 'default' : paused ? 'warning' : 'success'}
            label={!hasConsent ? 'Off' : paused ? 'Paused' : 'On'}
            size="small"
            variant="outlined"
          />
        )}
      </Stack>

      {error && <Alert severity="error" role="alert">{error}</Alert>}

      {loading ? (
        <Stack spacing={1} role="status" aria-label="Loading message history settings">
          <Skeleton width="60%" />
          <Skeleton width="80%" />
          <Skeleton height={40} width={200} variant="rounded" />
        </Stack>
      ) : settings !== null ? (
        <Stack spacing={2}>
          {!hasConsent ? (
            <Stack spacing={1}>
              <Typography variant="body2">
                Add your older conversations so your numbers cover your whole message history, not
                just new messages.
              </Typography>
              <Box component="ul" sx={{ color: 'text.secondary', m: 0, pl: 2.5, typography: 'body2' }}>
                <li>Read-only: it never sends messages or changes your account.</li>
                <li>Everything stays on this computer.</li>
                <li>Pause or turn it off at any time.</li>
              </Box>
            </Stack>
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
                      ? `${completeConversations} conversations synced so far`
                      : `${completeConversations} of ${discoveredConversations} conversations synced`}
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

          {!hasConsent && canManageHistorySync && (
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

          {canManageHistorySync && (
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
                <Button color="error" disabled={busy} onClick={() => setConfirmRevoke(true)}>
                  Turn off
                </Button>
              )}
            </Stack>
          )}

          <Disclosure label="Details">
            <Box
              component="dl"
              sx={{ columnGap: 3, display: 'grid', gridTemplateColumns: 'auto 1fr', m: 0, rowGap: 0.5 }}
            >
              {[
                ['Sync order', `Newest ${settings.recent_window_days} days first, then older messages`],
                ['Creator account', settings.authorized_platform_creator_id ?? 'Set when you turn this on'],
                ['Consent version', settings.consent_policy_version],
              ].map(([label, value]) => (
                <Box key={label} sx={{ display: 'contents' }}>
                  <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
                    {label}
                  </Typography>
                  <Typography component="dd" variant="body2" sx={{ m: 0, overflowWrap: 'anywhere' }}>
                    {value}
                  </Typography>
                </Box>
              ))}
            </Box>
          </Disclosure>
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
