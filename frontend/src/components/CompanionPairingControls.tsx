import {
  Alert, Button, Checkbox, FormControlLabel, Stack, Typography,
} from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Panel } from './ui';
import { usePermissions } from '../hooks/usePermissions';
import {
  companionPairingApi,
  type CompanionPairingAction,
  type CompanionPairingApi,
  type CompanionPairingStatus,
} from '../services/companionPairingApi';
import { bridgeTransportStore } from '../store/transportStore';

const WINDOW_LIMIT_MS = 300_000;
const POLL_INTERVAL_MS = 1_000;
const terminal = (status: CompanionPairingStatus) => (
  ['confirmed', 'admitted', 'declined', 'cancelled', 'expired', 'revoked'].includes(status.state)
);

function PairingAttemptControls({ api, creatorAccountId }: {
  api: CompanionPairingApi;
  creatorAccountId: string;
}) {
  const [status, setStatus] = useState<CompanionPairingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [codesMatch, setCodesMatch] = useState(false);
  const current = useRef<CompanionPairingStatus | null>(null);
  const deadline = useRef(0);
  const operation = useRef<AbortController | null>(null);
  const epoch = useRef(0);

  useEffect(() => () => {
    epoch.current += 1;
    operation.current?.abort();
    const pending = current.current;
    if (pending && !terminal(pending)) {
      // Best effort only: the server deadline remains authoritative if navigation interrupts this.
      void api.change(pending.pairing_id, 'cancel', pending.version).catch(() => undefined);
    }
  }, [api]);

  const acceptStatus = (next: CompanionPairingStatus) => {
    const previous = current.current;
    if (next.creator_account_id !== creatorAccountId || (previous && (
      next.pairing_id !== previous.pairing_id || next.generation !== previous.generation
      || next.version < previous.version
    ))) {
      throw new Error('Pairing state changed.');
    }
    if (!previous || previous.version !== next.version || previous.state !== next.state
      || previous.comparison_code !== next.comparison_code
      || previous.agent_identity_thumbprint !== next.agent_identity_thumbprint) {
      setCodesMatch(false);
    }
    current.current = next;
    setStatus(next);
  };

  const run = async (action: 'open' | 'get' | CompanionPairingAction) => {
    const previous = current.current;
    if (action !== 'open' && previous === null) return;
    if (action === 'confirm' && (!codesMatch || previous?.state !== 'awaiting_confirmation')) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    const version = ++epoch.current;
    setBusy(true);
    setFailed(false);
    setCodesMatch(false);
    if (action === 'open') {
      current.current = null;
      setStatus(null);
      deadline.current = Date.now() + WINDOW_LIMIT_MS;
    }
    try {
      const next = action === 'open'
        ? await api.open(creatorAccountId, controller.signal)
        : action === 'get'
          ? await api.get(previous!.pairing_id, controller.signal)
          : await api.change(previous!.pairing_id, action, previous!.version, controller.signal);
      if (controller.signal.aborted || epoch.current !== version) return;
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      acceptStatus(next);
    } catch {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
    } finally {
      if (!controller.signal.aborted && epoch.current === version) setBusy(false);
    }
  };

  useEffect(() => {
    if (!status || terminal(status)) return;
    const remaining = deadline.current - Date.now();
    const expire = () => {
      epoch.current += 1;
      operation.current?.abort();
      const pending = current.current;
      if (!pending || terminal(pending)) return;
      const expired = { ...pending, state: 'expired' as const };
      current.current = expired;
      setStatus(expired);
      setCodesMatch(false);
      setBusy(false);
      setFailed(false);
    };
    const expiryTimer = setTimeout(expire, Math.max(0, remaining));
    return () => clearTimeout(expiryTimer);
  }, [status]);

  useEffect(() => {
    if (!status || terminal(status) || busy || failed) return;
    const controller = new AbortController();
    const version = epoch.current;
    const timer = setTimeout(() => {
      void api.get(status.pairing_id, controller.signal).then((next) => {
        if (controller.signal.aborted || epoch.current !== version) return;
        deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
        acceptStatus(next);
      }).catch(() => {
        if (!controller.signal.aborted && epoch.current === version) setFailed(true);
      });
    }, POLL_INTERVAL_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, status, busy, failed]);

  const approved = status?.state === 'confirmed' || status?.state === 'admitted';
  const active = status !== null && !terminal(status);
  const awaiting = active && !failed && status.state === 'awaiting_confirmation';

  return (
    <Stack spacing={2}>
      <Typography variant="body2">Creator account: {creatorAccountId}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Open a pairing window, then start pairing from the extension.
        Keep both views open and compare their codes. Preview works without pairing.
      </Typography>
      {failed && (
        <Alert severity="error" role="alert">
          Pairing status could not be verified. Check its status before continuing.
        </Alert>
      )}
      {approved && (
        <Alert severity="success" role="status">
          Local approval is complete. Full mode becomes available only after the extension
          establishes its encrypted companion session.
        </Alert>
      )}
      {status && terminal(status) && !approved && (
        <Alert severity="info" role="status">
          {status.state === 'expired' ? 'The pairing window expired.'
            : status.state === 'revoked' ? 'This pairing was revoked.'
              : status.state === 'declined' ? 'Pairing was declined.' : 'Pairing was cancelled.'}
        </Alert>
      )}
      {active && !awaiting && !failed && (
        <Typography role="status">Waiting for the extension to verify this companion.</Typography>
      )}
      {awaiting && (
        <Stack spacing={1.5}>
          <Typography component="h3" variant="subtitle1">Compare the pairing codes</Typography>
          <Typography aria-label="Pairing comparison code" variant="h4" sx={{ fontFamily: 'monospace' }}>
            {status.comparison_code!.slice(0, 3)} {status.comparison_code!.slice(3)}
          </Typography>
          <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
            Extension identity: {status.agent_identity_thumbprint}
          </Typography>
          <FormControlLabel
            control={(
              <Checkbox
                checked={codesMatch}
                disabled={busy}
                onChange={(event) => setCodesMatch(event.target.checked)}
              />
            )}
            label="Both views show the same code and creator account."
          />
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <Button
              disabled={busy || !codesMatch}
              onClick={() => void run('confirm')}
              variant="contained"
            >
              Confirm pairing
            </Button>
            <Button color="error" disabled={busy} onClick={() => void run('decline')}>
              Codes do not match
            </Button>
          </Stack>
        </Stack>
      )}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
        {!active && (
          <Button disabled={busy} onClick={() => void run('open')} variant="outlined">
            Open pairing window
          </Button>
        )}
        {active && failed && (
          <Button disabled={busy} onClick={() => void run('get')} variant="outlined">
            Check pairing status
          </Button>
        )}
        {active && (
          <Button disabled={busy} onClick={() => void run('cancel')}>
            Cancel pairing
          </Button>
        )}
      </Stack>
    </Stack>
  );
}

export function CompanionPairingControls({ api = companionPairingApi }: { api?: CompanionPairingApi }) {
  const { canViewSettings } = usePermissions();
  const { creatorAccountId } = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  return (
    <Panel>
      <Typography component="h2" variant="h6">Pair extension</Typography>
      {canViewSettings && creatorAccountId ? (
        <PairingAttemptControls key={creatorAccountId} api={api} creatorAccountId={creatorAccountId} />
      ) : (
        <Alert severity="info">Sign in to an approved creator account to pair the extension.</Alert>
      )}
    </Panel>
  );
}
