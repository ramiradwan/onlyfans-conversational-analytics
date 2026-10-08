import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, FormControlLabel, Stack, Typography,
} from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

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
import {
  defaultExtensionPort,
  type ExtensionPort,
  type ExtensionStage,
} from '../services/extensionPort';
import { onboardingView, subscribeOnboarding } from '../services/onboardingSession';
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
// Extension-side steps that must finish before this browser can pair.
const EXTENSION_SETUP_STAGES: ReadonlySet<ExtensionStage> = new Set([
  'needs_terms', 'paused', 'needs_full', 'needs_site_access', 'needs_account',
]);
const EXTENSION_STAGE_COPY: Partial<Record<ExtensionStage, string>> = {
  needs_terms: 'Review the terms in the extension window.',
  paused: 'Resume analytics in the extension window.',
  needs_full: 'Turn on Full analytics in the extension window.',
  needs_site_access: 'Allow site access in the extension window.',
  needs_account: 'Sign in to your creator account on OnlyFans in this browser.',
};

function remainingLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60).padStart(2, '0');
  return `${minutes}:${remainder}`;
}

function PairingAttemptControls({ api, browserApi, port, connection, creatorAccountId }: {
  api: CompanionPairingApi;
  browserApi: BrowserControlApi;
  port: ExtensionPort;
  connection: ExtensionConnection;
  creatorAccountId: string;
}) {
  const [status, setStatus] = useState<CompanionPairingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [codesMatch, setCodesMatch] = useState(false);
  const [connectedCount, setConnectedCount] = useState<number | null>(null);
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null);
  // Same-browser flow: the extension finishes its own steps, then pairs through the port.
  const [waitingForExtension, setWaitingForExtension] = useState(false);
  const [browserPairing, setBrowserPairing] = useState(false);
  const [extensionRefused, setExtensionRefused] = useState(false);
  const current = useRef<CompanionPairingStatus | null>(null);
  const deadline = useRef(0);
  const operation = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const verifiedVersion = useRef<string | null>(null);
  const automaticAttempted = useRef(false);
  const onboarding = useSyncExternalStore(subscribeOnboarding, onboardingView, onboardingView);
  const extension = useSyncExternalStore(port.subscribe, port.getState, port.getState);
  const notice = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    () => bridgeTransportStore.getState().companion,
    () => bridgeTransportStore.getState().companion,
  );
  const sameBrowser = extension.status === 'connected';
  const browser = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
    () => bridgeTransportStore.getState().connection === 'connected' ? bridgeTransportStore.getState().agent?.browser ?? null : null,
  );

  useEffect(() => () => {
    epoch.current += 1;
    operation.current?.abort();
    const pending = current.current;
    if (pending && !terminal(pending)) {
      // Best effort only: the server deadline remains authoritative if navigation interrupts this.
      void api.change(pending.pairing_id, 'cancel', pending.version).catch(() => undefined);
    }
    port.cancel();
  }, [api, port]);

  // An extension that was absent may have been installed or enabled since.
  useEffect(() => {
    const retry = () => port.retry();
    globalThis.addEventListener?.('focus', retry);
    return () => globalThis.removeEventListener?.('focus', retry);
  }, [port]);

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

  const run = async (action: 'open' | 'get' | CompanionPairingAction | 'verified', code?: string) => {
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
          : action === 'verified'
            ? await api.confirmVerified(previous!.pairing_id, previous!.version, code!, controller.signal)
            : await api.change(previous!.pairing_id, action, previous!.version, controller.signal);
      if (controller.signal.aborted || epoch.current !== version) return false;
      deadline.current = Math.min(deadline.current, Date.parse(next.expires_at));
      acceptStatus(next);
      return true;
    } catch {
      if (!controller.signal.aborted && epoch.current === version) setFailed(true);
      return false;
    } finally {
      if (!controller.signal.aborted && epoch.current === version) setBusy(false);
    }
  };

  const startPairing = async () => {
    setExtensionRefused(false);
    verifiedVersion.current = null;
    const viaBrowser = port.getState().status === 'connected';
    setBrowserPairing(viaBrowser);
    // Brain's window opens first, so the extension never races a closed window.
    if (await run('open') && viaBrowser && !port.pair()) setBrowserPairing(false);
  };

  useEffect(() => {
    const brain = onboarding?.sources.brain;
    const observer = onboarding?.sources.extension;
    if (automaticAttempted.current || current.current || busy || failed || connectedCount !== 0
      || extension.status !== 'connected' || extension.stage !== 'ready_to_pair'
      || !brain?.certain || !observer?.certain || !brain.snapshot || !observer.snapshot
      || brain.snapshot.pending_operation || observer.snapshot.pending_operation
      || brain.snapshot.facts.installation !== 'verified' || brain.snapshot.facts.enrollment !== 'verified'
      || brain.snapshot.facts.pairing !== 'missing' || observer.snapshot.facts.mode !== 'full'
      || observer.snapshot.facts.consent !== 'valid' || observer.snapshot.facts.site_access !== 'granted'
      || observer.snapshot.facts.account === 'mismatch') return;
    // Completed Full consent and current owner facts survive navigation. The
    // new page starts a fresh attempt; it never revives a cancelled port/code.
    automaticAttempted.current = true;
    void startPairing();
  }, [onboarding, extension.status, extension.stage, connectedCount, busy, failed]);

  const connect = () => {
    const stage = port.getState().stage;
    if (sameBrowser && stage !== null && EXTENSION_SETUP_STAGES.has(stage)) {
      setWaitingForExtension(true);
      port.open('setup');
      return;
    }
    void startPairing();
  };

  // The extension pushes its stage; continue as soon as its steps are done.
  useEffect(() => {
    if (!waitingForExtension) return;
    if (extension.status !== 'connected') { setWaitingForExtension(false); return; }
    if (extension.stage === 'ready_to_pair' || extension.stage === 'paired') {
      setWaitingForExtension(false);
      void startPairing();
    }
    // startPairing reads only refs and stable callbacks.
  }, [waitingForExtension, extension.status, extension.stage]);

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

  // Same browser: confirm with the code the extension reported. Brain compares it.
  const extensionCode = browserPairing && extension.attempt?.state === 'compare'
    ? extension.attempt.comparison_code : null;
  useEffect(() => {
    if (!status || status.state !== 'awaiting_confirmation' || extensionCode === null || busy || failed) return;
    const key = `${status.pairing_id}:${status.version}`;
    if (verifiedVersion.current === key) return;
    verifiedVersion.current = key;
    void run('verified', extensionCode);
    // run is recreated each render but reads the current attempt from refs.
  }, [status, extensionCode, busy, failed]);

  // The extension ended its side of the attempt; end Brain's window too.
  const extensionAttempt = browserPairing ? extension.attempt?.state ?? null : null;
  useEffect(() => {
    if (!extensionAttempt || !['failed', 'not_ready', 'cancelled'].includes(extensionAttempt)) return;
    const pending = current.current;
    if (!pending || terminal(pending)) return;
    setExtensionRefused(true);
    void run('cancel');
  }, [extensionAttempt]);

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
  const awaiting = active && !failed && status.state === 'awaiting_confirmation';
  const connected = approved || (connectedCount !== null && connectedCount > 0);
  const verifying = active && browserPairing && !failed;

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
  const sectionStatus: SectionStatus | null = active || waitingForExtension
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

  return (
    <Stack
      data-journey-state="desktop.extension_pairing"
      data-pairing-active={active || waitingForExtension ? 'true' : undefined}
      data-pairing-path={sameBrowser ? 'browser-verified' : 'operator-compared'}
      spacing={2}
    >
      <SectionHeader
        sx={{ minHeight: '4.5rem', '& > [aria-live]': { width: '8rem', '& .MuiChip-root': { width: '100%' }, '& .MuiChip-label': { width: '100%', textAlign: 'left' } } }}
        status={sectionStatus}
        title="Browser extension"
      />
      <ReservedRegion grow id="pairing-body" size={{ xs: 672, sm: 432 }} sx={{ display: 'grid' }}>
      <Box sx={{ gridArea: '1 / 1', visibility: active || waitingForExtension ? 'hidden' : 'visible' }}><AdmittedPairings
        api={api}
        browserApi={browserApi}
        port={port}
        connection={connection}
        creatorAccountId={creatorAccountId}
        onCount={setConnectedCount}
        refresh={`${status?.version ?? -1}:${notice?.revision ?? -1}:${notice?.changed_at ?? ''}`}
      /></Box>
      <Stack spacing={1.5} sx={{ gridArea: '1 / 1', pointerEvents: active || waitingForExtension ? 'auto' : 'none' }}>
      {waitingForExtension && (
        <Stack data-journey-state="desktop.extension_handoff" spacing={0.5}>
          <Typography role="status">
            {(extension.stage && EXTENSION_STAGE_COPY[extension.stage]) ?? 'Finish setup in the extension window.'}
            {' '}This continues here automatically.
          </Typography>
        </Stack>
      )}
      {verifying && (
        <Stack spacing={0.5}>
          <Typography role="status">Connecting the extension in this browser…</Typography>
          {remainingSeconds !== null && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Keep this page open. Time left: {remainingLabel(remainingSeconds)}.
            </Typography>
          )}
        </Stack>
      )}
      {active && !browserPairing && !awaiting && !failed && (
        <Stack spacing={0.5}>
          <Typography role="status">
            Open the browser extension where it&apos;s installed, choose Continue setup, then Pair device. Keep this page open.
          </Typography>
          {remainingSeconds !== null && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Connection window expires in {remainingLabel(remainingSeconds)}.
            </Typography>
          )}
        </Stack>
      )}
      {awaiting && !browserPairing && (
        <Stack spacing={1.5}>
          <Typography component="h3" variant="subtitle1">Check the code</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Desktop code</Typography>
          <Typography
            aria-label="Connection comparison code"
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
            {status.comparison_code!.slice(0, 3)} {status.comparison_code!.slice(3)}
          </Typography>
          {remainingSeconds !== null && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Code expires in {remainingLabel(remainingSeconds)}.
            </Typography>
          )}
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            Compare this with the extension’s setup tab. Confirm here only if both codes match.
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
      </Stack>
      </ReservedRegion>
      <StatusLine essential id="pairing-result" tone={failed ? 'error' : 'secondary'} text={failed
        ? "The connection couldn't be checked. Keep this page open and try again."
        : approved ? connection === 'connected' ? 'Extension connected.' : 'Connection approved. Waiting for the extension.'
          : status && terminal(status) ? extensionRefused ? 'The extension stopped the connection. Try again.'
            : status.state === 'expired' ? 'Time ran out before the connection finished. Try again.'
              : status.state === 'revoked' ? 'This browser extension was disconnected.'
                : status.state === 'declined' ? "The codes didn't match, so nothing was connected. Try again."
                  : 'Connection cancelled.' : connectedCount === 0 && status === null && !waitingForExtension ? 'Connect the browser extension so your messages reach this app.' : null} />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: 'flex-start', minHeight: '3rem', '& > button': { minWidth: '12.5rem', height: '3rem', justifyContent: 'flex-start' } }} useFlexGap>
        {!active && !waitingForExtension && (
          <Button
            disabled={busy}
            onClick={connect}
            size="medium"
            variant={connected ? 'text' : 'contained'}
          >
            {connected ? 'Connect another extension' : 'Connect extension'}
          </Button>
        )}
        {waitingForExtension && (
          <Button onClick={() => port.open('setup')} variant="outlined">
            Show extension window
          </Button>
        )}
        {active && failed && (
          <Button disabled={busy} onClick={() => void run('get')} variant="outlined">
            Try again
          </Button>
        )}
        {(active || waitingForExtension) && (
          <Button
            disabled={busy}
            onClick={() => {
              setWaitingForExtension(false);
              port.cancel();
              if (active) void run('cancel');
            }}
          >
            Cancel
          </Button>
        )}
      </Stack>
    </Stack>
  );
}

function AdmittedPairings({ api, browserApi, port, connection, creatorAccountId, onCount, refresh }: {
  api: CompanionPairingApi;
  browserApi: BrowserControlApi;
  port: ExtensionPort;
  connection: ExtensionConnection;
  creatorAccountId: string;
  onCount: (count: number | null) => void;
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
          <ReservedRegion grow id="browser-facts" size={{ xs: 560, sm: 340 }} sx={{ visibility: pins.length ? 'visible' : 'hidden' }}><BrowserExtensionControls
            api={browserApi}
            browser={browser}
            canManage={isCreator}
            connection={connection}
            port={port}
          /></ReservedRegion>
          <Box data-reading-viewport data-region-role="scroll" sx={{ height: '3.75rem', overflowY: 'auto', scrollbarGutter: 'stable' }}>{pins.map((pin, index) => (
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
      <Box sx={{ height: '2.5rem', display: 'flex', gap: 1 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}><StatusLine id="pairing-links-feedback" tone="error" text={failed ? "Connected extensions couldn't be checked." : null} /></Box>
        <Button sx={{ visibility: failed ? 'visible' : 'hidden', alignSelf: 'flex-start' }} color="inherit" disabled={busy} onClick={() => setRevision((value) => value + 1)} size="small">Try again</Button>
      </Box>
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
export function CompanionPairingControls({ api = companionPairingApi, browserApi = browserControlApi, port }: {
  api?: CompanionPairingApi;
  browserApi?: BrowserControlApi;
  port?: ExtensionPort;
}) {
  const extensionPort = port ?? defaultExtensionPort();
  const { canViewSettings } = usePermissions();
  const { agent, creatorAccountId, connection } = useSyncExternalStore(
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
          browserApi={browserApi}
          port={extensionPort}
          connection={extensionConnection(connection === 'connected' ? agent : null)}
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
