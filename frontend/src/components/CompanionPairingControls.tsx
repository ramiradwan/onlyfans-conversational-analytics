import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, FormControlLabel, Stack, Typography, useMediaQuery,
} from '@mui/material';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { BrowserExtensionControls } from './BrowserExtensionControls';
import { Panel, SectionHeader, SettingRow, useRevealHold, type SectionStatus } from './ui';
import { OnboardingStatus } from './ui/OnboardingCard';
import { ReservedRegion } from './ui/ReservedRegion';
import { usePermissions } from '../hooks/usePermissions';
import { browserControlApi, type BrowserControlApi } from '../services/browserControlApi';
import {
  companionPairingApi,
  type CompanionPairingAction,
  type CompanionPairingApi,
  type CompanionPairingStatus,
} from '../services/companionPairingApi';
import { desktopOnboardingApi, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import { bridgeTransportStore } from '../store/transportStore';
import {
  extensionConnection,
  extensionLabel,
  type ExtensionConnection,
} from '../utils/statusCopy';

const WINDOW_LIMIT_MS = 300_000;
const PAIRING_EXPIRED = 'Time ran out before the connection finished.';
const terminal = (status: CompanionPairingStatus) => (
  ['confirmed', 'admitted', 'declined', 'cancelled', 'expired', 'revoked'].includes(status.state)
);
function remainingLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60).padStart(2, '0');
  return `${minutes}:${remainder}`;
}

interface PairingListFeedback {
  failed: boolean;
  busy: boolean;
  retry: () => void;
}

function PairingAttemptControls({ api, browserApi, onboarding, connection, creatorAccountId, embedded }: {
  api: CompanionPairingApi;
  browserApi: BrowserControlApi;
  onboarding: DesktopOnboardingApi;
  connection: ExtensionConnection;
  creatorAccountId: string;
  embedded: boolean;
}) {
  const narrowDialog = useMediaQuery(theme => theme.breakpoints.down('sm'));
  const [status, setStatus] = useState<CompanionPairingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [codesMatch, setCodesMatch] = useState(false);
  const [connectedCount, setConnectedCount] = useState<number | null>(null);
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null);
  const [manualRecovery, setManualRecovery] = useState(false);
  const [openingWindow, setOpeningWindow] = useState(false);
  const expectedAccount = useRef<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);
  const [browserFeedback, setBrowserFeedback] = useState<string | null>(null);
  const [listFeedback, setListFeedback] = useState<PairingListFeedback | null>(null);
  const [showFeedback, setShowFeedback] = useState(false);
  const lastKnownCount = useRef<number | null>(null);
  const updateCount = useCallback((count: number | null) => {
    if (count !== null) lastKnownCount.current = count;
    setConnectedCount(count);
  }, []);
  const openingRef = useRef(false);
  const current = useRef<CompanionPairingStatus | null>(null);
  const deadline = useRef(0);
  const operation = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const confirmationInFlight = useRef(false);
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const pendingPairing = desktop.snapshot?.pending_operations.some(value => value.action === 'browser.pair') === true;
  const canStart = desktop.connected && (pendingPairing || desktop.snapshot?.allowed_actions.includes('browser.pair'));
  const notice = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    () => bridgeTransportStore.getState().companion,
    () => bridgeTransportStore.getState().companion,
  );
  const browser = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
  );

  useEffect(() => () => {
    epoch.current += 1;
    // The owner may advance before the confirmation response arrives.
    // Neither an unresolved confirmation nor an admitted connection may be cancelled.
    const pending = current.current;
    if (confirmationInFlight.current || pending?.state === 'confirmed' || pending?.state === 'admitted') return;
    operation.current?.abort();
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
    if (confirmationInFlight.current) return false;
    const previous = current.current;
    if (action !== 'open' && previous === null) return;
    if (action === 'confirm' && (!codesMatch || busy || failed || status !== previous
      || previous?.state !== 'awaiting_confirmation' || Date.now() >= deadline.current)) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    const version = ++epoch.current;
    if (action === 'confirm') confirmationInFlight.current = true;
    setBusy(true);
    if (action === 'get') setRechecking(true);
    else setFailed(false);
    setCodesMatch(false);
    if (action === 'open') {
      const owner = onboarding.getState();
      const label = owner.connected ? owner.snapshot?.creator_label : null;
      expectedAccount.current = label?.creator_id === creatorAccountId ? label.display_name ?? label.username : null;
      current.current = null;
      setStatus(null);
      deadline.current = Date.now() + WINDOW_LIMIT_MS;
      setOpeningWindow(true);
    }
    try {
      const next = action === 'open'
        ? await api.open(creatorAccountId, controller.signal)
        : action === 'get'
          ? await api.get(previous!.pairing_id, controller.signal)
          : await api.change(previous!.pairing_id, action, previous!.version, controller.signal);
      if (controller.signal.aborted || epoch.current !== version) return false;
      acceptStatus(next);
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      setFailed(false);
      if (terminal(next)) setManualRecovery(false);
      return true;
    } catch {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
      return false;
    } finally {
      if (action === 'confirm') confirmationInFlight.current = false;
      if (!controller.signal.aborted && epoch.current === version) {
        setBusy(false);
        setRechecking(false);
        if (action === 'open') setOpeningWindow(false);
      }
    }
  };

  const connect = async () => {
    if (openingRef.current || !canStart) return;
    openingRef.current = true;
    setOpening(true);
    setEntryError(null);
    try { await onboarding.begin('browser.pair'); }
    catch { setEntryError('The browser connection request could not be opened.'); }
    finally { openingRef.current = false; setOpening(false); }
  };

  // Brain pushes a change notice; re-read the attempt only then, never on a timer.
  useEffect(() => {
    const pending = current.current;
    if (!notice || !pending || terminal(pending) || busy || failed) return;
    const controller = new AbortController();
    // The window deadline aborts this read like any other pending operation.
    operation.current = controller;
    const version = epoch.current;
    void api.get(pending.pairing_id, controller.signal).then((next) => {
      if (controller.signal.aborted || epoch.current !== version) return;
      acceptStatus(next);
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      if (terminal(next)) setManualRecovery(false);
    }).catch(() => {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
    });
    return () => controller.abort();
    // acceptStatus is recreated each render but only touches refs and setters.
  }, [api, notice]);

  useEffect(() => {
    if (!openingWindow && (!status || terminal(status))) return;
    const remaining = deadline.current - Date.now();
    const expire = () => {
      epoch.current += 1;
      operation.current?.abort();
      const pending = current.current;
      if (pending && !terminal(pending)) {
        const expired = { ...pending, state: 'expired' as const };
        current.current = expired;
        setStatus(expired);
      } else if (!pending) setEntryError(PAIRING_EXPIRED);
      setCodesMatch(false);
      setBusy(false);
      setFailed(false);
      setRechecking(false);
      setOpeningWindow(false);
      setManualRecovery(false);
    };
    const expiryTimer = setTimeout(expire, Math.max(0, remaining));
    return () => clearTimeout(expiryTimer);
  }, [status, openingWindow]);

  const approved = status?.state === 'confirmed' || status?.state === 'admitted';
  const active = status !== null && !terminal(status);
  const awaiting = active && status.state === 'awaiting_confirmation';
  const starting = active || openingWindow || pendingPairing || opening;
  const connected = approved || (connectedCount !== null && connectedCount > 0);

  useEffect(() => {
    if (!active && !openingWindow) {
      setRemainingSeconds(null);
      return;
    }
    const update = () => {
      setRemainingSeconds(Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000)));
    };
    update();
    // A local countdown for display; it never reads remote state.
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [active, openingWindow, status?.pairing_id, status?.expires_at]);

  // The chip names the most important current fact: a link problem, then what
  // the extension itself reports, then a healthy link. A running attempt reads
  // as connecting rather than as not connected.
  const sectionStatus: SectionStatus | null = starting
    ? { label: 'Connecting', tone: 'info' }
    : connected
      ? connection !== 'connected'
        ? { label: extensionLabel(connection), tone: 'warning' }
        : browser && (browser.legal_review_required || browser.site_access !== 'granted')
          ? { label: 'Needs attention', tone: 'warning' }
          : browser?.capture === 'paused'
            ? { label: 'Paused', tone: 'default' }
            : { label: extensionLabel(connection), tone: 'success' }
      : connectedCount === null
        ? null
        : { label: 'Not connected', tone: 'default' };

  const attemptFeedback = approved ? connection === 'connected' ? 'Extension connected.' : 'Connection approved. Waiting for the extension.'
      : status && terminal(status) ? status.state === 'expired' ? PAIRING_EXPIRED
          : status.state === 'revoked' ? 'This browser extension was disconnected.'
            : status.state === 'declined' ? 'Connection declined.'
              : 'Connection cancelled.' : starting && !manualRecovery ? 'Waiting for the extension…' : null;
  const feedback = [...new Set([
    browserFeedback,
    listFeedback?.failed ? "Connected extensions couldn't be checked." : null,
    entryError,
    failed && !manualRecovery ? 'Dipsy can’t check the connection right now.' : null,
  ].filter((text): text is string => text !== null))].join(' ') || null;
  const canCompare = !connected && !pendingPairing && (!canStart || Boolean(entryError));
  const showOptionsInline = canCompare && (lastKnownCount.current === 0
    || lastKnownCount.current === null && !listFeedback?.failed);
  const openOptions = () => {
    setFailed(false);
    setManualRecovery(true);
  };
  const optionsAction = <Button aria-label="Connection options" onClick={openOptions}>Options</Button>;
  useEffect(() => {
    if (!feedback) setShowFeedback(false);
  }, [feedback]);
  const expiration = remainingSeconds === null ? null : (
    <Typography variant="body2" sx={{ color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}>
      {awaiting ? 'Code expires in' : 'This attempt expires in'} {remainingLabel(remainingSeconds)}.
    </Typography>
  );

  return (
    <Stack
      data-journey-state="desktop.extension_pairing"
      data-pairing-active={starting ? 'true' : undefined}
      data-pairing-path={active ? 'operator-compared' : 'desktop-task'}
      spacing={1.5}
    >
      {!embedded && <SectionHeader
        sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'minmax(0, 1fr) 8rem' }, gap: { xs: 0, sm: 2 },
          '& > [aria-live]': { display: 'flex', width: '100%', minHeight: '1.5rem', margin: '0 !important',
            '& .MuiChip-root': { maxWidth: '100%', height: 'auto', minHeight: '1.5rem' },
            '& .MuiChip-label': { whiteSpace: 'normal', overflowWrap: 'anywhere' } } }}
        status={sectionStatus}
        title="Browser extension"
        summary={!connected && lastKnownCount.current === 0 ? 'Connect the extension to this app' : undefined}
      />}
      {embedded && sectionStatus && <Typography role="status">{sectionStatus.label}</Typography>}
      <AdmittedPairings
        api={api}
        browserApi={browserApi}
        connection={connection}
        creatorAccountId={creatorAccountId}
        onCount={updateCount}
        onBrowserFeedback={setBrowserFeedback}
        onFeedback={setListFeedback}
        refresh={`${status?.version ?? -1}:${notice?.revision ?? -1}:${notice?.changed_at ?? ''}`}
      />
      <Box data-browser-feedback sx={{ minWidth: 0, mt: '0 !important' }}>
        {(canStart || showOptionsInline) && (
          <Stack data-pairing-actions direction="row" spacing={1} useFlexGap
            sx={{ flexWrap: 'wrap', alignItems: 'flex-start', pt: 1,
              '& > button': { maxWidth: '100%', overflowWrap: 'anywhere' } }}>
            {canStart && <Button disabled={opening || manualRecovery} onClick={() => void connect()}
              variant={connected && !entryError ? 'outlined' : 'contained'}>
              {pendingPairing ? 'Continue connecting' : connected && !entryError ? 'Connect another extension' : 'Connect extension'}
            </Button>}
            {showOptionsInline && optionsAction}
          </Stack>
        )}
        <Stack data-reserved-region="browser-feedback" direction="row" spacing={1} useFlexGap aria-live="polite"
          sx={{ minHeight: '1.25rem', flexWrap: 'wrap', alignItems: 'flex-start' }}>
          {attemptFeedback && <Typography data-region-content variant="body2" role="status"
            sx={{ color: 'text.secondary', lineHeight: '1.25rem', overflowWrap: 'anywhere' }}>{attemptFeedback}</Typography>}
          {feedback && <Button data-region-content size="small" onClick={() => setShowFeedback(true)}
            sx={{ minWidth: 0, minHeight: '1.25rem', p: 0, lineHeight: '1.25rem', color: 'text.secondary' }}>Browser alert</Button>}
        </Stack>
      </Box>
      <Dialog open={showFeedback && Boolean(feedback)} onClose={() => setShowFeedback(false)} aria-labelledby="browser-alert-title">
        <DialogTitle id="browser-alert-title">Browser alert</DialogTitle>
        <DialogContent><DialogContentText role="alert">{feedback}</DialogContentText></DialogContent>
        <DialogActions sx={{ flexWrap: 'wrap', gap: 0.5 }} disableSpacing>
          {canCompare && !showOptionsInline && optionsAction}
          {listFeedback?.failed && <Button aria-label="Retry checking connected extensions" disabled={listFeedback.busy}
            onClick={listFeedback.retry}>Check list</Button>}
          <Button onClick={() => setShowFeedback(false)}>Close</Button>
        </DialogActions>
      </Dialog>
      <Dialog open={manualRecovery} onClose={() => {
        if (active && !busy) void run('cancel');
        else if (!busy) setManualRecovery(false);
      }} aria-labelledby="pairing-options-title" fullWidth maxWidth="sm" scroll={narrowDialog ? 'body' : 'paper'}
        slotProps={{ paper: { sx: { m: { xs: 1, sm: 4 }, width: { xs: 'calc(100% - 16px)' },
          maxWidth: { xs: 'calc(100% - 16px)', sm: 'sm' } } } }}>
        <DialogTitle id="pairing-options-title" sx={{ overflowWrap: 'anywhere' }}>
          {failed ? 'Extension connection' : awaiting ? 'Confirm the connection' : active || openingWindow
            ? 'Connect the extension to this app' : 'Connect another Chrome profile'}
        </DialogTitle>
        <DialogContent>
          {!active && !openingWindow && !failed && <DialogContentText>
            Use code comparison when this app cannot reach the Chrome profile containing your extension.
          </DialogContentText>}
          {awaiting && <Stack spacing={1} useFlexGap>
            <DialogContentText variant="body2">
              Compare this with the extension’s setup tab. Confirm here only if both codes match.
            </DialogContentText>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>Desktop code</Typography>
            <Typography aria-label={failed ? undefined : 'Connection comparison code'} variant="h4"
              sx={(theme) => ({ fontFamily: theme.brandTypography.displayNumeric, fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' })}>
              {failed ? '••• •••' : `${status.comparison_code!.slice(0, 3)} ${status.comparison_code!.slice(3)}`}
            </Typography>
            {expiration}
            {expectedAccount.current && !failed && <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
              OnlyFans account: {expectedAccount.current}
            </Typography>}
            <FormControlLabel control={<Checkbox checked={codesMatch} disabled={busy || failed}
              onChange={(event) => setCodesMatch(event.target.checked)} />} label="The codes match" />
          </Stack>}
          {!awaiting && (active || openingWindow) && expiration}
          {failed && !rechecking ? <Typography variant="body2" role="alert" sx={{ color: 'error.main' }}>
            Dipsy can’t check the connection right now.
          </Typography> : (rechecking || (active || openingWindow) && !awaiting) && (
            <Box sx={{ '& .onboarding-status:not(:empty)': { color: 'text.secondary', overflowWrap: 'anywhere' } }}>
              <OnboardingStatus tone="pending">{rechecking || openingWindow ? 'Checking the connection…' : 'Waiting for the extension…'}</OnboardingStatus>
            </Box>
          )}
        </DialogContent>
        <DialogActions sx={{ flexWrap: 'wrap', gap: 0.5, '& > button': { maxWidth: '100%', overflowWrap: 'anywhere' } }} disableSpacing>
          <Button disabled={busy} onClick={() => active ? void run('cancel') : setManualRecovery(false)}>
            {active ? 'Cancel' : 'Close'}
          </Button>
          {failed && active && <Button disabled={busy} onClick={() => void run('get')} variant="outlined">
            Check connection
          </Button>}
          {awaiting && <>
            <Button disabled={busy || failed} onClick={() => void run('decline')}>Codes don’t match</Button>
            <Button disabled={busy || failed || !codesMatch} onClick={() => void run('confirm')} variant="contained">
              Confirm connection
            </Button>
          </>}
          {!active && !openingWindow && !failed && <Button disabled={busy || !desktop.connected}
            onClick={() => { setEntryError(null); void run('open'); }}>
            Compare connection codes
          </Button>}
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

function AdmittedPairings({ api, browserApi, connection, creatorAccountId, onCount, onBrowserFeedback, onFeedback, refresh }: {
  api: CompanionPairingApi;
  browserApi: BrowserControlApi;
  connection: ExtensionConnection;
  creatorAccountId: string;
  onCount: (count: number | null) => void;
  onBrowserFeedback: (text: string | null) => void;
  onFeedback: (feedback: PairingListFeedback | null) => void;
  refresh: string;
}) {
  const [pins, setPins] = useState<CompanionPairingStatus[]>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [confirming, setConfirming] = useState<CompanionPairingStatus | null>(null);
  const [checked, setChecked] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const { isCreator } = usePermissions();
  const retry = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => { onFeedback({ failed, busy, retry }); }, [failed, busy, retry, onFeedback]);
  useEffect(() => () => onFeedback(null), [onFeedback]);
  const browser = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
  );
  useRevealHold(!checked);
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
    }).finally(() => {
      if (controller.signal.aborted) return;
      setBusy(false);
      setChecked(true);
    });
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
  return (
    <>
      {pins.length > 0 && <Stack spacing={0.75}>
          <ReservedRegion grow id="browser-facts" size={{ xs: 0, sm: 0 }}><BrowserExtensionControls
            api={browserApi}
            creatorAccountId={creatorAccountId}
            browser={browser}
            canManage={isCreator}
            connection={connection}
            onFeedback={onBrowserFeedback}
          /></ReservedRegion>
          <Box data-reading-viewport data-region-role="scroll" sx={{ maxHeight: '8rem', overflowY: 'auto', scrollbarGutter: 'stable' }}>{pins.map((pin, index) => (
            <SettingRow
              key={pin.pairing_id}
              title={pins.length === 1 ? 'Extension linked to this app' : `Linked extension ${index + 1}`}
              action={(
                <Button
                  aria-label={`Disconnect browser extension ${index + 1}`}
                  disabled={busy}
                  onClick={() => setConfirming(pin)}
                  size="small"
                  sx={{ color: 'text.secondary' }}
                >
                  Disconnect
                </Button>
              )}
            />
          ))}</Box>
      </Stack>}
      <Dialog open={confirming !== null} onClose={() => setConfirming(null)}>
        <DialogTitle>Disconnect the browser extension?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            New messages stop reaching this app until you connect it again.
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
    </>
  );
}

/** Browser extension section of Settings: connection status, pairing, and disconnect. */
export function CompanionPairingControls({ api = companionPairingApi, browserApi = browserControlApi, onboarding = desktopOnboardingApi, embedded = false }: {
  api?: CompanionPairingApi;
  browserApi?: BrowserControlApi;
  onboarding?: DesktopOnboardingApi;
  embedded?: boolean;
}) {
  const { canViewSettings } = usePermissions();
  const { agent, creatorAccountId, connection } = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  return (
    <Panel emphasis={embedded ? 'quiet' : 'secondary'} sx={embedded ? {
      p: 0,
      '& [data-reserved-region]': { minBlockSize: 0, minHeight: 0 },
      '& [data-reading-viewport]': { height: 'auto' },
    } : undefined}>
      {canViewSettings && creatorAccountId ? (
        <PairingAttemptControls
          key={creatorAccountId}
          api={api}
          browserApi={browserApi}
          onboarding={onboarding}
          connection={extensionConnection(connection === 'connected' ? agent : null)}
          creatorAccountId={creatorAccountId}
          embedded={embedded}
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
