import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControlLabel,
  Radio,
  RadioGroup,
  Stack,
  Switch,
  TextField,
} from '@mui/material';
import { useEffect, useId, useState } from 'react';

import { Panel, quietNoteSx, SectionHeader, SettingRow, useRevealHold } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import { VaultRowsSkeleton } from './ui/Skeletons';
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

const ARCHIVE_PURPOSE = 'Choose how long this computer keeps your messages.';

function archiveLabel(policy: CreatorVaultStatus['policy']): string {
  if (!policy.enabled) return ARCHIVE_PURPOSE;
  if (policy.policy_type === 'finite' && policy.finite_horizon_days !== null) {
    return `Keeping messages for ${policy.finite_horizon_days} days.`;
  }
  return policy.policy_type === 'indefinite_until_delete' ? 'Keeping messages until you delete them.' : 'On';
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
  useRevealHold(loading);

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
        <SectionHeader icon={<Inventory2Outlined />} title="Stored messages" />
        <Alert severity="info" sx={quietNoteSx}>Only the account owner can manage stored messages.</Alert>
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
    <Panel data-visual="stored-messages" sx={{
      // Once loaded, the reserved feedback line ends the card and stands in for most of its bottom padding.
      ...(loading ? {} : { pb: 1.5 }),
      '& .MuiButton-outlined:not(.Mui-disabled)': { color: 'action.selectedForeground' },
    }}>
      <Stack data-journey-state="desktop.stored_messages" spacing={0}>
        <SectionHeader icon={<Inventory2Outlined />} sx={{ pb: 1.5 }} summary="Saved only on this computer." title="Stored messages" />

        {loading && <VaultRowsSkeleton />}

        {!loading && status !== null && (
          <>
            <SettingRow
              action={(
                // Either change asks first, so the switch moves only once the dialog is confirmed.
                <Switch
                  checked={status.policy.enabled}
                  disabled={busy}
                  onChange={() => {
                    if (status.policy.enabled) { setPending({ kind: 'disable' }); setConfirmOpen(true); }
                    else setEditingArchive(true);
                  }}
                  slotProps={{ input: { 'aria-label': 'Archive', 'aria-haspopup': 'dialog' } }}
                />
              )}
              description={archiveLabel(status.policy)}
              title="Archive"
            />

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
              description="Save your stored messages to a file."
              title="Download a copy"
            />

            <SettingRow
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
              description="Messages stay on this computer until you delete them, even after you uninstall the app."
              title="Delete messages"
            />
          </>
        )}
        {!loading && (
          <Box sx={{ display: 'flex', gap: 1 }}>
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <StatusLine
                id="vault-feedback"
                tone={error ? 'error' : 'secondary'}
                text={!editingArchive && error ? error : notice ?? (status?.deletion_operation ? "Deleting isn't finished. The messages are gone, but some of your numbers still include them." : null)}
              />
            </Box>
            {api.retryDeletion && status?.deletion_operation && (
              <Button sx={{ alignSelf: 'flex-start' }} color="inherit" disabled={busy} onClick={() => void retryDeletion()} size="small" variant="outlined">
                Finish deleting
              </Button>
            )}
          </Box>
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
                <Box sx={{ height: '5rem', visibility: keepChoice === 'finite' ? 'visible' : 'hidden' }}>{daysField}</Box>
                <FormControlLabel control={<Radio />} label="Keep until I delete them" value="indefinite" />
              </RadioGroup>
            ) : (
              daysField
            )}
            <StatusLine id="archive-feedback" text={error} tone="error" />
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
