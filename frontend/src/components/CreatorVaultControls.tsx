import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Chip,
  Divider,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useState } from 'react';

import { Panel } from './ui';
import { usePermissions } from '../hooks/usePermissions';
import {
  creatorVaultApi as defaultCreatorVaultApi,
  type CreatorVaultApi,
  type CreatorVaultCommand,
  type CreatorVaultExportDocument,
  type CreatorVaultStatus,
  type UnlinkArchiveTreatment,
} from '../services/creatorVaultApi';

export type CreatorVaultDownload = (document: CreatorVaultExportDocument) => void;

interface CreatorVaultControlsProps {
  api?: CreatorVaultApi;
  onDownload?: CreatorVaultDownload;
}

type SelectiveScope = 'message' | 'conversation' | 'participant';

function sentenceCase(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function defaultDownload(document: CreatorVaultExportDocument): void {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = window.document.createElement('a');
  anchor.href = url;
  anchor.download = 'creator-vault-export.json';
  anchor.click();
  URL.revokeObjectURL(url);
}

export function CreatorVaultControls({
  api = defaultCreatorVaultApi,
  onDownload = defaultDownload,
}: CreatorVaultControlsProps) {
  const { isCreator } = usePermissions();
  const [status, setStatus] = useState<CreatorVaultStatus | null>(null);
  const [loading, setLoading] = useState(isCreator);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [finiteDays, setFiniteDays] = useState('365');
  const [scope, setScope] = useState<SelectiveScope>('message');
  const [targetId, setTargetId] = useState('');
  const [unlinkTreatment, setUnlinkTreatment] = useState<UnlinkArchiveTreatment>('preserve');

  useEffect(() => {
    if (!isCreator) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    void api.get(controller.signal).then(
      (next) => {
        setStatus(next);
        setError(null);
        setLoading(false);
      },
      (cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : 'Creator Vault status is unavailable.');
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [api, isCreator]);

  const run = async (command: CreatorVaultCommand) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.command(command);
      setStatus(result.status);
      if (command.action.startsWith('delete_')) {
        setNotice('The managed Vault deletion was recorded.');
      } else if (command.action === 'unlink') {
        setNotice(
          command.unlink_archive_treatment === 'preserve'
            ? 'Vault preservation was selected for unlink.'
            : 'Vault deletion was selected for unlink.',
        );
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The Creator Vault change failed.');
    } finally {
      setBusy(false);
    }
  };

  const exportVault = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const document = await api.exportDocument();
      onDownload(document);
      const recovery = document.manifest.copy_domains.managed_recovery;
      setNotice(
        recovery.copies_may_remain
          ? 'Export created. Product-managed recovery copies may still remain; the export itself is outside managed Vault deletion after delivery.'
          : 'Export created. The export itself is outside managed Vault deletion after delivery.',
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Creator Vault export failed.');
    } finally {
      setBusy(false);
    }
  };

  if (!isCreator) {
    return (
      <Panel>
        <Alert severity="info">
          Creator Vault controls are available only to the creator account owner.
        </Alert>
      </Panel>
    );
  }

  const days = Number.parseInt(finiteDays, 10);
  const finiteValid = Number.isInteger(days) && days > 0;
  const selectiveAction = `delete_${scope}` as CreatorVaultCommand['action'];

  return (
    <Panel>
      <Stack spacing={2}>
        <Stack
          direction={{ xs: 'column', sm: 'row' }}
          spacing={2}
          sx={{ justifyContent: 'space-between' }}
        >
          <Box>
            <Typography component="h2" variant="h6">Creator Vault</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary', maxWidth: 680 }}>
              Keep an explicit local archive separately from analytics. Vault is disabled by
              default, and its lifecycle controls operate on the same canonical retention authority
              used by ingestion, deletion barriers, restore, and export.
            </Typography>
          </Box>
          <Chip
            label={loading ? 'Loading' : status?.policy.enabled ? 'Enabled' : 'Disabled'}
            color={status?.policy.enabled ? 'success' : 'default'}
            variant="outlined"
          />
        </Stack>

        {error && (
          <Alert severity="error" role="alert">
            <AlertTitle>Creator Vault needs attention</AlertTitle>
            {error}
          </Alert>
        )}
        {notice && <Alert severity="info" role="status">{notice}</Alert>}

        {!loading && status !== null && (
          <>
            <Divider />
            <Stack spacing={1.5}>
              <Typography variant="subtitle2">Archive lifecycle</Typography>
              {status.policy.enabled ? (
                <>
                  <Typography variant="body2">
                    Policy: {sentenceCase(status.policy.policy_type)}
                    {status.policy.finite_horizon_days !== null
                      ? ` · ${status.policy.finite_horizon_days} days`
                      : ''}
                  </Typography>
                  <Button
                    disabled={busy}
                    onClick={() => void run({ action: 'disable' })}
                    variant="outlined"
                  >
                    Disable Vault
                  </Button>
                </>
              ) : (
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
                  <TextField
                    label="Retention days"
                    onChange={(event) => setFiniteDays(event.target.value)}
                    size="small"
                    type="number"
                    value={finiteDays}
                  />
                  <Button
                    disabled={busy || !finiteValid}
                    onClick={() => void run({
                      action: 'enable_finite',
                      finite_horizon_days: days,
                    })}
                    variant="contained"
                  >
                    Enable finite Vault
                  </Button>
                  {status.capabilities.indefinite_retention && (
                    <Button
                      disabled={busy}
                      onClick={() => void run({ action: 'enable_indefinite' })}
                      variant="outlined"
                    >
                      Keep until I delete
                    </Button>
                  )}
                </Stack>
              )}
            </Stack>

            <Divider />

            <Stack spacing={1.5}>
              <Typography variant="subtitle2">Delete managed Vault data</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Deletion writes the existing durable barrier so ordinary replay, reconnect, restore,
                and rebuild paths cannot silently recreate the deleted scope.
              </Typography>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
                <FormControl size="small" sx={{ minWidth: 160 }}>
                  <InputLabel id="vault-delete-scope-label">Scope</InputLabel>
                  <Select
                    label="Scope"
                    labelId="vault-delete-scope-label"
                    onChange={(event) => setScope(event.target.value as SelectiveScope)}
                    value={scope}
                  >
                    <MenuItem value="message">Message</MenuItem>
                    <MenuItem value="conversation">Conversation</MenuItem>
                    <MenuItem value="participant">Participant</MenuItem>
                  </Select>
                </FormControl>
                <TextField
                  label={`${sentenceCase(scope)} ID`}
                  onChange={(event) => setTargetId(event.target.value)}
                  size="small"
                  value={targetId}
                />
                <Button
                  color="error"
                  disabled={busy || !targetId.trim()}
                  onClick={() => void run({ action: selectiveAction, target_id: targetId.trim() })}
                  variant="outlined"
                >
                  Delete selected scope
                </Button>
                <Button
                  color="error"
                  disabled={busy}
                  onClick={() => void run({ action: 'delete_all' })}
                  variant="contained"
                >
                  Delete all Vault data
                </Button>
              </Stack>
            </Stack>

            <Divider />

            <Stack spacing={1.5}>
              <Typography variant="subtitle2">Unlink archive treatment</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Choose what happens to the managed Vault when applying an account-unlink lifecycle
                event. This control changes Vault treatment; account connectivity is managed
                separately.
              </Typography>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
                <FormControl size="small" sx={{ minWidth: 190 }}>
                  <InputLabel id="vault-unlink-treatment-label">Archive treatment</InputLabel>
                  <Select
                    label="Archive treatment"
                    labelId="vault-unlink-treatment-label"
                    onChange={(event) => setUnlinkTreatment(
                      event.target.value as UnlinkArchiveTreatment,
                    )}
                    value={unlinkTreatment}
                  >
                    <MenuItem value="preserve">Preserve Vault</MenuItem>
                    <MenuItem value="delete">Delete Vault</MenuItem>
                  </Select>
                </FormControl>
                <Button
                  color={unlinkTreatment === 'delete' ? 'error' : 'primary'}
                  disabled={busy}
                  onClick={() => void run({
                    action: 'unlink',
                    unlink_archive_treatment: unlinkTreatment,
                  })}
                  variant="outlined"
                >
                  Apply unlink treatment
                </Button>
              </Stack>
            </Stack>

            <Divider />

            <Stack spacing={1}>
              <Typography variant="subtitle2">Export</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Export the current managed Vault plus its state-derived manifest. Once delivered,
                that file is outside Product-managed Vault deletion.
              </Typography>
              <Button
                disabled={busy || !status.capabilities.export}
                onClick={() => void exportVault()}
                variant="outlined"
              >
                Export Creator Vault
              </Button>
            </Stack>
          </>
        )}
      </Stack>
    </Panel>
  );
}
