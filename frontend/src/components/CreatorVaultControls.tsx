import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Skeleton,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useState } from 'react';

import { Disclosure, Panel } from './ui';
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

type SelectiveScope = 'message' | 'conversation' | 'participant';

const SCOPE_LABELS: Record<SelectiveScope, string> = {
  message: 'Message',
  conversation: 'Conversation',
  participant: 'Person',
};

const SCOPE_TITLES: Record<SelectiveScope, string> = {
  message: 'Delete this message?',
  conversation: 'Delete this conversation?',
  participant: "Delete this person's messages?",
};

type PendingConfirmation =
  | { kind: 'delete_all' }
  | { kind: 'delete_scope'; scope: SelectiveScope; targetId: string }
  | { kind: 'disable' };

function defaultDownload(document: CreatorVaultExportDocument): void {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = window.document.createElement('a');
  anchor.href = url;
  anchor.download = 'creator-vault-export.json';
  anchor.click();
  URL.revokeObjectURL(url);
}

function archiveLabel(policy: CreatorVaultStatus['policy']): string {
  if (!policy.enabled) return 'Off';
  if (policy.policy_type === 'finite' && policy.finite_horizon_days !== null) {
    return `${policy.finite_horizon_days} days`;
  }
  return policy.policy_type === 'indefinite_until_delete' ? 'Until you delete' : 'On';
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
  const [scope, setScope] = useState<SelectiveScope>('message');
  const [targetId, setTargetId] = useState('');
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

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
      if (command.action.startsWith('delete_') && result.deletion_operation?.status !== 'incomplete') {
        setNotice('Messages deleted.');
      }
      if (command.action === 'delete_message' || command.action === 'delete_conversation'
        || command.action === 'delete_participant') {
        setTargetId('');
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
    if (current?.kind === 'delete_scope') {
      void run({
        action: `delete_${current.scope}` as CreatorVaultCommand['action'],
        target_id: current.targetId,
      });
    }
  };

  if (!isCreator) {
    return (
      <Panel>
        <Typography component="h2" variant="h6">Stored messages</Typography>
        <Alert severity="info">Only the account owner can manage stored messages.</Alert>
      </Panel>
    );
  }

  const days = Number.parseInt(finiteDays, 10);
  const finiteValid = Number.isInteger(days) && days > 0;

  return (
    <Panel>
      <Stack spacing={2}>
        <Box>
          <Typography component="h2" variant="h6">Stored messages</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            Your messages are saved only on this computer.
          </Typography>
        </Box>

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
              <Stack direction="row" spacing={2} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
                <Typography component="h3" variant="subtitle2">Keep a longer archive</Typography>
                <Chip
                  color={status.policy.enabled ? 'success' : 'default'}
                  label={archiveLabel(status.policy)}
                  size="small"
                  variant="outlined"
                />
              </Stack>
              {status.policy.enabled ? (
                <Box>
                  <Button
                    disabled={busy}
                    onClick={() => { setPending({ kind: 'disable' }); setConfirmOpen(true); }}
                    variant="outlined"
                  >
                    Turn off archive
                  </Button>
                </Box>
              ) : (
                <>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                    Older messages are removed automatically. Keep them longer to build your own archive.
                  </Typography>
                  <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' } }}>
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
                        variant="outlined"
                      >
                        Keep until I delete
                      </Button>
                    )}
                  </Stack>
                </>
              )}
            </Stack>

            <Divider />

            <Stack spacing={1.5}>
              <Typography component="h3" variant="subtitle2">Download a copy</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Save your stored messages to a file. Deleting messages here doesn&apos;t delete the file.
              </Typography>
              <Box>
                <Button
                  disabled={busy || !status.capabilities.export}
                  onClick={() => void exportVault()}
                  variant="outlined"
                >
                  Download messages
                </Button>
              </Box>
            </Stack>

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
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  Or delete one message, conversation, or person by ID.
                </Typography>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
                  <FormControl size="small" sx={{ minWidth: 180 }}>
                    <InputLabel id="vault-delete-scope-label">What to delete</InputLabel>
                    <Select
                      label="What to delete"
                      labelId="vault-delete-scope-label"
                      onChange={(event) => setScope(event.target.value as SelectiveScope)}
                      value={scope}
                    >
                      {(Object.keys(SCOPE_LABELS) as SelectiveScope[]).map((value) => (
                        <MenuItem key={value} value={value}>{SCOPE_LABELS[value]}</MenuItem>
                      ))}
                    </Select>
                  </FormControl>
                  <TextField
                    label={`${SCOPE_LABELS[scope]} ID`}
                    onChange={(event) => setTargetId(event.target.value)}
                    size="small"
                    value={targetId}
                  />
                  <Button
                    color="error"
                    disabled={busy || !targetId.trim()}
                    onClick={() => {
                      setPending({ kind: 'delete_scope', scope, targetId: targetId.trim() });
                      setConfirmOpen(true);
                    }}
                    variant="outlined"
                  >
                    Delete
                  </Button>
                </Stack>
              </Stack>
            </Disclosure>
          </>
        )}
      </Stack>

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>
          {pending?.kind === 'delete_all' ? 'Delete all messages?'
            : pending?.kind === 'delete_scope' ? SCOPE_TITLES[pending.scope]
              : 'Turn off the archive?'}
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            {pending?.kind === 'disable'
              ? 'Older messages kept only by the archive will be removed from this computer.'
              : pending?.kind === 'delete_all'
                ? 'Every stored message for this account is removed from this computer, and your numbers are updated without them. This can’t be undone.'
                : 'It’s removed from this computer, and your numbers are updated without it. This can’t be undone.'}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button color="error" onClick={confirm} variant="contained">
            {pending?.kind === 'delete_all' ? 'Delete all' : pending?.kind === 'disable' ? 'Turn off' : 'Delete'}
          </Button>
        </DialogActions>
      </Dialog>
    </Panel>
  );
}
