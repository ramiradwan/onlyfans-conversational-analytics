import DeleteOutlinedIcon from '@mui/icons-material/DeleteOutlined';
import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  LinearProgress,
  Skeleton,
  Stack,
  Typography,
} from '@mui/material';
import { useEffect, useState, useSyncExternalStore } from 'react';

import { Panel } from '../components/ui';
import { usePermissions } from '../hooks/usePermissions';
import type { HistorySettings } from '../protocol';
import {
  historySettingsApi as defaultHistorySettingsApi,
  type HistorySettingsApi,
} from '../services/historySettingsApi';
import { bridgeTransportStore } from '../store/transportStore';
import { coverageProgressLabel } from '../utils/dataReadiness';

interface SettingsViewProps {
  api?: HistorySettingsApi;
}

function stateColor(state: HistorySettings['effective_state']) {
  if (state === 'running') return 'success' as const;
  if (state === 'paused') return 'warning' as const;
  return 'default' as const;
}

function sentenceCase(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

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
      (cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : 'History settings are unavailable.');
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
      setError(cause instanceof Error ? cause.message : 'The settings change failed.');
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
      setError(cause instanceof Error ? cause.message : 'Consent could not be revoked.');
    } finally {
      setBusy(false);
    }
  };

  const hasConsent = settings?.consent_revision !== null && settings?.desired_state !== 'revoked';
  const progress = transport.snapshotProgress.percentage;

  return (
    <Box sx={{ maxWidth: 960, mx: 'auto', width: '100%' }}>
      <Stack spacing={3}>
        <Box>
          <Typography component="h1" variant="h4">Settings</Typography>
          <Typography
            variant="body2"
            sx={{
              color: 'text.secondary',
              mt: 0.5
            }}>
            Control local historical acquisition and see exactly what data is ready.
          </Typography>
        </Box>

        {error && (
          <Alert severity="error" role="alert">
            <AlertTitle>Settings need attention</AlertTitle>
            {error}
          </Alert>
        )}

        {transport.agent?.degraded_reason && (
          <Alert severity="warning" role="status">
            <AlertTitle>Local Agent needs attention</AlertTitle>
            {transport.agent.degraded_reason}{' '}
            If the History Sync extension is installed and enabled, reload this page to pair it.
          </Alert>
        )}

        {!canManageHistorySync && (
          <Alert severity="info">
            History controls are available to the creator account owner. You can still view status.
          </Alert>
        )}

        <Panel>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{
            justifyContent: 'space-between'
          }}>
            <Box>
              <Typography component="h2" variant="h6">Historical coverage</Typography>
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>
                {coverageProgressLabel(transport.coverage)}
              </Typography>
            </Box>
            <Chip
              label={sentenceCase(transport.coverage.phase)}
              color={transport.coverage.phase === 'blocked' ? 'error' : 'default'}
              variant="outlined"
            />
          </Stack>
          {progress !== null && (
            <Stack spacing={0.75}>
              <LinearProgress
                aria-label="Historical coverage progress"
                value={progress}
                variant="determinate"
              />
              <Typography variant="caption" sx={{
                color: 'text.secondary'
              }}>
                {transport.snapshotProgress.completeConversations} of{' '}
                {transport.snapshotProgress.discoveredConversations ?? 'unknown'} conversations complete
              </Typography>
            </Stack>
          )}
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <Chip label={`Projection: ${sentenceCase(transport.projection.status)}`} size="small" />
            <Chip label={`Live updates: ${sentenceCase(transport.liveFreshness.status)}`} size="small" />
          </Stack>
        </Panel>

        <Panel>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{
            justifyContent: 'space-between'
          }}>
            <Box>
              <Typography component="h2" variant="h6">Historical message sync</Typography>
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  maxWidth: 680
                }}>
                Read older creator-visible conversations through your authenticated browser session.
                Acquisition is read-only, stays local, and can be paused or revoked at any time.
              </Typography>
            </Box>
            {settings && (
              <Stack spacing={0.75} sx={{
                alignItems: { sm: 'flex-end' }
              }}>
                <Chip
                  color={stateColor(settings.effective_state)}
                  label={`Effective: ${sentenceCase(settings.effective_state)}`}
                  variant="outlined"
                />
                <Typography variant="caption" sx={{
                  color: 'text.secondary'
                }}>
                  Requested: {sentenceCase(settings.desired_state)}
                </Typography>
              </Stack>
            )}
          </Stack>

          <Divider />

          {loading ? (
            <Stack spacing={1} role="status" aria-label="Loading history settings">
              <Skeleton width="45%" />
              <Skeleton width="80%" />
              <Skeleton height={40} width={180} variant="rounded" />
            </Stack>
          ) : settings !== null ? (
            <Stack spacing={2}>
              {settings.desired_state !== settings.effective_state && (
                <Alert severity="info" role="status">
                  The requested state is saved. Waiting for the bound Agent to apply the new
                  configuration.
                </Alert>
              )}
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={3}>
                <Box>
                  <Typography variant="caption" sx={{
                    color: 'text.secondary'
                  }}>Recent-first priority</Typography>
                  <Typography variant="body2">
                    Start with the latest {settings.recent_window_days} days
                  </Typography>
                  <Typography
                    variant="caption"
                    sx={{
                      color: 'text.secondary',
                      display: 'block'
                    }}>
                    Sync then continues to the proven start of available history.
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" sx={{
                    color: 'text.secondary'
                  }}>Page size</Typography>
                  <Typography variant="body2">{settings.page_size} messages</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" sx={{
                    color: 'text.secondary'
                  }}>Applied revision</Typography>
                  <Typography variant="body2">
                    {settings.effective_config_revision ?? 'Waiting to apply'}
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" sx={{
                    color: 'text.secondary'
                  }}>
                    Authorized platform identity
                  </Typography>
                  <Typography variant="body2">
                    {settings.authorized_platform_creator_id ?? 'Established when consent starts'}
                  </Typography>
                </Box>
              </Stack>

              {!hasConsent && canManageHistorySync && (
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={consentAccepted}
                      onChange={(event) => setConsentAccepted(event.target.checked)}
                    />
                  }
                  label={`I authorize read-only local historical sync under policy ${settings.consent_policy_version}.`}
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
                      Start historical sync
                    </Button>
                  ) : settings.desired_state === 'running' ? (
                    <Button
                      disabled={busy}
                      onClick={() => void update('paused')}
                      startIcon={<PauseCircleOutlinedIcon />}
                      variant="outlined"
                    >
                      Pause sync
                    </Button>
                  ) : (
                    <Button
                      disabled={busy}
                      onClick={() => void update('running')}
                      startIcon={<PlayCircleOutlinedIcon />}
                      variant="contained"
                    >
                      Resume sync
                    </Button>
                  )}

                  {hasConsent && (
                    <Button
                      color="error"
                      disabled={busy}
                      onClick={() => setConfirmRevoke(true)}
                      startIcon={<DeleteOutlinedIcon />}
                    >
                      Revoke consent
                    </Button>
                  )}
                </Stack>
              )}
            </Stack>
          ) : null}
        </Panel>
      </Stack>

      <Dialog open={confirmRevoke} onClose={() => !busy && setConfirmRevoke(false)}>
        <DialogTitle>Revoke historical sync consent?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            Historical acquisition will stop and this browser’s paged message cache will be cleared.
            History already synced to the server is retained — revoking stops further collection, it
            does not delete data already acquired. Live capture status is managed separately.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => setConfirmRevoke(false)}>Cancel</Button>
          <Button color="error" disabled={busy} onClick={() => void revoke()} variant="contained">
            Revoke consent
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
