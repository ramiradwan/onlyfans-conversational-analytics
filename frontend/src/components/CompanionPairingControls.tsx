import {
  Alert, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, FormControlLabel, Stack, Typography,
} from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader, type SectionStatus } from './ui';
import { usePermissions } from '../hooks/usePermissions';
import {
  companionPairingApi,
  type CompanionPairingAction,
  type CompanionPairingApi,
  type CompanionPairingStatus,
} from '../services/companionPairingApi';
import { bridgeTransportStore } from '../store/transportStore';
import {
  extensionConnection,
  extensionIssue,
  extensionLabel,
  type ExtensionConnection,
} from '../utils/statusCopy';

const WINDOW_LIMIT_MS = 300_000;
const POLL_INTERVAL_MS = 1_000;
const terminal = (status: CompanionPairingStatus) => (
  ['confirmed', 'admitted', 'declined', 'cancelled', 'expired', 'revoked'].includes(status.state)
);

function PairingAttemptControls({ api, connection, creatorAccountId }: {
  api: CompanionPairingApi;
  connection: ExtensionConnection;
  creatorAccountId: string;
}) {
  const [status, setStatus] = useState<CompanionPairingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [codesMatch, setCodesMatch] = useState(false);
  const [connectedCount, setConnectedCount] = useState<number | null>(null);
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
      throw new Error('Connection state changed.');
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
  const connected = connectedCount !== null && connectedCount > 0;
  const issue = extensionIssue(connection);
  const sectionStatus: SectionStatus | null = connectedCount === null
    ? null
    : connectedCount === 0
      ? { label: 'Not connected', tone: 'default' }
      : {
          label: extensionLabel(connection),
          tone: connection === 'connected' ? 'success' : issue?.severity === 'info' ? 'default' : 'warning',
        };

  return (
    <Stack spacing={2}>
      <SectionHeader
        status={sectionStatus}
        summary={connectedCount === 0 && status === null
          ? 'Connect the browser extension so your messages reach this app.'
          : undefined}
        title="Browser extension"
      />
      <AdmittedPairings
        api={api}
        connection={connection}
        creatorAccountId={creatorAccountId}
        onCount={setConnectedCount}
        refresh={status?.version ?? -1}
      />
      {failed && (
        <Alert severity="error" role="alert">
          The connection couldn&apos;t be checked. Keep this page open and try again.
        </Alert>
      )}
      {approved && (
        <Alert severity="success" role="status">
          Extension connected. Go back to the browser extension to continue.
        </Alert>
      )}
      {status && terminal(status) && !approved && (
        <Alert severity="info" role="status">
          {status.state === 'expired' ? 'Time ran out before the codes were confirmed. Try again.'
            : status.state === 'revoked' ? 'This browser extension was disconnected.'
              : status.state === 'declined' ? "The codes didn't match, so nothing was connected. Try again."
                : 'Connection cancelled.'}
        </Alert>
      )}
      {active && !awaiting && !failed && (
        <Typography role="status">
          Open the browser extension and choose Pair device. Keep this page open.
        </Typography>
      )}
      {awaiting && (
        <Stack spacing={1.5}>
          <Typography component="h3" variant="subtitle1">Check the code</Typography>
          <Typography aria-label="Connection comparison code" variant="h4" sx={{ fontFamily: 'monospace' }}>
            {status.comparison_code!.slice(0, 3)} {status.comparison_code!.slice(3)}
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            The browser extension should show the same code. If it doesn&apos;t, don&apos;t connect.
          </Typography>
          <FormControlLabel
            control={(
              <Checkbox
                checked={codesMatch}
                disabled={busy}
                onChange={(event) => setCodesMatch(event.target.checked)}
              />
            )}
            label="The codes match"
          />
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <Button
              disabled={busy || !codesMatch}
              onClick={() => void run('confirm')}
              variant="contained"
            >
              Confirm connection
            </Button>
            <Button color="error" disabled={busy} onClick={() => void run('decline')}>
              Codes don&apos;t match
            </Button>
          </Stack>
        </Stack>
      )}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: 'flex-start' }} useFlexGap>
        {!active && (
          <Button
            disabled={busy}
            onClick={() => void run('open')}
            size={connected ? 'small' : 'medium'}
            sx={connected ? { ml: -1 } : undefined}
            variant={connected ? 'text' : 'contained'}
          >
            {connected ? 'Connect another extension' : 'Connect extension'}
          </Button>
        )}
        {active && failed && (
          <Button disabled={busy} onClick={() => void run('get')} variant="outlined">
            Try again
          </Button>
        )}
        {active && (
          <Button disabled={busy} onClick={() => void run('cancel')}>
            Cancel
          </Button>
        )}
      </Stack>
    </Stack>
  );
}

function AdmittedPairings({ api, connection, creatorAccountId, onCount, refresh }: {
  api: CompanionPairingApi;
  connection: ExtensionConnection;
  creatorAccountId: string;
  onCount: (count: number | null) => void;
  refresh: number;
}) {
  const [pins, setPins] = useState<CompanionPairingStatus[]>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [confirming, setConfirming] = useState<CompanionPairingStatus | null>(null);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    void api.pins(controller.signal).then((values) => {
      if (controller.signal.aborted) return;
      if (values.some((pin) => pin.creator_account_id !== creatorAccountId || pin.state !== 'admitted')) {
        throw new Error('Connection scope changed.');
      }
      setPins(values);
      onCount(values.length);
      setFailed(false);
    }).catch(() => {
      if (controller.signal.aborted) return;
      setFailed(true);
      onCount(null);
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { controller.abort(); operation.current?.abort(); };
  }, [api, creatorAccountId, onCount, refresh, revision]);
  const revoke = async (pin: CompanionPairingStatus) => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setConfirming(null);
    try {
      await api.revoke(pin.pairing_id, pin.version, controller.signal);
      if (controller.signal.aborted) return;
      const next = pins.filter((value) => value.pairing_id !== pin.pairing_id);
      setPins(next);
      onCount(next.length);
      setFailed(false);
    } catch { if (!controller.signal.aborted) setFailed(true); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  const issue = extensionIssue(connection);
  if (pins.length === 0 && !failed && confirming === null) return null;
  return (
    <Stack spacing={1.5}>
      {pins.length > 0 && (
        <>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            {issue ? `${issue.detail} ` : ''}
            To pause, allow message history, or remove site access, open the browser extension.
          </Typography>
          {pins.map((pin, index) => (
            <Stack
              key={pin.pairing_id}
              direction="row"
              spacing={2}
              sx={{ alignItems: 'center', justifyContent: 'space-between' }}
            >
              <Typography variant="body2">
                {pins.length === 1 ? 'Connected to this app' : `Browser extension ${index + 1}`}
              </Typography>
              <Button
                aria-label={`Disconnect browser extension ${index + 1}`}
                disabled={busy}
                onClick={() => setConfirming(pin)}
                size="small"
                sx={{ color: 'text.secondary' }}
              >
                Disconnect
              </Button>
            </Stack>
          ))}
        </>
      )}
      {failed && (
        <Alert
          action={(
            <Button color="inherit" disabled={busy} onClick={() => setRevision((value) => value + 1)} size="small">
              Try again
            </Button>
          )}
          severity="error"
        >
          Connected extensions couldn&apos;t be checked.
        </Alert>
      )}
      <Dialog open={confirming !== null} onClose={() => setConfirming(null)}>
        <DialogTitle>Disconnect the browser extension?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            New messages stop reaching this app until you connect it again. Messages already here are kept.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirming(null)}>Cancel</Button>
          <Button
            color="error"
            onClick={() => { if (confirming) void revoke(confirming); }}
            variant="contained"
          >
            Disconnect
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

/** Browser extension section of Settings: connection status, pairing, and disconnect. */
export function CompanionPairingControls({ api = companionPairingApi }: { api?: CompanionPairingApi }) {
  const { canViewSettings } = usePermissions();
  const { agent, creatorAccountId } = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  return (
    <Panel>
      {canViewSettings && creatorAccountId ? (
        <PairingAttemptControls
          key={creatorAccountId}
          api={api}
          connection={extensionConnection(agent)}
          creatorAccountId={creatorAccountId}
        />
      ) : (
        <>
          <SectionHeader title="Browser extension" />
          <Alert severity="info">Finish setting up the desktop app before connecting the browser extension.</Alert>
        </>
      )}
    </Panel>
  );
}
