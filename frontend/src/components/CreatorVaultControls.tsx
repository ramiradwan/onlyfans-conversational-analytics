import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  Skeleton,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useState } from 'react';

import { Disclosure, Panel, SectionHeader, SettingRow } from './ui';
import { usePermissions } from '../hooks/usePermissions';
import {
  creatorVaultApi as defaultCreatorVaultApi,
  type CreatorVaultApi,
  type CreatorVaultCommand,
  type CreatorVaultExportDocument,
  type CreatorVaultStatus,
} from '../services/creatorVaultApi';

export type CreatorVaultDownload = (document: CreatorVaultExportDocument) => void;

interface CreatorVaultControlsProps {
  api?: CreatorVaultApi;
  onDownload?: CreatorVaultDownload;
}

type PendingConfirmation = { kind: 'delete_all' } | { kind: 'disable' };

function defaultDownload(document: CreatorVaultExportDocument): void {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = window.document.createElement('a');
  anchor.href = url;
  anchor.download = 'stored-messages.json';
  anchor.click();
  URL.revokeObjectURL(url);
}

function archiveLabel(policy: CreatorVaultStatus['policy']): string {
  if (!policy.enabled) return 'Off';
  if (policy.policy_type === 'finite' && policy.finite_horizon_days !== null) {
    return `Keeping messages for ${policy.finite_horizon_days} days`;
  }
  return policy.policy_type === 'indefinite_until_delete' ? 'Keeping messages until you delete them' : 'On';
}

/** Stored messages section of Settings: archive length, download, and deletion. */
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
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [editingArchive, setEditingArchive] = useState(false);

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
      () => {
        if (controller.signal.aborted) return;
        setError("Stored message settings couldn't be loaded. Reload the page to try again.");
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
      if (command.action.startsWith('enable_')) setEditingArchive(false);
      if (command.action.startsWith('delete_') && result.deletion_operation?.status !== 'incomplete') {
        setNotice('Messages deleted.');
      }
    } catch {
      setError(command.action.startsWith('delete_')
        ? "Messages couldn't be deleted. Try again."
        : "Your change couldn't be saved. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const retryDeletion = async () => {
    const operation = status?.deletion_operation;
    if (!operation || !api.retryDeletion) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.retryDeletion(operation.operation_id);
      setStatus((current) => current === null ? null : {
        ...current,
        deletion_operation: result.status === 'complete' ? null : result,
      });
      setNotice(result.status === 'complete' ? 'Messages deleted.' : null);
    } catch {
      setError("Deleting couldn't be finished. Try again.");
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
          ? 'Download started. Some backup copies may stay on this computer for a while after you delete messages.'
          : 'Download started.',
      );
    } catch {
      setError("Your messages couldn't be downloaded. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = () => {
    const current = pending;
    setConfirmOpen(false);
    if (current?.kind === 'delete_all') void run({ action: 'delete_all' });
    if (current?.kind === 'disable') void run({ action: 'disable' });
  };

  if (!isCreator) {
    return (
      <Panel>
        <SectionHeader title="Stored messages" />
        <Alert severity="info">Only the account owner can manage stored messages.</Alert>
      </Panel>
    );
  }

  const days = Number.parseInt(finiteDays, 10);
  const finiteValid = Number.isInteger(days) && days > 0;

  return (
    <Panel>
      <Stack spacing={2}>
        <SectionHeader summary="Saved only on this computer." title="Stored messages" />

        {error && <Alert severity="error" role="alert">{error}</Alert>}
        {notice && <Alert severity="success" role="status">{notice}</Alert>}
        {status?.deletion_operation && (
          <Alert
            action={api.retryDeletion && (
              <Button color="inherit" disabled={busy} onClick={() => void retryDeletion()} size="small">
                Finish deleting
              </Button>
            )}
            severity="warning"
          >
            <AlertTitle>Deleting isn&apos;t finished</AlertTitle>
            The messages are gone, but some of your numbers still include them.
          </Alert>
        )}

        {loading && (
          <Stack spacing={1} role="status" aria-label="Loading stored message settings">
            <Skeleton width="50%" />
            <Skeleton height={40} width={220} variant="rounded" />
          </Stack>
        )}

        {!loading && status !== null && (
          <>
            <Divider />
            <Stack spacing={1.5}>
              <SettingRow
                action={status.policy.enabled ? (
                  <Button
                    disabled={busy}
                    onClick={() => { setPending({ kind: 'disable' }); setConfirmOpen(true); }}
                    size="small"
                    sx={{ color: 'text.secondary' }}
                  >
                    Turn off archive
                  </Button>
                ) : !editingArchive && (
                  <Button
                    aria-expanded={false}
                    disabled={busy}
                    onClick={() => setEditingArchive(true)}
                    size="small"
                    variant="outlined"
                  >
                    Turn on archive
                  </Button>
                )}
                description={archiveLabel(status.policy)}
                title="Archive"
              />
              {!status.policy.enabled && (
                <Collapse in={editingArchive} unmountOnExit>
                  <Stack spacing={1.5}>
                    <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                      Choose how long this computer keeps your messages.
                    </Typography>
                    <Stack
                      direction={{ xs: 'column', sm: 'row' }}
                      spacing={1.5}
                      sx={{ alignItems: { xs: 'flex-start', sm: 'center' } }}
                    >
                      <TextField
                        label="Days to keep"
                        onChange={(event) => setFiniteDays(event.target.value)}
                        size="small"
                        type="number"
                        value={finiteDays}
                      />
                      <Button
                        disabled={busy || !finiteValid}
                        onClick={() => void run({ action: 'enable_finite', finite_horizon_days: days })}
                        variant="contained"
                      >
                        Keep messages
                      </Button>
                      {status.capabilities.indefinite_retention && (
                        <Button
                          disabled={busy}
                          onClick={() => void run({ action: 'enable_indefinite' })}
                          size="small"
                          variant="outlined"
                        >
                          Keep until I delete
                        </Button>
                      )}
                      <Button disabled={busy} onClick={() => setEditingArchive(false)} size="small">
                        Cancel
                      </Button>
                    </Stack>
                  </Stack>
                </Collapse>
              )}
            </Stack>

            <Divider />

            <SettingRow
              action={(
                <Button
                  disabled={busy || !status.capabilities.export}
                  onClick={() => void exportVault()}
                  size="small"
                  variant="outlined"
                >
                  Download messages
                </Button>
              )}
              description="Save your stored messages to a file. Deleting messages here doesn't delete the file."
              title="Download a copy"
            />

            <Divider />

            <Disclosure label="Delete messages">
              <Stack spacing={2}>
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  Deleted messages are removed from this computer and your numbers are updated without
                  them. Uninstalling the app doesn&apos;t delete them, so delete them here first if you
                  want them gone.
                </Typography>
                <Box>
                  <Button
                    color="error"
                    disabled={busy}
                    onClick={() => { setPending({ kind: 'delete_all' }); setConfirmOpen(true); }}
                    variant="outlined"
                  >
                    Delete all messages
                  </Button>
                </Box>
              </Stack>
            </Disclosure>
          </>
        )}
      </Stack>

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>
          {pending?.kind === 'delete_all' ? 'Delete all messages?' : 'Turn off the archive?'}
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            {pending?.kind === 'disable'
              ? 'Older messages kept only by the archive will be removed from this computer.'
              : 'Every stored message for this account is removed from this computer, and your numbers are updated without them. This can’t be undone.'}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button color="error" onClick={confirm} variant="contained">
            {pending?.kind === 'delete_all' ? 'Delete all' : 'Turn off'}
          </Button>
        </DialogActions>
      </Dialog>
    </Panel>
  );
}
