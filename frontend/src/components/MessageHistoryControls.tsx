import PauseCircleOutlinedIcon from '@mui/icons-material/PauseCircleOutlined';
import PlayCircleOutlinedIcon from '@mui/icons-material/PlayCircleOutlined';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControlLabel,
  LinearProgress,
  Skeleton,
  Stack,
  Typography,
} from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader, useRevealHold } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import { usePermissions } from '../hooks/usePermissions';
import type { HistorySettings } from '../protocol';
import { browserControlApi, type BrowserControlApi } from '../services/browserControlApi';
import { desktopOnboardingApi, selectAnalysisReadiness, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import { focusDesktopWindow } from '../services/desktopWindow';
import {
  historySettingsApi as defaultHistorySettingsApi,
  historyFailure,
  type HistorySettingsApi,
} from '../services/historySettingsApi';
import { bridgeTransportStore, type BridgeTransportStore } from '../store/transportStore';
import { presentHistory } from '../utils/historyPresentation';
import { extensionConnection } from '../utils/statusCopy';

const NUMBER_FORMAT = new Intl.NumberFormat('en-US');

// Optional promotion is presentation state, scoped to the current account,
// journey and legal policy, not to a disposable desktop session generation.
const dismissedReviews = new WeakMap<HistorySettingsApi, Set<string>>();
function rememberDismissal(api: HistorySettingsApi, scope: string) {
  let scopes = dismissedReviews.get(api);
  if (!scopes) { scopes = new Set(); dismissedReviews.set(api, scopes); }
  scopes.add(scope);
  if (scopes.size > 128) scopes.delete(scopes.values().next().value!);
}

export interface MessageHistoryControlsProps {
  api?: HistorySettingsApi;
  browserApi?: BrowserControlApi;
  desktopApi?: DesktopOnboardingApi;
  store?: BridgeTransportStore;
  reviewCreatorId?: string;
  continuation?: boolean;
}

interface PermissionReturn {
  creatorId: string;
  journeyId: string;
  generation: string;
  expires: number;
  delivered: boolean;
}

interface HistoryChange {
  before: HistorySettings;
  desired: 'running' | 'paused' | 'revoked';
  acceptConsent: boolean;
}

const CHANGE_UNCONFIRMED = "Your change hasn't been confirmed.";

function sameEffect(change: HistoryChange, next: HistorySettings): boolean {
  const before = change.before;
  if (next.creator_account_id !== before.creator_account_id
    || next.consent_policy_version !== before.consent_policy_version
    || next.desired_state !== change.desired) return false;
  if (change.desired === 'revoked') {
    return next.consent_revision === null && next.authorized_platform_creator_id === null;
  }
  if (next.recent_window_days !== before.recent_window_days || next.page_size !== before.page_size
    || next.pages_per_wake !== before.pages_per_wake || next.request_interval_ms !== before.request_interval_ms
    || next.retry_limit !== before.retry_limit) return false;
  if (change.acceptConsent) {
    return next.consent_revision !== null && next.consent_revision !== before.consent_revision
      && next.authorized_platform_creator_id !== null
      && (before.authorized_platform_creator_id === null
        || next.authorized_platform_creator_id === before.authorized_platform_creator_id);
  }
  return next.consent_revision === before.consent_revision
    && next.authorized_platform_creator_id === before.authorized_platform_creator_id;
}

function settingsIdentity(value: HistorySettings | null): string | null {
  return value === null ? null : JSON.stringify([value.creator_account_id, value.settings_revision,
    value.consent_policy_version, value.consent_revision, value.authorized_platform_creator_id]);
}

/** Shared history consent, permission, progress and management controls. */
export function MessageHistoryControls({ api = defaultHistorySettingsApi, browserApi = browserControlApi,
  desktopApi = desktopOnboardingApi, store = bridgeTransportStore, reviewCreatorId, continuation = false }: MessageHistoryControlsProps) {
  const { canManageHistorySync } = usePermissions();
  const transport = useSyncExternalStore(
    store.subscribe,
    store.getState,
    store.getState,
  );
  const [loadedSettings, setSettings] = useState<HistorySettings | null>(null);
  const latest = useRef<HistorySettings | null>(null);
  const accountId = reviewCreatorId ?? transport.creatorAccountId;
  const settings = loadedSettings && (accountId === null || loadedSettings.creator_account_id === accountId)
    ? loadedSettings : null;
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [consentSelection, setConsentSelection] = useState<string | null>(null);
  const [revokeSelection, setRevokeSelection] = useState<string | null>(null);
  const identity = settingsIdentity(settings);
  const consentAccepted = identity !== null && consentSelection === identity;
  const confirmRevoke = identity !== null && revokeSelection === identity;
  const [pendingChange, setPendingChange] = useState<HistoryChange | null>(null);
  const [refusedIdentity, setRefusedIdentity] = useState<string | null>(null);
  const [recoveryWake, setRecoveryWake] = useState(0);
  const [openReview, setOpenReview] = useState<string | null>(null);
  const [dismissedReview, setDismissedReview] = useState<string | null>(null);
  const desktop = useSyncExternalStore(desktopApi.subscribe, desktopApi.getState, desktopApi.getState);
  const [permissionReturn, setPermissionReturn] = useState<PermissionReturn | null>(null);
  const consumedPermissionReturn = useRef<PermissionReturn | null>(null);
  const permissionRequest = useRef<AbortController | null>(null);
  const [openingPermission, setOpeningPermission] = useState(false);
  const mutation = useRef<AbortController | null>(null);
  const review = reviewCreatorId !== undefined;
  const reviewCurrent = () => {
    if (!review) return true;
    const current = store.getState();
    const owner = desktopApi.getState();
    const readiness = selectAnalysisReadiness(owner, reviewCreatorId);
    const browser = current.agent?.browser;
    return current.creatorAccountId === reviewCreatorId && current.connection === 'connected'
      && current.agent?.creator_account_id === reviewCreatorId && extensionConnection(current.agent) === 'connected'
      && owner.connected && owner.snapshot?.generation === desktop.snapshot?.generation
      && owner.snapshot?.journey_id === desktop.snapshot?.journey_id
      && readiness.availability === 'current' && readiness.value?.commercial_authority === 'active'
      && readiness.value?.analysis_admission === 'admitted'
      && browser?.capture === 'active' && browser.site_access === 'granted' && !browser.legal_review_required;
  };
  const reviewAvailable = reviewCurrent();
  const reviewGeneration = review ? desktop.snapshot?.generation : undefined;
  const reviewJourney = review ? desktop.snapshot?.journey_id : undefined;
  useEffect(() => {
    setBusy(false);
    setPendingChange(null);
    setRefusedIdentity(null);
    setRevokeSelection(null);
    setConsentSelection(null);
    return () => { mutation.current?.abort(); mutation.current = null; };
  }, [review, reviewCreatorId, reviewJourney, transport.creatorAccountId]);

  const currentAccount = (value: HistorySettings) => {
    const current = store.getState().creatorAccountId;
    const owner = desktopApi.getState().snapshot;
    return current !== null && current === value.creator_account_id && current === accountId
      && (!review || owner?.journey_id === reviewJourney && owner?.analysis_readiness.creator_account_id === value.creator_account_id);
  };
  const acceptSnapshot = (next: HistorySettings) => {
    if ((accountId !== null && next.creator_account_id !== accountId)
      || store.getState().creatorAccountId !== transport.creatorAccountId
      || (latest.current !== null && (latest.current.creator_account_id !== next.creator_account_id
        || latest.current.settings_revision > next.settings_revision))) return false;
    latest.current = next;
    setSettings(next);
    return true;
  };

  // Only a current, changed owner snapshot can settle an interrupted write.
  // A read of the old revision does not prove a delayed request never committed.
  const reconcileChange = async (change: HistoryChange, signal: AbortSignal, refused = false) => {
    try {
      const next = await api.get(signal);
      if (signal.aborted || !currentAccount(change.before) || !currentAccount(next)
        || next.settings_revision < change.before.settings_revision
        || (!refused && next.settings_revision === change.before.settings_revision
          && next.consent_policy_version === change.before.consent_policy_version)) return false;
      if (!acceptSnapshot(next)) return false;
      if (sameEffect(change, next)) {
        setError(null);
        if (change.desired === 'revoked') store.clearMessageCache();
      } else if (refused && settingsIdentity(next) === settingsIdentity(change.before)) {
        setRefusedIdentity(settingsIdentity(next));
        setError("Dipsy couldn't change message history settings.");
      } else {
        setError('Message history settings changed.');
      }
      setPendingChange(null);
      return true;
    } catch (cause) {
      // The read failed; only a transport failure is eligible for a timed retry.
      if (!signal.aborted && historyFailure(cause) !== 'transport') throw cause;
      return false;
    }
  };

  useEffect(() => {
    if (pendingChange === null) return;
    const wake = () => setRecoveryWake(value => value + 1);
    const visible = () => { if (document.visibilityState === 'visible') wake(); };
    window.addEventListener('focus', wake);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('focus', wake);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [pendingChange]);
  useRevealHold(loading);

  useEffect(() => {
    setOpeningPermission(false);
    return () => {
      permissionRequest.current?.abort();
      permissionRequest.current = null;
    };
  }, [transport.creatorAccountId, desktop.connected, desktop.snapshot?.journey_id, desktop.snapshot?.generation]);

  useEffect(() => {
    if (!permissionReturn) return;
    if (performance.now() >= permissionReturn.expires || !desktop.connected
      || desktop.snapshot?.journey_id !== permissionReturn.journeyId
      || desktop.snapshot.generation !== permissionReturn.generation
      || transport.creatorAccountId !== permissionReturn.creatorId) {
      setPermissionReturn(null);
      return;
    }
    if (permissionReturn.delivered && transport.connection === 'connected'
      && transport.agent?.creator_account_id === permissionReturn.creatorId
      && transport.agent.status === 'connected'
      && transport.agent.browser?.history_permission === 'granted') {
      setPermissionReturn(null);
      if (consumedPermissionReturn.current !== permissionReturn) {
        consumedPermissionReturn.current = permissionReturn;
        void focusDesktopWindow().catch(() => { /* Window focus does not change permission state. */ });
      }
      return;
    }
    const timer = setTimeout(() => setPermissionReturn(null), permissionReturn.expires - performance.now());
    return () => clearTimeout(timer);
  }, [permissionReturn, desktop.connected, desktop.snapshot?.journey_id, desktop.snapshot?.generation,
    transport.creatorAccountId, transport.connection, transport.agent]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setConsentSelection(null);
    latest.current = null;
    setSettings(null);
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = () => {
      void api.get(controller.signal).then(
        (next) => {
          if (controller.signal.aborted) return;
          if (!acceptSnapshot(next)) {
            setError('Message history settings are unavailable for this account.');
            setLoading(false);
            return;
          }
          setError(null);
          setLoading(false);
        },
        (cause) => {
          if (controller.signal.aborted) return;
          if (historyFailure(cause) === 'transport' && ++attempts < 3) {
            timer = setTimeout(load, 1000 * 2 ** (attempts - 1));
            return;
          }
          setError("Message history settings couldn't be loaded.");
          setLoading(false);
        },
      );
    };
    load();
    return () => {
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [api, transport.creatorAccountId, review, reviewCreatorId, reviewJourney]);

  useEffect(() => {
    if (busy || settings === null) return;
    const ownerChanged = Math.max(transport.agent?.required_history_settings_revision ?? 0,
      transport.agent?.applied_history_settings_revision ?? 0) > settings.settings_revision;
    if (pendingChange === null && !ownerChanged && (settings.desired_state === 'not_started'
      || settings.desired_state === settings.effective_state)) return;
    const controller = new AbortController();
    let delay = ownerChanged ? 0 : 1000;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      let settled = false;
      try {
        if (pendingChange !== null) {
          settled = await reconcileChange(pendingChange, controller.signal);
          if (settled) return;
        } else {
          const next = await api.get(controller.signal);
          if (controller.signal.aborted || mutation.current || !currentAccount(next)) return;
          acceptSnapshot(next);
        }
      } catch (cause) {
        // Stop active reads after a definite refusal or invalid response.
        // A later owner revision or normal wake may still reconcile a change.
        if (historyFailure(cause) !== 'transport') settled = true;
      } finally {
        if (!settled && !controller.signal.aborted && ++attempts < 3) {
          delay = Math.min(Math.max(delay * 2, 1000), 15_000);
          timer = setTimeout(() => void refresh(), delay);
        }
      }
    };
    timer = setTimeout(() => void refresh(), delay);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, busy, settings?.settings_revision, settings?.desired_state, settings?.effective_state,
    transport.agent?.applied_history_settings_revision, transport.agent?.required_history_settings_revision,
    transport.creatorAccountId, transport.connection, reviewAvailable, reviewCreatorId, reviewGeneration, reviewJourney,
    pendingChange, recoveryWake]);

  const allowBrowserHistory = async (value = settings) => {
    const current = store.getState();
    if (permissionRequest.current || pendingChange || !canManageHistorySync || !value
      || !currentAccount(value) || !reviewCurrent() || latest.current !== value || !value.consent_revision
      || value.desired_state !== 'running' || current.connection !== 'connected'
      || current.agent?.creator_account_id !== value.creator_account_id
      || current.agent.browser?.history_permission !== 'missing') return;
    const controller = new AbortController();
    permissionRequest.current = controller;
    setOpeningPermission(true);
    setError(null);
    const snapshot = desktop.snapshot;
    const intent: PermissionReturn | null = desktop.connected && snapshot?.journey_id
      && transport.creatorAccountId && transport.agent?.creator_account_id === transport.creatorAccountId
      && transport.agent.browser?.history_permission === 'missing'
      ? { creatorId: transport.creatorAccountId, journeyId: snapshot.journey_id,
        generation: snapshot.generation, expires: performance.now() + 90_000, delivered: false } : null;
    setPermissionReturn(intent);
    try {
      const result = await browserApi.open('history', controller.signal);
      if (controller.signal.aborted) return;
      setPermissionReturn(current => current && current === intent && result === 'delivered'
        ? { ...current, delivered: true } : current === intent ? null : current);
      if (result === 'unreachable') setError('The browser extension is not connected.');
    } catch {
      if (controller.signal.aborted) return;
      setPermissionReturn(current => current === intent ? null : current);
      setError('Chrome permission could not be opened.');
    } finally {
      if (permissionRequest.current === controller) {
        permissionRequest.current = null;
        setOpeningPermission(false);
      }
    }
  };

  const submitChange = async (desired: HistoryChange['desired'], acceptConsent = false) => {
    if (!settings || loading || busy || pendingChange || mutation.current || !canManageHistorySync
      || refusedIdentity === identity || !currentAccount(settings) || latest.current !== settings || !reviewCurrent()) return;
    const hasReceipt = settings.consent_revision !== null && settings.desired_state !== 'revoked';
    if (desired === 'revoked' ? review || !confirmRevoke || !hasReceipt
      : acceptConsent ? !consentAccepted || hasReceipt || desired !== 'running'
        : !hasReceipt || desired === settings.desired_state || review && (desired !== 'running' || settings.desired_state !== 'paused')) return;
    const change: HistoryChange = { before: settings, desired, acceptConsent };
    const controller = new AbortController();
    mutation.current = controller;
    setBusy(true);
    setError(null);
    setConsentSelection(null);
    try {
      const next = desired === 'revoked' ? await api.revoke(settings.settings_revision, controller.signal)
        : await api.update(settings.settings_revision, {
          desired_state: desired,
          consent_policy_version: acceptConsent ? settings.consent_policy_version : null,
          accept_consent: acceptConsent,
          recent_window_days: settings.recent_window_days,
          page_size: settings.page_size,
          pages_per_wake: settings.pages_per_wake,
          request_interval_ms: settings.request_interval_ms,
          retry_limit: settings.retry_limit,
        }, controller.signal);
      if (controller.signal.aborted || !currentAccount(settings)) return;
      if (!currentAccount(next) || next.settings_revision <= settings.settings_revision || !acceptSnapshot(next)) {
        throw new Error('unconfirmed_history_change');
      }
      if (sameEffect(change, next)) {
        if (desired === 'revoked') store.clearMessageCache();
        // Preserve the existing Settings gesture, but never open Chrome
        // after a read-only reconciliation of a competing or lost write.
        if (!review && desired === 'running') await allowBrowserHistory(next);
      } else {
        setError('Message history settings changed.');
      }
    } catch (cause) {
      if (controller.signal.aborted || !currentAccount(settings)) return;
      const disposition = historyFailure(cause);
      const refused = disposition === 'refused';
      let reconciled = false;
      try { reconciled = await reconcileChange(change, controller.signal, refused); }
      catch { /* A rejected read is not evidence about the write. */ }
      if (!reconciled && !controller.signal.aborted && currentAccount(settings)) {
        if (refused) {
          setRefusedIdentity(settingsIdentity(settings));
          setError("Dipsy couldn't change message history settings.");
        } else {
          setPendingChange(change);
          setError(CHANGE_UNCONFIRMED);
        }
      }
    } finally {
      if (mutation.current === controller) {
        mutation.current = null;
        setRevokeSelection(null);
        setBusy(false);
      }
    }
  };

  const update = (desired: 'running' | 'paused', acceptConsent = false) => submitChange(desired, acceptConsent);
  const revoke = () => submitChange('revoked');
  const controlsDisabled = loading || busy || pendingChange !== null || accountId === null
    || transport.creatorAccountId !== accountId || refusedIdentity === identity && identity !== null;

  const hasConsent = settings !== null
    && settings.consent_revision !== null
    && settings.desired_state !== 'revoked';
  const paused = hasConsent && settings.desired_state === 'paused';
  const progress = transport.snapshotProgress.percentage;
  const { completeConversations, discoveredConversations } = transport.snapshotProgress;
  const agent = transport.connection === 'connected' ? transport.agent : null;
  const waitingForExtension = settings !== null && !hasConsent && extensionConnection(agent) === 'offline';
  const presentation = settings && !pendingChange ? presentHistory(settings, agent, transport.coverage) : null;
  const permissionNeeded = hasConsent && !paused && agent?.browser?.history_permission === 'missing';
  const reviewScope = continuation && settings && reviewJourney && accountId
    ? JSON.stringify([accountId, reviewJourney, settings.consent_policy_version]) : null;
  const dismissed = reviewScope !== null && (dismissedReview === reviewScope
    || dismissedReviews.get(api)?.has(reviewScope) === true);
  const reviewNeeded = !hasConsent || paused || permissionNeeded;
  const dialogOpen = continuation && reviewScope !== null && openReview === reviewScope
    && reviewAvailable && (reviewNeeded || busy || pendingChange !== null);
  useEffect(() => {
    if (openReview !== null && (!reviewAvailable || openReview !== reviewScope
      || !reviewNeeded && !busy && pendingChange === null)) setOpenReview(null);
  }, [openReview, reviewAvailable, reviewScope, reviewNeeded, busy, pendingChange]);

  const controls = (
    <Panel sx={{ gap: 1.5 }}>
      <SectionHeader
        sx={{ minHeight: '1.5rem', display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'minmax(0, 1fr) 10rem' },
          gap: { xs: 1, sm: 2 }, '& > [aria-live]': { width: '100%', maxWidth: '10rem', minHeight: { xs: '2.75rem', sm: '1.5rem' }, margin: '0 !important',
            '& .MuiChip-root': { width: '100%', height: { xs: '2.75rem', sm: '1.5rem' }, alignItems: { xs: 'flex-start', sm: 'center' } },
            '& .MuiChip-icon': { mt: { xs: '0.5rem', sm: 0 } },
            '& .MuiChip-label': { minWidth: 0, width: '100%', textAlign: 'left', whiteSpace: 'normal', lineHeight: 1.35, pt: { xs: '0.125rem', sm: 0 } } } }}
        status={presentation}
        summary={
          settings !== null && !hasConsent
            ? review ? undefined : 'Add your older conversations so your numbers cover your whole history.'
            : undefined
        }
        title="Message history"
      />

      {!reviewAvailable && <Alert severity="info">Message history is unavailable for this account right now.</Alert>}
      <StatusLine id="history-feedback" tone={error && error !== 'Message history settings changed.' ? 'error' : 'secondary'}
        text={error ?? presentation?.detail ?? null} />

      {loading ? (
        <Stack spacing={1} role="status" aria-label="Loading message history settings">
          <Skeleton width="60%" />
          <Skeleton width="80%" />
          <Skeleton height={40} width={200} variant="rounded" />
        </Stack>
      ) : settings !== null ? (
        <Stack data-journey-state="desktop.message_history" spacing={1.5}>
          {waitingForExtension ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Available once the browser extension is connected.
            </Typography>
          ) : !hasConsent ? (
            <Box component="ul" sx={{ color: 'text.secondary', m: 0, pl: 2.5, typography: 'body2' }}>
              <li>Read-only: it never sends messages or changes your account.</li>
              <li>Synced message history stays on this computer.</li>
              <li>Pause or turn it off at any time.</li>
            </Box>
          ) : (
            <Box sx={{ minHeight: { xs: '2.75rem', sm: '1.75rem' } }}>
              {progress !== null && transport.coverage.status !== 'complete' && (
                <>
                  <LinearProgress aria-label="Message history progress" value={progress} variant="determinate" />
                  <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                    {discoveredConversations === null
                      ? `${NUMBER_FORMAT.format(completeConversations)} conversations synced so far`
                      : `${NUMBER_FORMAT.format(completeConversations)} of ${NUMBER_FORMAT.format(discoveredConversations)} conversations`}
                  </Typography>
                </>
              )}
            </Box>
          )}

          {!canManageHistorySync && (
            <Alert severity="info">Only the account owner can change message history.</Alert>
          )}

          {!hasConsent && !waitingForExtension && canManageHistorySync && reviewAvailable && <Box>
            <FormControlLabel
              control={(
                <Checkbox
                  checked={consentAccepted}
                  disabled={controlsDisabled}
                  onChange={(event) => setConsentSelection(event.target.checked ? identity : null)}
                />
              )}
              label="I allow read-only syncing of my older messages to this computer."
            />
          </Box>}

          {canManageHistorySync && !waitingForExtension && reviewAvailable && (
            <Stack
              direction={{ xs: 'column', sm: 'row' }}
              spacing={1.5}
              sx={{ alignItems: { xs: 'flex-start', sm: 'center' }, '& > button': { height: '2.5rem', width: 'min(16rem, 100%)', justifyContent: 'flex-start' } }}
            >
              {!hasConsent ? (
                <Button
                  disabled={!consentAccepted || controlsDisabled}
                  onClick={() => void update('running', true)}
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="contained"
                >
                  {review ? 'Allow message history' : 'Turn on message history'}
                </Button>
              ) : permissionNeeded ? (
                <Button disabled={controlsDisabled || openingPermission} onClick={() => void allowBrowserHistory()} variant="contained">Allow Chrome access</Button>
              ) : paused ? (
                <Button
                  disabled={controlsDisabled}
                  onClick={() => void update('running')}
                  size="small"
                  startIcon={<PlayCircleOutlinedIcon />}
                  variant="outlined"
                >
                  Resume
                </Button>
              ) : review ? null : (
                <Button
                  disabled={controlsDisabled}
                  onClick={() => void update('paused')}
                  size="small"
                  startIcon={<PauseCircleOutlinedIcon />}
                  variant="outlined"
                >
                  Pause
                </Button>
              )}
              {hasConsent && !review && (
                <Button
                  disabled={controlsDisabled}
                  onClick={() => setRevokeSelection(identity)}
                  size="small"
                  sx={{ color: 'text.secondary' }}
                >
                  Turn off
                </Button>
              )}
            </Stack>
          )}
        </Stack>
      ) : null}

      <Dialog open={confirmRevoke} onClose={() => !busy && setRevokeSelection(null)}>
        <DialogTitle>Turn off message history?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Older messages stop syncing. Messages already synced stay on this computer.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button disabled={controlsDisabled} onClick={() => setRevokeSelection(null)}>Cancel</Button>
          <Button color="error" disabled={controlsDisabled} onClick={() => void revoke()} variant="contained">
            Turn off
          </Button>
        </DialogActions>
      </Dialog>
    </Panel>
  );

  if (!continuation) return controls;
  const ready = reviewAvailable && reviewScope !== null && settings !== null && !loading;
  const offer = ready && (!hasConsent || paused) && !dismissed && settings.desired_state !== 'revoked';
  const quiet = ready && (!hasConsent || paused) && !offer;
  const acquisitionTitle = presentation?.label === 'Adding messages' ? 'Adding messages'
    : presentation?.label === 'Starting' || presentation?.label === 'Waiting' && transport.coverage.phase === 'not_started'
      ? 'Starting message history'
      : presentation?.label === 'Waiting' ? 'Waiting for message history'
        : presentation?.label === 'Update needed' ? 'Update needed'
          : presentation?.label === 'Needs attention' || presentation?.label === 'Incomplete' ? 'Message history needs attention'
            : 'Waiting for messages';
  const title = !ready ? 'Waiting for messages'
    : pendingChange !== null ? 'Checking message history'
      : quiet ? 'Waiting for messages'
        : offer ? paused ? 'Message history is paused' : 'See your first conversations'
          : permissionNeeded ? 'Allow Chrome access' : acquisitionTitle;
  const detail = pendingChange !== null && ready ? "Your change hasn't been confirmed."
    : offer ? paused ? 'Resume adding older messages when you’re ready.'
      : 'Dipsy can use new messages as they arrive. Add older conversations to see results sooner.'
      : permissionNeeded && ready ? 'Allow Chrome to read older messages for this app.'
        : ready && acquisitionTitle === 'Starting message history' && hasConsent ? 'Checking your browser connection.'
          : ready && hasConsent && !quiet && acquisitionTitle !== 'Adding messages' ? presentation?.detail ?? null
            : 'Results will appear as messages become available.';
  const dismiss = () => {
    if (!reviewScope) return;
    rememberDismissal(api, reviewScope);
    setDismissedReview(reviewScope);
    setOpenReview(null);
  };
  return (
    <>
      <Panel component="section" emphasis="quiet" aria-labelledby="history-continuation-title">
        <Typography component="h2" id="history-continuation-title" variant="h5">{title}</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{detail}</Typography>
        {offer && <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
          <Button variant="outlined" disabled={controlsDisabled} onClick={() => setOpenReview(reviewScope)}>Review message history</Button>
          <Button onClick={dismiss}>Not now</Button>
        </Stack>}
        {quiet && <Button variant="text" disabled={controlsDisabled} onClick={() => setOpenReview(reviewScope)}
          sx={{ alignSelf: 'flex-start' }}>Message history</Button>}
        {ready && permissionNeeded && !dialogOpen && <Button variant="outlined"
          disabled={controlsDisabled || openingPermission} onClick={() => void allowBrowserHistory()}
          sx={{ alignSelf: 'flex-start' }}>Allow Chrome access</Button>}
        {error && error !== detail && !dialogOpen && <StatusLine id="history-continuation-feedback" text={error} tone="error" />}
      </Panel>
      <Dialog open={dialogOpen} onClose={() => setOpenReview(null)} aria-labelledby="history-review-title" fullWidth maxWidth="sm">
        <DialogTitle id="history-review-title">Add older conversations</DialogTitle>
        <DialogContent>{controls}</DialogContent>
        <DialogActions><Button onClick={() => setOpenReview(null)}>Close</Button></DialogActions>
      </Dialog>
    </>
  );
}
