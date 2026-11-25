import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
  useMediaQuery,
} from '@mui/material';
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react';

import { ProviderSignInButtons, type SignInProvider } from './ProviderSignInButtons';
import { Panel, useRevealHold } from './ui';
import type { FullPresentationIntent } from '../protocol';
import {
  capabilityLicenseApi,
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  CapabilityLicenseApiError,
  type CapabilityLicenseApi,
} from '../services/capabilityLicenseApi';
import { desktopOnboardingApi, selectAnalysisReadiness, selectBrowserFeedback, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import { componentTokens, typography } from '../theme';
import { OnboardingAction, OnboardingPill, OnboardingSpot, OnboardingStatus } from './ui/OnboardingCard';
import { offerPriceColors } from '../theme/generated/tokens';

const ACTIVATION_BODY = 'Conversation insights, like your most common topics, stored on this computer.';
const ACTIVATION_PENDING = 'Checking your activation…';
const SIGN_IN_SWITCH = 'Use a different sign-in for Full analytics';
const CONTROL_HEIGHT = componentTokens.staticUi.controlHeight;
const buttonSx = { minHeight: CONTROL_HEIGHT, maxWidth: '100%', overflowWrap: 'anywhere' } as const;
const quietButtonSx = { ...buttonSx, color: 'text.primary', fontWeight: 600, px: 0, justifyContent: 'flex-start', textAlign: 'start' } as const;
const taskTitleSx = { fontWeight: 600, lineHeight: 1.25, letterSpacing: '-.015em', textWrap: 'balance' } as const;
const dialogActionsSx = { justifyContent: 'flex-start', p: 0, mt: 3 } as const;
const dismissedOfferScopes = new Set<string>();
const DISMISSED_OFFER_KEY = 'full-analytics-review-dismissals-v1';
function dismissedIntent(scope: string) {
  if (dismissedOfferScopes.has(scope)) return true;
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(DISMISSED_OFFER_KEY) ?? '[]');
    return Array.isArray(saved) && saved.includes(scope);
  } catch { return false; }
}
function dismissIntent(scope: string) {
  dismissedOfferScopes.add(scope);
  if (dismissedOfferScopes.size > 64) dismissedOfferScopes.delete(dismissedOfferScopes.values().next().value!);
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(DISMISSED_OFFER_KEY) ?? '[]');
    const scopes = Array.isArray(saved) ? saved.filter((value): value is string => typeof value === 'string' && value !== scope) : [];
    localStorage.setItem(DISMISSED_OFFER_KEY, JSON.stringify([...scopes, scope].slice(-64)));
  } catch { /* The current presentation still remembers dismissal. */ }
}

function safeMessage(error: unknown): string {
  return error instanceof CapabilityLicenseApiError ? error.message : "Activation couldn't be checked.";
}

/** Native offer review and explicit consent; readiness remains Brain-owned. */
export function CommercialActivationControls({
  api = capabilityLicenseApi,
  onboarding = desktopOnboardingApi,
  embedded = false,
  creatorAccountId,
  presentationIntent,
  dashboard = false,
}: {
  api?: CapabilityLicenseApi;
  onboarding?: DesktopOnboardingApi;
  embedded?: boolean;
  creatorAccountId?: string | null;
  presentationIntent?: FullPresentationIntent;
  dashboard?: boolean;
}) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const sectionTitleId = useId();
  const dialogTitleId = useId();
  const feedbackId = useId();
  const [entryError, setEntryError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [openingAction, setOpeningAction] = useState<string | null>(null);
  const [codeOpen, setCodeOpen] = useState(false);
  const [dismissedPrompt, setDismissedPrompt] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [codeBusy, setCodeBusy] = useState(false);
  const [codePending, setCodePending] = useState(false);
  const [codeUnconfirmed, setCodeUnconfirmed] = useState(false);
  const [refusedCode, setRefusedCode] = useState<string | null>(null);
  const attemptedCode = useRef<string | null>(null);
  const codePresentation = useRef<string | null>(null);
  const codeOperation = useRef<AbortController | null>(null);
  const codeScope = useRef<string | null>(null);
  const codeCreator = useRef<string | null>(null);
  const offerCodeButton = useRef<HTMLButtonElement | null>(null);
  const previousCodeOpen = useRef(false);
  const sectionHeading = useRef<HTMLHeadingElement | null>(null);
  const returnFocus = useRef<HTMLButtonElement | null>(null);
  const codeFormId = useId();
  const focusCodeForm = useCallback((form: HTMLFormElement | null) => {
    form?.querySelector<HTMLElement>('input:enabled, button:enabled')?.focus();
  }, []);
  // Only an action in this presentation opens a review. A pending offer can
  // be reviewed again explicitly after navigation or dismissal.
  const [openReviewScope, setOpenReviewScope] = useState<string | null>(null);
  const [unconfirmedOffer, setUnconfirmedOffer] = useState<string | null>(null);
  const [choosingAccount, setChoosingAccount] = useState(false);
  const choicesElement = useRef<HTMLDivElement | null>(null);
  const switchElement = useRef<HTMLButtonElement | null>(null);
  const previousChoosing = useRef(false);
  const entryOperation = useRef<AbortController | null>(null);
  const entryScope = useRef<string | null>(null);
  const previousReviewScope = useRef<string | null>(null);
  const desktop = useSyncExternalStore(onboarding.subscribe, onboarding.getState, onboarding.getState);
  const { value: readiness, checking, availability } = selectAnalysisReadiness(desktop, creatorAccountId);
  const error = !checking && desktop.connected && availability !== 'current' ? "Activation couldn't be checked." : null;
  const snapshot = desktop.snapshot;
  const ownerCreator = snapshot?.analysis_readiness.creator_account_id;
  const sameCreator = Boolean(ownerCreator && (creatorAccountId === undefined || ownerCreator === creatorAccountId));
  const currentAuthority = desktop.connected && sameCreator && availability === 'current';
  const decision = sameCreator && readiness?.commercial_authority !== 'active' && snapshot?.decision?.action === 'activate-complimentary'
    && snapshot.decision_status?.state !== 'accepted' ? snapshot.decision : null;
  const decisionStatus = sameCreator ? snapshot?.decision_status?.state : undefined;
  const authentication = sameCreator ? snapshot?.authentication : null;
  const allowed = currentAuthority ? snapshot?.allowed_actions || [] : [];
  const reviewScope = sameCreator && snapshot ? JSON.stringify([snapshot.generation, snapshot.journey_id, ownerCreator]) : null;
  const offerOpen = reviewScope !== null && openReviewScope === reviewScope;
  const setOfferOpen = (open: boolean) => setOpenReviewScope(open ? reviewScope : null);
  const ownerScope = sameCreator && snapshot ? JSON.stringify([snapshot.journey_id, ownerCreator]) : null;
  // Renewal changes the presentation, not the original commercial decision.
  const offerIdentity = decision && ownerScope ? JSON.stringify([ownerScope, decision.operation_id, decision.offer.offer_version_id]) : null;
  // A rendered offer must not confirm a newer decision that arrived before React
  // could paint it. The command owner independently rechecks admission.
  const currentPresentation = () => {
    const current = onboarding.getState();
    return current.connected && current.snapshot === snapshot && current.pending === desktop.pending
      && current.snapshot?.analysis_readiness.creator_account_id === ownerCreator;
  };
  const accepted = decisionStatus === 'accepted';
  const unprojectedOperation = Boolean(desktop.pending && !snapshot?.pending_operations.some(item => item.operation_id === desktop.pending));
  const pendingConfirmation = Boolean(snapshot?.pending_operations.some(item => item.action === 'activation.confirm'));
  const unresolvedCommand = unprojectedOperation || pendingConfirmation;
  const unresolvedActivation = pendingConfirmation || unprojectedOperation
    && (desktop.pendingAction?.startsWith('activation.') || desktop.pendingAction?.startsWith('auth.'));
  const codeInProgress = codeBusy || codePending || codeUnconfirmed;
  const canSelect = !accepted && !unresolvedCommand && !codeInProgress && authentication?.actions.select && allowed.includes('auth.select');
  const canSwitch = !accepted && !unresolvedCommand && !codeInProgress && authentication?.state === 'admitted'
    && authentication.actions.switch_account && allowed.includes('auth.switch');
  const choosingProvider = Boolean(canSelect || (choosingAccount && canSwitch));
  const providers = (['google', 'microsoft-personal', 'telegram', 'tiktok', 'default'] as const).filter(provider => authentication?.choices[provider]);
  const canConfirm = currentAuthority && decision !== null && allowed.includes('activation.confirm')
    && unconfirmedOffer !== offerIdentity && !unresolvedCommand && !codeInProgress;
  const activationOutcome = sameCreator ? snapshot?.pending_operations.find(value => value.action === 'activation.review') : undefined;
  const activationRequired = readiness?.commercial_authority === 'required';
  const reviewOperationId = snapshot?.decision_status?.operation_id ?? decision?.operation_id ?? authentication?.operation_id;
  const mismatchOperations = currentAuthority && activationRequired ? snapshot?.pending_operations.filter(operation =>
    operation.action === 'activation.review' && (!reviewOperationId || operation.operation_id === reviewOperationId)
    || operation.action === 'activation.confirm' && operation.operation_id === desktop.pending) ?? [] : [];
  const mismatchOperation = mismatchOperations.length === 1 ? mismatchOperations[0] : undefined;
  const operationError = desktop.operationError;
  const classifiedMismatch = mismatchOperation !== undefined && (mismatchOperation.reason === 'account_mismatch'
    || operationError?.reason === 'account_mismatch' && operationError.operationId === mismatchOperation.operation_id
      && operationError.action === mismatchOperation.action);
  const mismatchMessage = classifiedMismatch ? snapshot?.facts.account === 'verified'
    ? 'Switch to the OnlyFans account you used for setup.'
    : 'Sign in to the OnlyFans account you want to connect.' : null;
  const pendingActivation = Boolean(activationOutcome);
  const replacementAvailable = pendingActivation && allowed.includes('activation.review');
  const canOpen = currentAuthority && (!pendingActivation || replacementAvailable)
    && (!unresolvedCommand || (replacementAvailable && !unprojectedOperation)) && !codeInProgress && allowed.includes('activation.review');
  // Code recovery may continue an accepted activation, not compete with an
  // outstanding confirmation or an unacknowledged desktop command.
  // Intent can display an already-admitted review. It cannot prepare a
  // ceremony, open the browser or execute activation in an effect.
  const intentScope = currentAuthority && snapshot?.local_ready && presentationIntent?.journey_id === snapshot.journey_id
    ? JSON.stringify([ownerCreator, snapshot.journey_id, presentationIntent.disclosure_bundle_id]) : null;
  const promotedIntent = useRef<string | null>(null);
  useEffect(() => {
    if (promotedIntent.current && promotedIntent.current !== intentScope) {
      promotedIntent.current = null;
      setOpenReviewScope(null);
      return;
    }
    if (intentScope && reviewScope && decision && canConfirm && !codeOpen && !offerOpen
      && !opening && !dismissedIntent(intentScope) && currentPresentation()) {
      promotedIntent.current = intentScope;
      setOpenReviewScope(reviewScope);
    }
  }, [intentScope, reviewScope, decision, canConfirm, codeOpen, offerOpen, opening]);
  const dismissOffer = () => {
    if (intentScope) dismissIntent(intentScope);
    promotedIntent.current = null;
    setOfferOpen(false);
  };
  const canRedeem = currentAuthority && !classifiedMismatch && !unresolvedCommand && (!pendingActivation || accepted || decision !== null)
    && (!unconfirmedOffer || unconfirmedOffer !== offerIdentity);
  useRevealHold(checking);

  useEffect(() => () => { entryOperation.current?.abort(); codeOperation.current?.abort(); }, []);
  useEffect(() => { setChoosingAccount(false); }, [authentication?.authentication_attempt?.authentication_attempt_id]);
  useEffect(() => {
    const previous = previousReviewScope.current;
    previousReviewScope.current = reviewScope;
    if (previous === null || previous === reviewScope) return;
    entryOperation.current?.abort();
    entryOperation.current = null;
    entryScope.current = null;
    setOpening(false); setOpeningAction(null); setEntryError(null);
    setOpenReviewScope(null); setChoosingAccount(false);
  }, [reviewScope]);
  useEffect(() => {
    if (choosingAccount) choicesElement.current?.querySelector('button')?.focus();
    else if (previousChoosing.current) switchElement.current?.focus();
    previousChoosing.current = choosingAccount;
  }, [choosingAccount]);

  const stopOpeningSetup = () => {
    entryOperation.current?.abort();
    entryOperation.current = null;
    setOpening(false);
    setOpeningAction(null);
    setEntryError(null);
  };

  useEffect(() => {
    if (decisionStatus === 'accepted') stopOpeningSetup();
  }, [decisionStatus]);

  const openSetup = async (action: 'activation.review' | 'activation.confirm' | 'auth.select' | 'auth.switch' = 'activation.review', provider?: SignInProvider) => {
    if (codeOperation.current || codeInProgress || (unresolvedCommand && !(action === 'activation.review' && replacementAvailable && !unprojectedOperation)) || (entryOperation.current && !entryOperation.current.signal.aborted)) return;
    const permitted = action === 'activation.confirm' ? canConfirm : action === 'auth.select' ? canSelect
      : action === 'auth.switch' ? canSwitch : canOpen;
    if (!currentPresentation()) return;
    if (!currentAuthority || !permitted || (provider && !providers.includes(provider))) {
      entryScope.current = reviewScope;
      setEntryError('Full analytics setup is unavailable on this page.');
      return;
    }
    const controller = new AbortController();
    entryOperation.current = controller;
    entryScope.current = reviewScope;
    setOpening(true);
    setOfferOpen(true);
    setOpeningAction(action);
    if (action === 'activation.confirm') {
      if (intentScope) dismissIntent(intentScope);
      setUnconfirmedOffer(offerIdentity);
    }
    setEntryError(null);
    try {
      if (provider) await onboarding.begin(action, { provider });
      else await onboarding.begin(action);
      if (!controller.signal.aborted) setChoosingAccount(false);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setEntryError(cause instanceof Error && cause.message === 'browser_unavailable'
          ? 'The browser action could not be confirmed.' : action === 'activation.confirm' ? 'Activation could not be confirmed.'
            : action === 'activation.review' ? "The activation offer couldn't be prepared." : 'Sign-in could not be opened.');
      }
    } finally {
      if (entryOperation.current === controller) {
        entryOperation.current = null;
        if (!controller.signal.aborted) { setOpening(false); setOpeningAction(null); }
      }
    }
  };

  const reopenBrowser = async () => {
    if (!currentAuthority || !currentPresentation() || codeInProgress || unresolvedCommand || entryOperation.current || !authentication?.actions.open_browser || !authentication.browser_operation_id) return;
    const controller = new AbortController(); entryOperation.current = controller; entryScope.current = reviewScope;
    setOpening(true); setOpeningAction('browser.open'); setEntryError(null);
    try { await onboarding.openBrowser(authentication.browser_operation_id); }
    catch { if (!controller.signal.aborted) setEntryError('The browser could not be opened.'); }
    finally {
      if (entryOperation.current === controller) {
        entryOperation.current = null;
        if (!controller.signal.aborted) { setOpening(false); setOpeningAction(null); }
      }
    }
  };

  useEffect(() => {
    if (readiness?.commercial_authority !== 'active') return;
    stopOpeningSetup();
    codeOperation.current?.abort(); codeOperation.current = null;
    codeScope.current = null; codeCreator.current = null; codePresentation.current = null; attemptedCode.current = null;
    setCode(''); setCodeOpen(false); setCodeBusy(false); setCodePending(false); setCodeUnconfirmed(false); setRefusedCode(null); setCodeError(null);
  }, [readiness?.commercial_authority]);

  useEffect(() => {
    // Losing the presentation is not evidence of a different owner.
    if (codeScope.current === null) return;
    const changedOwner = creatorAccountId !== undefined && creatorAccountId !== codeCreator.current
      || ownerCreator != null && ownerCreator !== codeCreator.current
      || ownerScope !== null && codeScope.current !== ownerScope;
    if (changedOwner) {
      codeOperation.current?.abort(); codeOperation.current = null;
      codeScope.current = null; codeCreator.current = null; codePresentation.current = null; attemptedCode.current = null;
      setCode(''); setCodeOpen(false); setCodeBusy(false); setCodePending(false);
      setCodeUnconfirmed(false); setRefusedCode(null); setCodeError(null);
      return;
    }
    if (codeOperation.current && (!desktop.connected || codePresentation.current !== reviewScope)) {
      codeOperation.current.abort(); codeOperation.current = null;
      setCodeBusy(false); setCodeUnconfirmed(true);
      setCodeError("Activation couldn't be confirmed.");
    }
  }, [ownerScope, reviewScope, desktop.connected, ownerCreator, creatorAccountId]);

  const redeemCode = async () => {
    if (!canRedeem || !currentPresentation() || codeOperation.current || entryOperation.current || opening || codePending || ownerScope === null) return;
    const value = code.trim();
    if (refusedCode === value || codeUnconfirmed && (attemptedCode.current !== value || codeScope.current !== ownerScope)) return;
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setCodeError('Enter the full activation code.'); return;
    }
    const controller = new AbortController(); codeOperation.current = controller;
    codeScope.current = ownerScope;
    codeCreator.current = ownerCreator ?? null;
    codePresentation.current = reviewScope;
    attemptedCode.current = value;
    setCodeBusy(true); setCodeError(null);
    try {
      await api.redeem(value, controller.signal);
      if (!controller.signal.aborted) { setCodePending(true); setCodeUnconfirmed(false); setRefusedCode(null); }
    } catch (cause) {
      if (!controller.signal.aborted) {
        const outcome = cause instanceof CapabilityLicenseApiError ? cause.outcome : 'unknown';
        setCodeUnconfirmed(outcome === 'unknown' || codeUnconfirmed && outcome === 'not_sent');
        setRefusedCode(outcome === 'refused' ? value : null);
        setCodeError(safeMessage(cause));
      }
    } finally {
      if (codeOperation.current === controller) {
        codeOperation.current = null; setCodeBusy(false);
      }
    }
  };

  const fullReady = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'admitted';
  const activeButBlocked = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'blocked';
  const activationUnavailable = readiness?.commercial_authority === 'unavailable';
  const signInBrowserId = authentication?.actions.open_browser ? authentication.browser_operation_id : null;
  const reviewBrowserId = !authentication && activationOutcome && (!reviewOperationId || activationOutcome.operation_id === reviewOperationId)
    ? activationOutcome.operation_id : null;
  const browserFeedback = currentAuthority && activationRequired && !accepted && !decision
    ? selectBrowserFeedback(desktop, signInBrowserId ?? reviewBrowserId) : null;
  const browserLaunchFailed = browserFeedback?.reason === 'browser_unavailable' || browserFeedback?.reason === 'chrome_unavailable';
  const reviewRefused = currentAuthority && activationRequired && !accepted && !decision && !authentication
    && operationError?.action === 'activation.review' && operationError.reason === 'activation_unavailable'
    && (!reviewOperationId || operationError.operationId === reviewOperationId)
    && (!activationOutcome || operationError.operationId === activationOutcome.operation_id);
  const noticeError = (entryScope.current === reviewScope ? entryError : null) ?? error ?? (!codeOpen ? codeError : null) ?? browserFeedback?.message;
  const offer = decision?.offer;
  const codeChecking = codeBusy || codePending;
  const activationChecking = currentAuthority && activationRequired && accepted;
  const terminalUnknown = currentAuthority && activationRequired && !classifiedMismatch && !unresolvedCommand && !codeInProgress && !authentication
    && decisionStatus === 'unavailable' && !offer && (!activationOutcome || activationOutcome.state === 'refused'
      || activationOutcome.state === 'accepted' && activationOutcome.reason === 'none');
  const feedback = noticeError || (!desktop.connected ? desktop.error === 'session_unavailable' ? 'The setup session ended. Reconnecting…' : 'Activation status could not be received.'
    : fullReady ? null : opening ? openingAction === 'activation.confirm' ? 'Confirming activation…'
      : openingAction === 'activation.review' ? 'Preparing activation…' : 'Opening browser…'
        : mismatchMessage ? mismatchMessage
          : terminalUnknown ? 'Activation couldn’t finish.'
          : replacementAvailable ? 'Activation was not completed.'
          : unresolvedActivation ? 'Activation has not been confirmed.'
          : choosingProvider ? choosingAccount ? 'Choose a different sign-in.' : 'Choose how to sign in.'
            : activationOutcome?.state === 'waiting' && activationOutcome.reason === 'activation_unavailable' ? 'Checking the activation offer.'
              : activationOutcome?.state === 'outcome_unknown' ? 'Activation has not been confirmed.'
            : authentication?.state === 'admitted' && authentication.actions.open_browser ? 'The activation offer is unavailable.'
            : authentication?.state === 'expired' ? 'This sign-in request expired.'
              : authentication?.state === 'retired' ? 'Sign-in was cancelled.'
                : authentication?.state === 'unavailable' ? 'The sign-in request could not be confirmed.'
                  : authentication?.state === 'selected' || decisionStatus === 'awaiting-authentication' ? 'Finish signing in in your browser.'
                    : decisionStatus === 'unavailable' || activationOutcome?.reason === 'activation_unavailable' || reviewRefused ? 'Full analytics activation could not be prepared.'
                      : unprojectedOperation ? 'Waiting for a desktop action to finish.'
                        : checking && !decision ? 'Checking activation…'
              : activeButBlocked ? "New messages aren't being analyzed"
                : activationUnavailable || readiness === null ? "Activation couldn't be checked." : null);
  const showOfferDialog = Boolean(offer && offerOpen && sameCreator && !fullReady);
  const showCodeDialog = codeOpen && !fullReady && (codeCreator.current === null
    || ((creatorAccountId === undefined || codeCreator.current === creatorAccountId)
      && (ownerCreator == null || codeCreator.current === ownerCreator)));
  const dialogOpen = showCodeDialog || showOfferDialog;
  const closeCode = () => {
    if (codeChecking) dismissOffer();
    setCodeOpen(false);
  };
  useEffect(() => {
    if (!showCodeDialog && previousCodeOpen.current && showOfferDialog) {
      offerCodeButton.current?.focus();
    }
    previousCodeOpen.current = showCodeDialog;
  }, [showCodeDialog, showOfferDialog]);

  const canEnterCode = !fullReady && !terminalUnknown && !classifiedMismatch && !choosingProvider
    && (!dashboard && !offer && activationRequired && (!pendingActivation || accepted) || codeUnconfirmed);
  const showReview = !fullReady && !accepted && (!codeChecking || showCodeDialog) && (!terminalUnknown || replacementAvailable) && !classifiedMismatch && !choosingProvider
    && Boolean(offer || (activationRequired && canOpen || showCodeDialog && !offer) && (!pendingActivation || replacementAvailable));
  const promptDismissed = ownerScope !== null && dismissedPrompt === ownerScope;
  const showPrompt = dashboard && !promptDismissed && !replacementAvailable && !activationChecking && showReview;
  const pending = activationChecking || codeChecking && !showCodeDialog;
  const title = pending ? 'Activating Full analytics' : terminalUnknown ? 'Full analytics isn’t on'
    : classifiedMismatch ? 'Full analytics unavailable' : showPrompt ? 'Turn on Full analytics' : 'Full analytics';
  const guided = dashboard || pending || terminalUnknown || classifiedMismatch;
  const status = fullReady ? 'On' : activeButBlocked ? 'Needs attention' : activationRequired && !pending && !terminalUnknown ? 'Off' : null;
  const unavailable = !decision && !authentication && !accepted && activationRequired && !allowed.includes('activation.review') && !pendingActivation && desktop.connected;
  const inlineFeedback = !dialogOpen && !pending ? feedback || (unavailable ? 'The activation request is unavailable.'
    : fullReady && !embedded ? 'Tone, reply, and topic insights.' : null) : null;
  const codeFeedback = codeError || (canRedeem ? null : feedback);
  const offerFeedback = !desktop.connected ? feedback : noticeError || (opening ? feedback : null);
  const showSwitch = canSwitch && !choosingAccount;
  const showBrowser = !accepted && !unresolvedCommand && !codeInProgress && !choosingProvider && signInBrowserId
    && currentAuthority && !desktop.browserLaunchPending && (authentication?.state === 'admitted' || browserLaunchFailed);
  const hasActions = pending || canEnterCode || showReview || showBrowser || showSwitch || choosingAccount;
  const inlineStatus = inlineFeedback && <Box id={feedbackId} role={noticeError || terminalUnknown ? 'alert' : undefined}>
    <OnboardingStatus tone={noticeError || terminalUnknown ? 'error' : classifiedMismatch ? 'attention' : 'neutral'} live={!noticeError && !terminalUnknown}>{inlineFeedback}</OnboardingStatus>
  </Box>;

  if (dashboard && promptDismissed && !dialogOpen && !pending && !inlineFeedback && !authentication && !pendingActivation) return null;

  return <Panel component="section" aria-labelledby={embedded ? undefined : sectionTitleId} emphasis="quiet"
    data-dashboard-task-part={dashboard ? `activation:${pending ? 'pending' : showPrompt ? 'prompt' : terminalUnknown ? 'ended' : 'status'}:${hasActions}:${Boolean(inlineStatus)}` : undefined}
    sx={{ gap: 1, minWidth: 0, overflowWrap: 'anywhere', ...(embedded ? { p: 0 } : dashboard ? {
      bgcolor: 'background.paper', border: 1, borderColor: 'divider', maxWidth: 760, p: { xs: 3, sm: 4 },
    } : {}) }}>
    {!embedded && <Box>
      {guided && <Box className="onboarding-surface-head"><OnboardingSpot icon="insights" />
        {title !== 'Full analytics' && <span>Full analytics</span>}
      </Box>}
      <Stack direction="row" useFlexGap sx={{ gap: 1, flexWrap: 'wrap', alignItems: 'flex-start', justifyContent: 'space-between' }}>
        <Typography ref={sectionHeading} id={sectionTitleId} component="h2" variant="h6" tabIndex={-1}
          sx={guided ? { ...taskTitleSx, fontSize: { xs: '1.25rem', sm: '1.375rem' } } : undefined}>{title}</Typography>
        {!dashboard && <Box aria-live="polite" sx={{
          display: 'grid', justifyItems: 'start',
          '&::before': { content: '"Needs attention" / ""', gridArea: '1 / 1', visibility: 'hidden',
            fontSize: 'var(--dipsy-static-caption-size, .75rem)', lineHeight: 1.5, fontWeight: 500, py: '3px', px: '16px' },
          '& > span': { gridArea: '1 / 1' },
        }}>{status && <OnboardingPill tone={fullReady ? 'progress' : 'neutral'}>{status}</OnboardingPill>}</Box>}
      </Stack>
    </Box>}
    {showPrompt && <Typography color="text.secondary" sx={{ maxWidth: '60ch' }}>{ACTIVATION_BODY}</Typography>}
    <Stack data-journey-state="desktop.full_analytics_activation" spacing={1}
      sx={guided && (hasActions || inlineStatus || choosingProvider) ? { mt: 2 } : undefined}>
      {choosingProvider && !offer && !fullReady && <Box ref={choicesElement}><ProviderSignInButtons providers={providers}
        disabled={opening} describedBy={inlineFeedback ? feedbackId : sectionTitleId}
        onSelect={provider => void openSetup(choosingAccount ? 'auth.switch' : 'auth.select', provider)} /></Box>}
      <OnboardingAction>
        {pending && !showCodeDialog && <OnboardingStatus tone="pending">{ACTIVATION_PENDING}</OnboardingStatus>}
        {canEnterCode && (!codeChecking || showCodeDialog) && <Button type="button" aria-haspopup="dialog" variant="contained" sx={buttonSx}
          disabled={!canRedeem || opening || codeChecking} onClick={event => {
            if (!currentPresentation()) return;
            returnFocus.current = event.currentTarget;
            setCodeOpen(true);
          }}>Use an activation code</Button>}
        {showReview && (!dashboard || !promptDismissed) && <Button type="button" aria-haspopup="dialog" variant={showPrompt ? 'contained' : 'outlined'}
          disabled={offer ? !currentAuthority : !canOpen || (!canRedeem && !replacementAvailable) || opening || checking}
          sx={buttonSx} onClick={event => {
            if (!currentPresentation()) return;
            returnFocus.current = event.currentTarget;
            if (offer) setOfferOpen(true);
            else void openSetup();
          }}>{replacementAvailable ? 'Review offer again' : 'Review offer'}</Button>}
        {showPrompt && <Button sx={quietButtonSx} onClick={() => { dismissOffer(); setDismissedPrompt(ownerScope); }}>Not now</Button>}
        {!fullReady && showBrowser && <Button onClick={() => void reopenBrowser()} disabled={opening} sx={buttonSx}>Open browser</Button>}
        {showSwitch && !offer && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={opening} sx={quietButtonSx}>{SIGN_IN_SWITCH}</Button>}
        {choosingAccount && !offer && <Button onClick={() => setChoosingAccount(false)} disabled={opening} sx={buttonSx}>Back</Button>}
        {!hasActions && (!dashboard && !embedded && !guided
          ? <Box sx={{ typography: 'body2', width: '100%', minBlockSize: '2lh' }}>{inlineStatus}</Box>
          : inlineStatus)}
      </OnboardingAction>
      {hasActions && inlineStatus}
    </Stack>

    <Dialog open={dialogOpen} onClose={showCodeDialog ? closeCode : dismissOffer} aria-labelledby={dialogTitleId} scroll="body" fullWidth maxWidth="sm"
      transitionDuration={reducedMotion ? 0 : 200}
      onTransitionExited={() => {
        const target = returnFocus.current;
        if (target?.isConnected && !target.disabled) target.focus();
        else sectionHeading.current?.focus();
        returnFocus.current = null;
      }}
      slotProps={{ paper: { sx: { bgcolor: 'background.paper', backgroundImage: 'none', overflow: 'visible', overflowWrap: 'anywhere',
        verticalAlign: 'top',
        boxSizing: 'border-box', m: { xs: 2, sm: 4 }, p: { xs: 3, sm: 4 }, width: { xs: 'calc(100% - 32px)', sm: 'calc(100% - 64px)' },
        '& .MuiDialogContent-root': { p: 0, overflow: 'visible' },
      } } }}>
      <Box className="onboarding-surface-head"><OnboardingSpot icon={showCodeDialog ? 'code' : 'insights'} /></Box>
      <DialogTitle id={dialogTitleId} sx={{ ...taskTitleSx, p: 0, mb: 1, fontSize: { xs: '1.125rem', sm: '1.25rem' } }}>
        {showCodeDialog ? codeChecking ? 'Activating Full analytics' : 'Use an activation code' : choosingAccount ? SIGN_IN_SWITCH : 'Activate Full analytics'}
      </DialogTitle>
      {showCodeDialog ? <Box component="form" ref={focusCodeForm} onSubmit={event => { event.preventDefault(); void redeemCode(); }}>
        <DialogContent>
          <Typography component="label" htmlFor={codeFormId} variant="body2" sx={{ display: 'block', mb: 1 }}>Activation code</Typography>
          <TextField id={codeFormId} autoComplete="off" fullWidth variant="outlined" value={code}
            disabled={!canRedeem || codeChecking || codeUnconfirmed || opening}
            slotProps={{ htmlInput: { spellCheck: false, maxLength: 256, 'aria-describedby': codeFeedback ? `${codeFormId}-status` : undefined },
              input: { sx: { fontFamily: typography.numericBody.fontFamily, fontVariantNumeric: 'tabular-nums' } } }}
            onChange={event => {
              if (!codeUnconfirmed && ownerScope !== null) {
                codeScope.current = ownerScope;
                codeCreator.current = ownerCreator ?? null;
                setCode(event.target.value); setCodeError(null);
              }
            }} />
        </DialogContent>
        <DialogActions sx={dialogActionsSx}>
          <OnboardingAction swap={codeChecking ? 'pending' : 'ready'}>
            {codeChecking ? <OnboardingStatus tone="pending">{ACTIVATION_PENDING}</OnboardingStatus>
              : <Button type="submit" variant="contained" sx={buttonSx} disabled={!canRedeem || opening || !code.trim() || refusedCode === code.trim()
                || codeUnconfirmed && (codeScope.current !== ownerScope || attemptedCode.current !== code.trim())}>
                {codeUnconfirmed ? 'Resume activation' : 'Activate'}
              </Button>}
            <Button onClick={closeCode} sx={quietButtonSx}>Cancel</Button>
          </OnboardingAction>
        </DialogActions>
        {!codeChecking && codeFeedback && <Box id={`${codeFormId}-status`} role={codeError ? 'alert' : undefined} sx={{ mt: 2 }}>
          <OnboardingStatus tone={codeError ? 'error' : 'neutral'} live={!codeError}>{codeFeedback}</OnboardingStatus>
        </Box>}
      </Box> : offer && <>
        <DialogContent>
          {choosingAccount ? <Stack spacing={2}>
            <Typography id={`${feedbackId}-switch`} variant="body2">Choose a different sign-in.</Typography>
            <Box ref={choicesElement}><ProviderSignInButtons providers={providers} disabled={opening || !canSwitch} describedBy={`${feedbackId}-switch`}
              onSelect={provider => void openSetup('auth.switch', provider)} /></Box>
            <Button onClick={() => setChoosingAccount(false)} disabled={opening}>Back</Button>
          </Stack> : <Stack spacing={2} useFlexGap>
            <Typography color="text.secondary" sx={{ maxWidth: '60ch' }}>{ACTIVATION_BODY}</Typography>
            <Box component="dl" sx={{ m: 0,
              '& > div': { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', alignItems: 'baseline', columnGap: 2, py: 1.25 },
              '& > div + div': { borderTop: 1, borderColor: 'divider' },
              '& dt': { color: 'text.secondary' },
              '& dd': { m: 0, color: 'text.primary', fontWeight: 600, fontVariantNumeric: 'tabular-nums', textAlign: 'end', maxWidth: '24ch' },
              '@media (max-width: 359px)': {
                '& > div': { gridTemplateColumns: '1fr' },
                '& dd': { textAlign: 'start' },
              },
            }}>
              <div><Typography component="dt" variant="body2">Funding</Typography><Typography component="dd" variant="body2">Complimentary pilot</Typography></div>
              <div><Typography component="dt" variant="body2">Price</Typography><Typography component="dd" variant="body2">
                <Typography component="span" variant="inherit" sx={[
                  { display: 'inline-block', px: 0.75, borderRadius: '6px', bgcolor: offerPriceColors.light.fill, color: offerPriceColors.light.ink },
                  theme => theme.applyStyles('dark', { bgcolor: offerPriceColors.dark.fill, color: offerPriceColors.dark.ink }),
                ]}>Free</Typography>
                <Typography component="span" variant="inherit" sx={{ display: 'block', color: 'text.secondary', fontWeight: 400 }}>
                  Uses {new Intl.NumberFormat().format(offer.pilot_credit_cost)} {offer.pilot_credit_cost === 1 ? 'pilot credit' : 'pilot credits'}
                </Typography>
              </Typography></div>
              <div><Typography component="dt" variant="body2">Licensed version</Typography><Typography component="dd" variant="body2">{offer.licensed_major_version}.x</Typography></div>
              <div><Typography component="dt" variant="body2">Update rights</Typography><Typography component="dd" variant="body2">{offer.update_rights ? 'Included' : 'Not included'}</Typography></div>
              <div><Typography component="dt" variant="body2">Earlier versions</Typography><Typography component="dd" variant="body2">
                {offer.fallback_major_versions.length ? offer.fallback_major_versions.map(version => `${version}.x`).join(', ') : 'Not included'}
              </Typography></div>
            </Box>
            {showSwitch && <Button ref={switchElement} onClick={() => setChoosingAccount(true)} disabled={opening}
              sx={{ ...quietButtonSx, alignSelf: 'flex-start' }}>{SIGN_IN_SWITCH}</Button>}
          </Stack>}
        </DialogContent>
        <DialogActions sx={dialogActionsSx}>
          <OnboardingAction>
            {!choosingAccount && <Button onClick={() => void openSetup('activation.confirm')} disabled={!canConfirm || opening}
              aria-describedby={offerFeedback ? `${feedbackId}-offer` : undefined} variant="contained" sx={buttonSx}>Activate for free</Button>}
            <Button onClick={dismissOffer} sx={quietButtonSx}>Not now</Button>
            {!choosingAccount && <Button ref={offerCodeButton} aria-haspopup="dialog" disabled={opening || !canRedeem || codeChecking}
              onClick={() => {
                if (!currentPresentation()) return;
                if (intentScope) dismissIntent(intentScope);
                promotedIntent.current = null;
                setCodeOpen(true);
              }} sx={quietButtonSx}>Use an activation code</Button>}
          </OnboardingAction>
        </DialogActions>
        {offerFeedback && <Box id={`${feedbackId}-offer`} role={noticeError ? 'alert' : undefined} sx={{ mt: 2 }}>
          <OnboardingStatus tone={noticeError ? 'error' : 'neutral'} live={!noticeError}>{offerFeedback}</OnboardingStatus>
        </Box>}
      </>}
    </Dialog>
  </Panel>;
}
