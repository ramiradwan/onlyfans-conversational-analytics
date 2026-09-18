import {
  Alert,
  AlertTitle,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  FormControlLabel,
  Radio,
  RadioGroup,
  Skeleton,
  Stack,
  TextField,
} from '@mui/material';
import { useEffect, useId, useState } from 'react';

import { Panel, SectionHeader, SettingRow } from './ui';
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
type ArchiveChoice = 'finite' | 'indefinite';

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
  const [archiveChoice, setArchiveChoice] = useState<ArchiveChoice>('finite');
  const archiveTitleId = useId();
  const archiveChoiceId = useId();

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

  const closeArchive = () => {
    setEditingArchive(false);
    setError(null);
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
  const indefiniteAvailable = status?.capabilities.indefinite_retention === true;
  const keepChoice: ArchiveChoice = indefiniteAvailable ? archiveChoice : 'finite';
  const daysField = (
    <TextField
      label="Days to keep"
      onChange={(event) => setFiniteDays(event.target.value)}
      size="small"
      sx={{ maxWidth: 160, ml: indefiniteAvailable ? 4 : 0, my: indefiniteAvailable ? 1 : 0 }}
      type="number"
      value={finiteDays}
    />
  );

  return (
    <Panel data-visual="stored-messages" sx={{ p: 0,
      '& .MuiButton-outlined:not(.Mui-disabled)': { color: 'action.selectedForeground', borderColor: 'surface.trust.border' },
    }}>
      <Stack data-journey-state="desktop.stored_messages" spacing={0}
        sx={{ '& > .MuiAlert-root, & > [role="status"]': { m: 3 } }}>
        <SectionHeader sx={{ p: 3 }} summary="Saved only on this computer." title="Stored messages" />

        {error && !editingArchive && <Alert severity="error" role="alert">{error}</Alert>}
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

            <SettingRow
              sx={{ px: 3, py: 2.25 }}
              action={status.policy.enabled ? (
                <Button
                  disabled={busy}
                  onClick={() => { setPending({ kind: 'disable' }); setConfirmOpen(true); }}
                  size="small"
                  sx={{ color: 'text.secondary' }}
                >
                  Turn off archive
                </Button>
              ) : (
                <Button
                  aria-haspopup="dialog"
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

            <Divider />

            <SettingRow
              sx={{ px: 3, py: 2.25 }}
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

            <SettingRow
              sx={{ px: 3, py: 2.25 }}
              action={(
                <Button
                  aria-haspopup="dialog"
                  color="error"
                  disabled={busy}
                  onClick={() => { setPending({ kind: 'delete_all' }); setConfirmOpen(true); }}
                  size="small"
                >
                  Delete all messages
                </Button>
              )}
              description="Uninstalling the app doesn't delete them, so delete them here if you want them gone."
              title="Delete messages"
            />
          </>
        )}
      </Stack>

      <Dialog
        aria-labelledby={archiveTitleId}
        fullWidth
        maxWidth="xs"
        onClose={closeArchive}
        open={editingArchive && status !== null && !status.policy.enabled}
      >
        <DialogTitle id={archiveTitleId}>Turn on archive</DialogTitle>
        <DialogContent>
          <Stack spacing={2}>
            <DialogContentText id={archiveChoiceId} variant="body2">
              Choose how long this computer keeps your messages.
            </DialogContentText>
            {indefiniteAvailable ? (
              <RadioGroup
                aria-labelledby={archiveChoiceId}
                onChange={(event) => setArchiveChoice(event.target.value as ArchiveChoice)}
                value={keepChoice}
              >
                <FormControlLabel control={<Radio />} label="Keep for a set number of days" value="finite" />
                {keepChoice === 'finite' && daysField}
                <FormControlLabel control={<Radio />} label="Keep until I delete them" value="indefinite" />
              </RadioGroup>
            ) : (
              daysField
            )}
            {error && <Alert severity="error" role="alert">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={closeArchive}>Cancel</Button>
          <Button
            disabled={busy || (keepChoice === 'finite' && !finiteValid)}
            onClick={() => void run(keepChoice === 'finite'
              ? { action: 'enable_finite', finite_horizon_days: days }
              : { action: 'enable_indefinite' })}
            variant="contained"
          >
            Turn on archive
          </Button>
        </DialogActions>
      </Dialog>

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
