import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, FormControlLabel, Stack, Typography,
} from '@mui/material';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { BrowserExtensionControls } from './BrowserExtensionControls';
import { Panel, SectionHeader, SettingRow, useRevealHold, type SectionStatus } from './ui';
import { ReservedRegion, StatusLine } from './ui/ReservedRegion';
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
  const [status, setStatus] = useState<CompanionPairingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [codesMatch, setCodesMatch] = useState(false);
  const [connectedCount, setConnectedCount] = useState<number | null>(null);
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null);
  const [manualRecovery, setManualRecovery] = useState(false);
  const [opening, setOpening] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);
  const [browserFeedback, setBrowserFeedback] = useState<string | null>(null);
  const [listFeedback, setListFeedback] = useState<PairingListFeedback | null>(null);
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
    if (action === 'confirm' && (!codesMatch || previous?.state !== 'awaiting_confirmation')) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    const version = ++epoch.current;
    if (action === 'confirm') confirmationInFlight.current = true;
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
      if (controller.signal.aborted || epoch.current !== version) return false;
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      acceptStatus(next);
      return true;
    } catch {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
      return false;
    } finally {
      if (action === 'confirm') confirmationInFlight.current = false;
      if (!controller.signal.aborted && epoch.current === version) setBusy(false);
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
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      acceptStatus(next);
    }).catch(() => {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
    });
    return () => controller.abort();
    // acceptStatus is recreated each render but only touches refs and setters.
  }, [api, notice]);

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

  const approved = status?.state === 'confirmed' || status?.state === 'admitted';
  const active = status !== null && !terminal(status);
  const awaiting = active && status.state === 'awaiting_confirmation';
  const connected = approved || (connectedCount !== null && connectedCount > 0);

  useEffect(() => {
    if (!active) {
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
  }, [active, status?.pairing_id]);

  // The chip names the most important current fact: a link problem, then what
  // the extension itself reports, then a healthy link. A running attempt reads
  // as connecting rather than as not connected.
  const sectionStatus: SectionStatus | null = active || pendingPairing || opening
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
      : status && terminal(status) ? status.state === 'expired' ? 'Time ran out before the connection finished. Try again.'
          : status.state === 'revoked' ? 'This browser extension was disconnected.'
            : status.state === 'declined' ? 'Connection declined. Try again.'
              : 'Connection cancelled.' : connectedCount === 0 && status === null && !pendingPairing ? 'Connect the browser extension so your messages reach this app.' : null;
  const feedback = [...new Set([
    browserFeedback,
    listFeedback?.failed ? "Connected extensions couldn't be checked." : null,
    entryError,
    failed ? "The connection couldn't be checked. Try again." : null,
    attemptFeedback,
  ].filter((text): text is string => text !== null))].join(' ') || null;
  const feedbackFailed = Boolean(browserFeedback || listFeedback?.failed || failed || entryError);

  return (
    <Stack
      data-journey-state="desktop.extension_pairing"
      data-pairing-active={active || pendingPairing || opening ? 'true' : undefined}
      data-pairing-path={active ? 'operator-compared' : 'desktop-task'}
      spacing={1.5}
      sx={{ containerType: 'inline-size' }}
    >
      {!embedded && <SectionHeader
        sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'minmax(0, 1fr) 8rem' }, gap: { xs: 0, sm: 2 },
          '& > [aria-live]': { width: '100%', minHeight: '2rem', margin: '0 !important', '& .MuiChip-root': { width: '100%' }, '& .MuiChip-label': { width: '100%', textAlign: 'left' } } }}
        status={sectionStatus}
        title="Browser extension"
      />}
      {embedded && sectionStatus && <Typography role="status">{sectionStatus.label}</Typography>}
      <ReservedRegion grow id="pairing-body" size={{ xs: 0, sm: 0 }} sx={{ display: 'grid' }}>
      <Box sx={{ gridArea: '1 / 1', visibility: active || pendingPairing || opening ? 'hidden' : 'visible' }}><AdmittedPairings
        api={api}
        browserApi={browserApi}
        connection={connection}
        creatorAccountId={creatorAccountId}
        onCount={setConnectedCount}
        onBrowserFeedback={setBrowserFeedback}
        onFeedback={setListFeedback}
        refresh={`${status?.version ?? -1}:${notice?.revision ?? -1}:${notice?.changed_at ?? ''}`}
      /></Box>
      <Stack spacing={1.5} sx={{ gridArea: '1 / 1', pointerEvents: active || pendingPairing || opening ? 'auto' : 'none' }}>
      {(pendingPairing || opening) && <Typography role="status">Connecting the browser extension…</Typography>}
      {active && !awaiting && !failed && (
        <Stack spacing={0.5}>
          <Typography role="status">
            Waiting for the browser extension to connect.
          </Typography>
          {remainingSeconds !== null && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Connection window expires in {remainingLabel(remainingSeconds)}.
            </Typography>
          )}
        </Stack>
      )}
      {awaiting && (
        <Stack spacing={1.5}>
          <Typography component="h3" variant="subtitle1">{failed ? 'Confirmation paused' : 'Check the code'}</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Desktop code</Typography>
          <Typography
            aria-label={failed ? undefined : 'Connection comparison code'}
            variant="h4"
            sx={(theme) => ({
              fontFamily: theme.brandTypography.fontFamilyMono,
              fontSize: theme.typography.h2.fontSize,
              fontWeight: theme.brandTypography.weights.semibold,
              letterSpacing: '0.08em',
              lineHeight: 1.2,
              marginBlock: theme.spacing(0.5),
            })}
          >
            {failed ? '••• •••' : `${status.comparison_code!.slice(0, 3)} ${status.comparison_code!.slice(3)}`}
          </Typography>
          {remainingSeconds !== null && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Code expires in {remainingLabel(remainingSeconds)}.
            </Typography>
          )}
          <Typography variant="body2" sx={{ color: 'text.secondary', display: 'grid' }}>
            <Box component="span" aria-hidden={failed} sx={{ gridArea: '1 / 1', visibility: failed ? 'hidden' : 'visible' }}>
              Compare this with the extension’s setup tab. Confirm here only if both codes match.
            </Box>
            {failed && <Box component="span" sx={{ gridArea: '1 / 1' }}>Try again to check this connection.</Box>}
          </Typography>
          <FormControlLabel
            control={(
              <Checkbox
                checked={codesMatch}
                disabled={busy || failed}
                onChange={(event) => setCodesMatch(event.target.checked)}
              />
            )}
            label="The codes match"
          />
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <Button
              disabled={busy || failed || !codesMatch}
              onClick={() => void run('confirm')}
              variant="contained"
            >
              Confirm connection
            </Button>
            <Button color="error" disabled={busy || failed} onClick={() => void run('decline')}>
              Codes don&apos;t match
            </Button>
          </Stack>
        </Stack>
      )}
      </Stack>
      </ReservedRegion>
      <Box data-browser-feedback sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 18rem', columnGap: 1, minWidth: 0, alignItems: 'start',
        '@container (max-width: 26rem)': { gridTemplateColumns: 'minmax(0, 1fr)' },
        '& [data-reserved-region="browser-feedback"]': { blockSize: '2.5rem' } }}>
        <Box sx={{ minWidth: 0 }}><StatusLine id="browser-feedback" text={feedback}
          summary={feedbackFailed ? 'Browser alert' : 'Browser status'} tone={feedbackFailed ? 'error' : 'secondary'} /></Box>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 4.5rem', columnGap: 1, minWidth: 0,
          '@container (max-width: 26rem)': { gridTemplateColumns: 'minmax(0, 1fr)' },
          '& button': { minWidth: 0, maxWidth: '100%', minHeight: '2.5rem', py: 0, px: 1, lineHeight: '1.25rem', overflowWrap: 'anywhere' } }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start', minWidth: 0, minHeight: '2.5rem',
          '@container (max-width: 26rem)': { flexDirection: 'column', minHeight: active || opening || entryError ? '5.5rem' : '2.5rem' },
          '& > button': { justifyContent: 'flex-start' } }} useFlexGap>
        {!active && canStart && <Button disabled={opening} onClick={() => void connect()}
          variant={connected ? 'text' : 'contained'}>
          {pendingPairing ? 'Continue connecting' : connected ? 'Connect another extension' : 'Connect extension'}
        </Button>}
        {!connected && !active && !pendingPairing && (!canStart || entryError) && <Button aria-label="Connection options" title="Connection options" onClick={() => setManualRecovery(true)} variant="text" sx={{ whiteSpace: 'nowrap', flexShrink: 0 }}>
          Options
        </Button>}
        {active && <Button disabled={busy} onClick={() => void run('cancel')}>Cancel</Button>}
        {active && failed && (
          <Button disabled={busy} onClick={() => void run('get')} variant="outlined">
            Try again
          </Button>
        )}
        </Stack>
        <Button aria-label="Retry checking connected extensions" size="small" color="inherit"
          sx={{ width: '4.5rem', justifySelf: 'start', visibility: listFeedback?.failed ? 'visible' : 'hidden' }}
          disabled={listFeedback?.busy ?? false} onClick={() => listFeedback?.retry()}>Try again</Button>
        </Box>
      </Box>
      <Dialog open={manualRecovery} onClose={() => setManualRecovery(false)} aria-labelledby="pairing-options-title">
        <DialogTitle id="pairing-options-title">Connect another Chrome profile</DialogTitle>
        <DialogContent><DialogContentText>
          Use code comparison when this app cannot reach the Chrome profile containing your extension.
        </DialogContentText></DialogContent>
        <DialogActions><Button onClick={() => setManualRecovery(false)}>Close</Button>
          <Button disabled={busy || !desktop.connected} onClick={() => { setManualRecovery(false); setEntryError(null); void run('open'); }}>
            Compare connection codes
          </Button></DialogActions>
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
    <Stack spacing={0.75}>
          {pins.length > 0 && <ReservedRegion grow id="browser-facts" size={{ xs: 0, sm: 0 }} sx={{ visibility: pins.length ? 'visible' : 'hidden' }}><BrowserExtensionControls
            api={browserApi}
            browser={browser}
            canManage={isCreator}
            connection={connection}
            onFeedback={onBrowserFeedback}
          /></ReservedRegion>}
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
    </Stack>
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
