import ManageSearchOutlined from '@mui/icons-material/ManageSearchOutlined';
import { Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Divider, Stack, Typography } from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { EngineDependencyControls } from './EngineDependencyControls';
import { TextAnalysisExecutionControls } from './TextAnalysisExecutionControls';
import { TextAnalysisReissueControls } from './TextAnalysisReissueControls';
import { Panel, SectionHeader, SessionHold, useReportedStatus, useRevealHold, type SectionMenuItem, type SectionStatus } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import type { EngineAcquisitionApi } from '../api/engineAcquisition';
import { usePermissions } from '../hooks/usePermissions';
import {
  nlpDependenciesApi, NlpDependenciesHttpError, parseNlpDependencyPlan,
  type NlpDependenciesApi, type NlpDependencyCommand, type NlpDependencyObservation,
  type NlpDependencyPlan, type NlpDependencyProfile, type NlpDependencyStatus,
} from '../services/nlpDependenciesApi';
import type { TextAnalysisClient } from '../services/textAnalysisClient';
import type { TextAnalysisReissueApi } from '../services/textAnalysisReissueApi';
import { bridgeTransportStore } from '../store/transportStore';

interface Props { api?: NlpDependenciesApi; engineApi?: EngineAcquisitionApi; client?: TextAnalysisClient; reissueApi?: TextAnalysisReissueApi; creatorAccountId: string | null }
const DEPENDENCY_STATUS_LABELS = ['Checking…', 'Needs attention', 'Unavailable', 'Ready', 'Installed', 'Not installed'] as const;
const number = new Intl.NumberFormat();
const gibibyte = 1024 ** 3;
const gibibytes = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
function formatBytes(value: number) {
  const exact = `${number.format(value)} bytes`;
  return value < gibibyte ? exact : `${gibibytes.format(value / gibibyte)} GiB (${exact})`;
}
const taskNames: Record<string, string> = { message_topics: 'Message topics', concept_mentions: 'Exact mentions' };
function hasProfileMetadata(profile: NlpDependencyProfile) {
  const resources = profile.resources;
  return profile.catalogued && !profile.withdrawn && profile.languages !== null && profile.languages.length > 0
    && new Set(profile.languages).size === profile.languages.length
    && profile.languages.every((language) => /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(language))
    && profile.tasks !== null && profile.tasks.length >= 1 && profile.tasks.length <= 2
    && new Set(profile.tasks).size === profile.tasks.length
    && profile.tasks.every((task) => Object.hasOwn(taskNames, task))
    && resources !== null
    && ['download_bytes', 'installed_bytes', 'transition_peak_memory_bytes', 'inference_threads'].every((key) => Number.isSafeInteger(resources[key]) && Number(resources[key]) > 0)
    && ['rollback_bytes', 'temporary_bytes'].every((key) => Number.isSafeInteger(resources[key]) && Number(resources[key]) >= 0);
}
function matchesPlan(profile: NlpDependencyProfile, plan: NlpDependencyPlan) {
  const resources = profile.resources;
  return profile.catalogued && !profile.withdrawn && hasProfileMetadata(profile) && resources !== null
    && profile.profile_sha256 === plan.profile_sha256
    && resources.download_bytes === plan.declared_full_artifact_download_bytes
    && resources.installed_bytes === plan.installed_bytes && resources.rollback_bytes === plan.rollback_bytes
    && resources.transition_peak_memory_bytes === plan.transition_peak_memory_bytes
    && resources.inference_threads === plan.inference_threads
    && plan.profile_metadata_bytes > 0 && plan.data_download_bytes > 0
    && plan.incremental_data_download_bytes <= plan.data_download_bytes
    && plan.data_download_bytes <= plan.declared_full_artifact_download_bytes
    && plan.acquisition_temporary_bytes <= Number(resources.temporary_bytes)
    && (plan.required_free_space_bytes === undefined || plan.required_free_space_bytes >= plan.acquisition_temporary_bytes)
    && (plan.incremental_data_download_bytes !== 0 || plan.acquisition_temporary_bytes === 0);
}
function ProfileSupport({ profile }: { profile: NlpDependencyProfile }) {
  if (!hasProfileMetadata(profile)) {
    return <Typography variant="body2" sx={{ color: 'text.secondary' }}>Language and task support: not yet verified.</Typography>;
  }
  return <>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>Supported languages: {profile.languages!.join(', ')}.</Typography>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>Supported tasks: {profile.tasks!.map((task) => taskNames[task]).join(', ')}.</Typography>
  </>;
}
function pendingAcquisition(status: NlpDependencyStatus) {
  const acquisitions = status.acquisitions.filter((item) => item.kind === 'acquire');
  return acquisitions.find((item) => item.in_flight || item.state === 'running')
    ?? acquisitions.find((item) => ['paused', 'interrupted'].includes(item.state));
}

type TextAnalysisPart = 'reissue' | 'engine' | 'dependencies' | 'processing';
const TEXT_ANALYSIS_PARTS: readonly TextAnalysisPart[] = ['reissue', 'engine', 'dependencies', 'processing'];

/** Account-bound presentation. Changing accounts discards the prior account's view immediately. */
export function NlpDependencyControls({ api = nlpDependenciesApi, engineApi, client, reissueApi, creatorAccountId, onAttentionChange, onAvailabilityChange }: Props & {
  /** Reports whether any text analysis section needs attention, for the Settings index. */
  onAttentionChange?: (attention: boolean) => void;
  /** Reports whether this installation offers text analysis. Every part loads either way. */
  onAvailabilityChange?: (available: boolean) => void;
}) {
  const { isCreator } = usePermissions();
  const transport = useSyncExternalStore(bridgeTransportStore.subscribe, bridgeTransportStore.getState, bridgeTransportStore.getState);
  const [apiBinding, setApiBinding] = useState({ api, revision: 0 });
  if (apiBinding.api !== api) setApiBinding({ api, revision: apiBinding.revision + 1 });
  const [attention, setAttention] = useState<ReadonlySet<TextAnalysisPart>>(() => new Set());
  const reporters = useMemo(() => Object.fromEntries(TEXT_ANALYSIS_PARTS.map((part) => [part, (status: SectionStatus | null) => {
    setAttention((current) => {
      const needs = status?.label === 'Needs attention';
      if (needs === current.has(part)) return current;
      const next = new Set(current);
      if (needs) next.add(part); else next.delete(part);
      return next;
    });
  }])) as Record<TextAnalysisPart, (status: SectionStatus | null) => void>, []);
  const [available, setAvailable] = useState(false);
  useEffect(() => { onAttentionChange?.(available && attention.size > 0); }, [attention, available, onAttentionChange]);
  useEffect(() => () => onAttentionChange?.(false), [onAttentionChange]);
  useEffect(() => { onAvailabilityChange?.(available); }, [available, onAvailabilityChange]);
  useEffect(() => () => onAvailabilityChange?.(false), [onAvailabilityChange]);
  const account = isCreator ? creatorAccountId : null;
  const binding = `${creatorAccountId}:${isCreator}:${transport.session?.connection_id ?? ''}:${transport.session?.bridge_session_id ?? ''}`;
  const dependencyKey = `${binding}:${apiBinding.revision}`;
  const [recoveryMenu, setRecoveryMenu] = useState<readonly SectionMenuItem[] | null>(null);

  // A reconnect starts a new dependency session, which reads its status again. Until that read settles the
  // panel keeps its last settled height, so the parts below it never move.
  const [settledDependencies, setSettledDependencies] = useState<string | null>(null);
  const settle = useCallback(() => setSettledDependencies(dependencyKey), [dependencyKey]);

  // The view decides visibility. Recovery comes last and shows only while one is under way. It starts from the
  // text analysis menu, because only someone moving from another computer needs it.
  return <Stack spacing={0}>
    <Stack spacing={0} divider={<Divider />}>
      <EngineDependencyControls key={`engine:${binding}`} account={account} api={engineApi} onStatus={reporters.engine} />
      <SessionHold scope={dependencyKey} identity={`${creatorAccountId}:${isCreator}`} settled={settledDependencies === dependencyKey}>
        <DependencySession key={dependencyKey} api={api} account={account} onStatus={reporters.dependencies}
          onAvailability={setAvailable} onSettled={settle} menu={recoveryMenu} />
      </SessionHold>
      <TextAnalysisExecutionControls creatorAccountId={account} client={client} dependenciesApi={api} onStatus={reporters.processing} />
    </Stack>
    <TextAnalysisReissueControls creatorAccountId={account} api={reissueApi} onStatus={reporters.reissue} onMenu={setRecoveryMenu} />
  </Stack>;
}

function DependencySession({ api, account, onStatus, onAvailability, onSettled, menu }: {
  api: NlpDependenciesApi; account: string | null; onStatus?: (status: SectionStatus | null) => void;
  /** True once a read shows that this installation has text analysis configured. */
  onAvailability?: (available: boolean) => void;
  /** Called once this session's first read has settled. */
  onSettled?: () => void;
  menu?: readonly SectionMenuItem[] | null;
}) {
  const [status, setStatus] = useState<NlpDependencyStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [readFailure, setReadFailure] = useState<'authentication' | 'authorization' | 'unavailable' | null>(null);
  const [review, setReview] = useState<{ profile: NlpDependencyProfile; plan: NlpDependencyPlan } | null>(null);
  const [removal, setRemoval] = useState<NlpDependencyProfile | null>(null);
  const [operation, setOperation] = useState<NlpDependencyObservation | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
  useRevealHold(Boolean(account && !status && !error));
  const lifetime = useRef<AbortController | null>(null);
  const requestVersion = useRef(0);
  const active = (signal: AbortSignal, version: number) => Boolean(account && !signal.aborted
    && version === requestVersion.current && bridgeTransportStore.getState().creatorAccountId === account);
  const failRead = (cause: unknown) => {
    setError(true);
    setReadFailure(cause instanceof NlpDependenciesHttpError && cause.status === 401 ? 'authentication'
      : cause instanceof NlpDependenciesHttpError && cause.status === 403 ? 'authorization' : 'unavailable');
    setReview(null);
    setRemoval(null);
  };

  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    const version = ++requestVersion.current;
    if (account) void api.get(controller.signal).then((value) => {
      if (active(controller.signal, version)) {
        setStatus(value);
        const pending = pendingAcquisition(value);
        if (pending) { setOperation(pending); setOperationId(pending.id); }
      }
    }).catch((cause: unknown) => { if (active(controller.signal, version)) failRead(cause); });
    return () => controller.abort();
  }, [account, api]);

  useEffect(() => {
    if (!operationId || !account || busy || readFailure !== null) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const version = requestVersion.current;
    const poll = async () => {
      if (!active(controller.signal, version)) return;
      try {
        const value = await api.observe(operationId, controller.signal);
        if (!active(controller.signal, version)) return;
        setOperation(value);
        if (value.in_flight || value.state === 'running') timer = setTimeout(() => { void poll(); }, 1000);
        else {
          const next = await api.get(controller.signal);
          if (active(controller.signal, version)) setStatus(next);
        }
      } catch (cause) { if (active(controller.signal, version)) failRead(cause); }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [account, api, operationId, pollRevision, busy, readFailure]);

  const checkStatus = async () => {
    const signal = lifetime.current?.signal;
    if (!signal || !active(signal, requestVersion.current) || busy) return;
    const version = ++requestVersion.current;
    setBusy(true); setError(false);
    try {
      const value = await api.get(signal);
      if (active(signal, version)) {
        setStatus(value);
        setReadFailure(null);
        const pending = pendingAcquisition(value);
        if (pending) { setOperation(pending); setOperationId(pending.id); setPollRevision((current) => current + 1); }
        else { setOperation(null); setOperationId(null); }
      }
    } catch (cause) { if (active(signal, version)) failRead(cause); }
    finally { if (active(signal, version)) setBusy(false); }
  };

  const run = async (command: NlpDependencyCommand, profile?: NlpDependencyProfile) => {
    const signal = lifetime.current?.signal;
    if (!signal || !active(signal, requestVersion.current) || busy || readFailure !== null) return;
    const version = ++requestVersion.current;
    setBusy(true); setError(false);
    setReview(null);
    try {
      const response = await api.command(command, signal);
      if (!active(signal, version)) return;
      if (command.action === 'plan' && profile) {
        const quote = parseNlpDependencyPlan(response.operation);
        const matching = response.status.profiles.filter((item) => item.profile_sha256 === quote.profile_sha256);
        if (!response.status.configured || quote.profile_sha256 !== profile.profile_sha256 || matching.length !== 1
          || matching[0].profile_id !== profile.profile_id || matching[0].revision !== profile.revision
          || !matchesPlan(matching[0], quote)) throw new Error('Profile changed');
        setReview({ profile: matching[0], plan: quote });
      }
      setStatus(response.status);
      setReadFailure(null);
      if (command.action === 'acquire' && typeof response.operation === 'string') {
        setOperation(null); setOperationId(response.operation);
      }
      if (['pause', 'cancel', 'resume'].includes(command.action) && operationId) {
        const value = await api.observe(operationId, signal);
        if (active(signal, version)) { setOperation(value); setPollRevision((current) => current + 1); }
      }
    } catch (cause) { if (active(signal, version)) failRead(cause); }
    finally { if (active(signal, version)) setBusy(false); }
  };
  const unavailable = !account || !status?.configured || readFailure !== null;
  const loading = Boolean(account && !status && !error);
  const configured = Boolean(account && status?.configured);
  // Only a completed read decides availability. A reload or a failed read keeps the last answer, so a reconnect
  // neither hides and shows the section nor hides its failure and any recovery under way.
  const decided = !account || (status !== null && readFailure === null);
  useEffect(() => {
    if (loading) return;
    if (decided) onAvailability?.(configured);
    onSettled?.();
  }, [configured, decided, loading, onAvailability, onSettled]);
  const feedback = [
    busy ? 'Updating text analysis settings…' : null,
    error ? 'Text analysis settings could not be updated. Your existing selection may still be active. Check status before trying again.' : null,
    readFailure === 'authentication' ? 'Your sign-in could not be verified. Sign in again before checking status. Current download progress and readiness could not be verified.' : null,
    readFailure === 'authorization' ? 'Access to text analysis settings was denied. Check your sign-in and permissions, then check status. Current download progress and readiness could not be verified.' : null,
    readFailure === 'unavailable' && operation ? 'Current download progress and readiness are unavailable. Check status to try reading them again.' : null,
    loading ? 'Checking text analysis availability…' : null,
    !loading && unavailable && readFailure === null ? 'Text analysis is unavailable until a qualified profile and compatible installed engine are available.' : null,
  ].filter(Boolean).join(' ');
  const reviewMatchesStatus = Boolean(review && status?.configured
    && status.profiles.filter((profile) => profile.profile_sha256 === review.profile.profile_sha256).length === 1
    && status.profiles.filter((profile) =>
    profile.profile_id === review.profile.profile_id && profile.revision === review.profile.revision
    && profile.profile_sha256 === review.profile.profile_sha256 && matchesPlan(profile, review.plan)
    && JSON.stringify(profile.languages) === JSON.stringify(review.profile.languages)
    && JSON.stringify(profile.tasks) === JSON.stringify(review.profile.tasks)
    && profile.resources?.temporary_bytes === review.profile.resources?.temporary_bytes).length === 1);
  const operationState = operation?.state === 'complete' ? 'Download complete' : operation?.state === 'paused' ? 'Download paused'
    : operation?.state === 'cancelled' ? 'Download cancelled' : operation?.state === 'running' ? 'Downloading' : 'Download needs attention';
  const headerStatus: SectionStatus<(typeof DEPENDENCY_STATUS_LABELS)[number]> = loading || busy
    ? { label: 'Checking…', tone: 'default' }
    : error || readFailure !== null
      ? { label: 'Needs attention', tone: 'warning' }
      : unavailable
        ? { label: 'Unavailable', tone: 'default' }
        : status?.profiles.some((item) => item.installed && item.prepared)
          ? { label: 'Ready', tone: 'success' }
          : status?.profiles.some((item) => item.installed)
            ? { label: 'Installed', tone: 'info' }
            : { label: 'Not installed', tone: 'default' };
  useReportedStatus(onStatus, headerStatus);
  const feedbackLine = <StatusLine id="dependency-feedback" text={feedback || null}
    summary={error ? 'Needs attention' : loading || busy ? 'Checking…' : unavailable ? 'Unavailable' : 'Status'}
    detailsOnly quietSummary tone={error ? 'error' : 'secondary'} />;
  return (
    <Panel component="section" aria-label="Optional text analysis">
      <SectionHeader
        icon={<ManageSearchOutlined />}
        title="Optional text analysis"
        summary="Find topics and mentions in your messages."
        status={headerStatus}
        statusLabels={DEPENDENCY_STATUS_LABELS}
        menu={menu ?? undefined}
      />
      {(account || readFailure === 'authentication') && (
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          {account && <Button size="small" variant="outlined" disabled={busy} onClick={() => { void checkStatus(); }}>Check status</Button>}
          {/* Always present, so a reload never rewraps the row. */}
          {account && (
            <Button size="small" variant="outlined" disabled={busy || loading || unavailable || !status} onClick={() => { void run({ action: 'refresh' }); }}>Check available profiles</Button>
          )}
          {readFailure === 'authentication' && <Button size="small" variant="outlined" component="a" href="/">Sign in again</Button>}
        </Stack>
      )}
      {!loading && unavailable
        ? <Box data-journey-state="textanalysis.unavailable">{feedbackLine}</Box>
        : <Box>{feedbackLine}</Box>}
      {!loading && !unavailable && status && (
        <Stack spacing={1.5} data-journey-state="textanalysis.controls">
          {status.profiles.length === 0 && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>No qualified profiles are currently listed.</Typography>
          )}
          {status.profiles.map((profile) => (
            <Stack
              spacing={1}
              key={profile.profile_sha256}
              sx={{
                bgcolor: 'surface.subtle',
                border: '1px solid',
                borderColor: 'divider',
                borderRadius: 1.5,
                p: 2,
              }}
            >
              <Typography component="h3" variant="subtitle2">{profile.profile_id} · {profile.revision}</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                {profile.withdrawn ? 'Unavailable profile' : profile.prepared && profile.installed ? 'Prepared for this session; available offline while the engine is ready' : profile.installed ? 'Files installed; preparation required before use' : 'Not installed'}
              </Typography>
              <ProfileSupport profile={profile} />
              <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap', pt: 0.5 }}>
                <Button size="small" variant="outlined" disabled={busy || profile.withdrawn || !profile.catalogued} onClick={() => { void run({ action: 'plan', profile_id: profile.profile_id, revision: profile.revision }, profile); }}>Review download for {profile.profile_id}</Button>
                {profile.installed && <Button size="small" variant="outlined" disabled={busy || profile.prepared || profile.withdrawn} onClick={() => { void run({ action: 'prepare', profile_sha256: profile.profile_sha256 }); }}>Prepare {profile.profile_id}</Button>}
                {profile.installed && <Button size="small" color="error" disabled={busy} onClick={() => setRemoval(profile)}>Remove {profile.profile_id}</Button>}
              </Stack>
            </Stack>
          ))}
        </Stack>
      )}
      {operation && (
        <Stack
          role="status"
          spacing={1}
          sx={{
            bgcolor: 'surface.subtle',
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1.5,
            p: 2,
          }}
        >
          <Typography variant="subtitle2">Download: {operation.profile_id ?? 'selected profile'} · {operation.revision ?? 'unknown revision'}</Typography>
          <Typography variant="body2">{readFailure !== null
            ? `Last confirmed progress: ${number.format(operation.verified_files)} verified files. Current state is unknown.`
            : `${operationState}. ${number.format(operation.verified_files)} verified files retained.`}</Typography>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap', pt: 0.5 }}>
            <Button size="small" variant="outlined" disabled={busy || unavailable || operation.state !== 'running'} onClick={() => { void run({ action: 'pause', operation_id: operation.id }); }}>Pause download</Button>
            <Button size="small" variant="outlined" disabled={busy || unavailable || !['paused', 'interrupted'].includes(operation.state) || operation.in_flight} onClick={() => { void run({ action: 'resume', operation_id: operation.id }); }}>Resume download</Button>
            <Button size="small" color="error" disabled={busy || unavailable || !['running', 'paused', 'interrupted'].includes(operation.state)} onClick={() => { void run({ action: 'cancel', operation_id: operation.id }); }}>Cancel download</Button>
          </Stack>
        </Stack>
      )}
      <Dialog open={review !== null} onClose={() => setReview(null)} aria-labelledby="text-analysis-cost-title">
        <DialogTitle id="text-analysis-cost-title">Review download costs</DialogTitle>
        <DialogContent data-journey-state="textanalysis.cost_review">
          {review && <Stack spacing={1}>
            <Typography>{review.profile.profile_id} · {review.profile.revision}</Typography>
            <ProfileSupport profile={review.profile} />
            {([
              ['Download now', review.plan.incremental_data_download_bytes], ['Complete data download', review.plan.data_download_bytes],
              ['Profile-declared complete download budget', review.plan.declared_full_artifact_download_bytes], ['Profile metadata already retrieved', review.plan.profile_metadata_bytes],
              ['Installed size', review.plan.installed_bytes], ['Estimated temporary space for model files', review.plan.acquisition_temporary_bytes],
              ['Minimum free space to start', review.plan.required_free_space_bytes],
              ['Rollback space', review.plan.rollback_bytes], ['Peak memory during transition', review.plan.transition_peak_memory_bytes],
            ] as const).map(([label, value]) => value !== undefined && <Typography key={label}>{label}: {formatBytes(value)}</Typography>)}
            <Typography>Processing threads: {number.format(review.plan.inference_threads)}</Typography>
            <Typography>After the download, prepare the files and choose this profile for your account.</Typography>
          </Stack>}
        </DialogContent>
        <DialogActions><Button onClick={() => setReview(null)}>Back</Button><Button disabled={busy || unavailable || !reviewMatchesStatus} onClick={() => { if (review && reviewMatchesStatus) void run({ action: 'acquire', profile_id: review.profile.profile_id, revision: review.profile.revision, operation_id: review.plan.operation_id }); }}>Download reviewed files</Button></DialogActions>
      </Dialog>
      <Dialog open={removal !== null} onClose={() => setRemoval(null)} aria-labelledby="text-analysis-remove-title">
        <DialogTitle id="text-analysis-remove-title">Remove installed profile?</DialogTitle>
        <DialogActions><Button onClick={() => setRemoval(null)}>Keep profile</Button><Button disabled={busy || unavailable} onClick={() => { if (removal) void run({ action: 'remove', profile_id: removal.profile_id, revision: removal.revision }); setRemoval(null); }}>Remove profile</Button></DialogActions>
      </Dialog>
    </Panel>
  );
}
