import { Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { EngineDependencyControls } from './EngineDependencyControls';
import { TextAnalysisExecutionControls } from './TextAnalysisExecutionControls';
import { Panel, SectionHeader, useRevealHold } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import type { EngineAcquisitionApi } from '../api/engineAcquisition';
import { usePermissions } from '../hooks/usePermissions';
import {
  nlpDependenciesApi, parseNlpDependencyPlan,
  type NlpDependenciesApi, type NlpDependencyCommand, type NlpDependencyObservation,
  type NlpDependencyPlan, type NlpDependencyProfile, type NlpDependencyStatus,
} from '../services/nlpDependenciesApi';
import type { TextAnalysisClient } from '../services/textAnalysisClient';
import { bridgeTransportStore } from '../store/transportStore';

interface Props { api?: NlpDependenciesApi; engineApi?: EngineAcquisitionApi; client?: TextAnalysisClient; creatorAccountId: string | null }
const number = new Intl.NumberFormat();
const formatBytes = (value: number) => `${number.format(value)} bytes`;
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
    && (plan.incremental_data_download_bytes !== 0 || plan.acquisition_temporary_bytes === 0);
}
function ProfileSupport({ profile }: { profile: NlpDependencyProfile }) {
  if (!hasProfileMetadata(profile)) return <Typography>Language and task support: not yet verified.</Typography>;
  return <>
    <Typography>Supported languages: {profile.languages!.join(', ')}.</Typography>
    <Typography>Supported tasks: {profile.tasks!.map((task) => taskNames[task]).join(', ')}.</Typography>
  </>;
}
function pendingAcquisition(status: NlpDependencyStatus) {
  const acquisitions = status.acquisitions.filter((item) => item.kind === 'acquire');
  return acquisitions.find((item) => item.in_flight || item.state === 'running')
    ?? acquisitions.find((item) => ['paused', 'interrupted'].includes(item.state));
}

/** Account-bound presentation. Changing accounts discards the prior account's view immediately. */
export function NlpDependencyControls({ api = nlpDependenciesApi, engineApi, client, creatorAccountId }: Props) {
  const { isCreator } = usePermissions();
  const transport = useSyncExternalStore(bridgeTransportStore.subscribe, bridgeTransportStore.getState, bridgeTransportStore.getState);
  const account = isCreator ? creatorAccountId : null;
  const binding = `${creatorAccountId}:${isCreator}:${transport.session?.connection_id ?? ''}:${transport.session?.bridge_session_id ?? ''}`;
  return <Stack spacing={2}>
    <EngineDependencyControls key={`engine:${binding}`} account={account} api={engineApi} />
    <DependencySession key={binding} api={api} account={account} />
    <TextAnalysisExecutionControls creatorAccountId={account} client={client} dependenciesApi={api} />
  </Stack>;
}

function DependencySession({ api, account }: { api: NlpDependenciesApi; account: string | null }) {
  const [status, setStatus] = useState<NlpDependencyStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [review, setReview] = useState<{ profile: NlpDependencyProfile; plan: NlpDependencyPlan } | null>(null);
  const [removal, setRemoval] = useState<NlpDependencyProfile | null>(null);
  const [operation, setOperation] = useState<NlpDependencyObservation | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
  useRevealHold(Boolean(account && !status && !error));
  const lifetime = useRef<AbortController | null>(null);
  const active = () => Boolean(account && !lifetime.current?.signal.aborted && bridgeTransportStore.getState().creatorAccountId === account);

  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    if (account) void api.get(controller.signal).then((value) => {
      if (!controller.signal.aborted && bridgeTransportStore.getState().creatorAccountId === account) {
        setStatus(value);
        const pending = pendingAcquisition(value);
        if (pending) { setOperation(pending); setOperationId(pending.id); }
      }
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [account, api]);

  useEffect(() => {
    if (!operationId || !account) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await api.observe(operationId, controller.signal);
        if (controller.signal.aborted || bridgeTransportStore.getState().creatorAccountId !== account) return;
        setOperation(value);
        if (value.in_flight || value.state === 'running') timer = setTimeout(() => { void poll(); }, 1000);
        else {
          const next = await api.get(controller.signal);
          if (!controller.signal.aborted && bridgeTransportStore.getState().creatorAccountId === account) setStatus(next);
        }
      } catch { if (!controller.signal.aborted) setError(true); }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [account, api, operationId, pollRevision]);

  const checkStatus = async () => {
    if (!active() || busy) return;
    setBusy(true); setError(false);
    try {
      const value = await api.get(lifetime.current!.signal);
      if (active()) {
        setStatus(value);
        const pending = pendingAcquisition(value);
        if (pending) { setOperation(pending); setOperationId(pending.id); setPollRevision((current) => current + 1); }
      }
    } catch { if (active()) setError(true); }
    finally { if (active()) setBusy(false); }
  };

  const run = async (command: NlpDependencyCommand, profile?: NlpDependencyProfile) => {
    if (!active() || busy) return;
    setBusy(true); setError(false);
    setReview(null);
    try {
      const response = await api.command(command, lifetime.current!.signal);
      if (!active()) return;
      if (command.action === 'plan' && profile) {
        const quote = parseNlpDependencyPlan(response.operation);
        const matching = response.status.profiles.filter((item) => item.profile_sha256 === quote.profile_sha256);
        if (!response.status.configured || quote.profile_sha256 !== profile.profile_sha256 || matching.length !== 1
          || matching[0].profile_id !== profile.profile_id || matching[0].revision !== profile.revision
          || !matchesPlan(matching[0], quote)) throw new Error('Profile changed');
        setReview({ profile: matching[0], plan: quote });
      }
      setStatus(response.status);
      if (command.action === 'acquire' && typeof response.operation === 'string') {
        setOperation(null); setOperationId(response.operation);
      }
      if (['pause', 'cancel', 'resume'].includes(command.action) && operationId) {
        const value = await api.observe(operationId, lifetime.current!.signal);
        if (active()) { setOperation(value); setPollRevision((current) => current + 1); }
      }
    } catch { if (active()) setError(true); }
    finally { if (active()) setBusy(false); }
  };
  const unavailable = !account || !status?.configured;
  const loading = Boolean(account && !status && !error);
  const feedback = [
    busy ? 'Updating text analysis settings…' : null,
    error ? 'Text analysis settings could not be updated. Your existing selection may still be active. Check status before trying again.' : null,
    loading ? 'Checking text analysis availability…' : null,
    !loading && unavailable ? 'Text analysis is unavailable until a qualified profile and compatible installed engine are available.' : null,
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
  const feedbackLine = <StatusLine id="dependency-feedback" text={feedback || null}
    summary={error ? 'Needs attention' : loading || busy ? 'Checking…' : unavailable ? 'Unavailable' : 'Status'}
    detailsOnly tone={error ? 'error' : 'secondary'} />;
  return (
    <Panel component="section" aria-label="Optional text analysis">
      <SectionHeader title="Optional text analysis" />
      <Typography>Find topics and mentions in messages. Setup is optional; other analytics remain available.</Typography>
      <Typography>Language and task support are profile-specific. Review the profile before choosing it.</Typography>
      {account && <Button disabled={busy} onClick={() => { void checkStatus(); }}>Check status</Button>}
      {!loading && unavailable
        ? <Box data-journey-state="textanalysis.unavailable">{feedbackLine}</Box>
        : <Box>{feedbackLine}</Box>}
      {!loading && !unavailable && status && (
        <Stack spacing={2} data-journey-state="textanalysis.controls">
          <Button disabled={busy} onClick={() => { void run({ action: 'refresh' }); }}>Check available profiles</Button>
          {status.profiles.length === 0 && <Typography>No qualified profiles are currently listed.</Typography>}
          {status.profiles.map((profile) => (
            <Stack spacing={1} key={profile.profile_sha256}>
              <Typography component="h3" variant="subtitle1">{profile.profile_id} · {profile.revision}</Typography>
              <Typography>{profile.withdrawn ? 'Unavailable profile' : profile.prepared && profile.installed ? 'Prepared for this session; available offline while the engine is ready' : profile.installed ? 'Files installed; preparation required before use' : 'Not installed'}</Typography>
              <ProfileSupport profile={profile} />
              <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
                <Button disabled={busy || profile.withdrawn || !profile.catalogued} onClick={() => { void run({ action: 'plan', profile_id: profile.profile_id, revision: profile.revision }, profile); }}>Review download for {profile.profile_id}</Button>
                {profile.installed && <Button disabled={busy || profile.prepared || profile.withdrawn} onClick={() => { void run({ action: 'prepare', profile_sha256: profile.profile_sha256 }); }}>Prepare {profile.profile_id}</Button>}
                {profile.installed && <Button disabled={busy} onClick={() => setRemoval(profile)}>Remove {profile.profile_id}</Button>}
              </Stack>
            </Stack>
          ))}
          <Typography>Manage this account’s processing in the processing controls below. Download controls apply to shared files on this device.</Typography>
        </Stack>
      )}
      {operation && <Stack role="status" spacing={1}>
        <Typography>Download: {operation.profile_id ?? 'selected profile'} · {operation.revision ?? 'unknown revision'}</Typography>
        <Typography>Downloads are shared on this device. Download controls do not change this account’s processing settings.</Typography>
        <Typography>{operationState}. {number.format(operation.verified_files)} verified files retained.</Typography>
        <Typography>Resume reuses verified files. The interrupted file restarts; this is not byte-level resume. Cancelling may discard staged files.</Typography>
        <Stack direction="row" spacing={1}>
          <Button disabled={busy || unavailable || operation.state !== 'running'} onClick={() => { void run({ action: 'pause', operation_id: operation.id }); }}>Pause download</Button>
          <Button disabled={busy || unavailable || !['paused', 'interrupted'].includes(operation.state) || operation.in_flight} onClick={() => { void run({ action: 'resume', operation_id: operation.id }); }}>Resume download</Button>
          <Button disabled={busy || unavailable || !['running', 'paused', 'interrupted'].includes(operation.state)} onClick={() => { void run({ action: 'cancel', operation_id: operation.id }); }}>Cancel download</Button>
        </Stack>
      </Stack>}
      <Dialog open={review !== null} onClose={() => setReview(null)} aria-labelledby="text-analysis-cost-title">
        <DialogTitle id="text-analysis-cost-title">Review download costs</DialogTitle>
        <DialogContent data-journey-state="textanalysis.cost_review">
          {review && <Stack spacing={1}>
            <Typography>This plan downloads model data. Review Engine installation separately. Full-profile totals are declared budgets.</Typography>
            <Typography>{review.profile.profile_id} · {review.profile.revision}</Typography>
            <ProfileSupport profile={review.profile} />
            {([
              ['Download now', review.plan.incremental_data_download_bytes], ['Complete data download', review.plan.data_download_bytes],
              ['Profile-declared complete download budget', review.plan.declared_full_artifact_download_bytes], ['Profile metadata already retrieved', review.plan.profile_metadata_bytes],
              ['Installed size', review.plan.installed_bytes], ['Temporary disk space', review.plan.acquisition_temporary_bytes],
              ['Rollback space', review.plan.rollback_bytes], ['Peak memory during transition', review.plan.transition_peak_memory_bytes],
            ] as const).map(([label, value]) => <Typography key={label}>{label}: {formatBytes(value)}</Typography>)}
            <Typography>Processing threads: {number.format(review.plan.inference_threads)}</Typography>
            <Typography>Downloading does not enable processing. Prepare the installed files and explicitly choose the profile afterward.</Typography>
          </Stack>}
        </DialogContent>
        <DialogActions><Button onClick={() => setReview(null)}>Back</Button><Button disabled={busy || unavailable || !reviewMatchesStatus} onClick={() => { if (review && reviewMatchesStatus) void run({ action: 'acquire', profile_id: review.profile.profile_id, revision: review.profile.revision, operation_id: review.plan.operation_id }); }}>Download reviewed files</Button></DialogActions>
      </Dialog>
      <Dialog open={removal !== null} onClose={() => setRemoval(null)} aria-labelledby="text-analysis-remove-title">
        <DialogTitle id="text-analysis-remove-title">Remove installed profile?</DialogTitle>
        <DialogContent>Files still used by an account or an active operation are retained. Other analytics and stored messages are preserved.</DialogContent>
        <DialogActions><Button onClick={() => setRemoval(null)}>Keep profile</Button><Button disabled={busy || unavailable} onClick={() => { if (removal) void run({ action: 'remove', profile_id: removal.profile_id, revision: removal.revision }); setRemoval(null); }}>Remove profile</Button></DialogActions>
      </Dialog>
    </Panel>
  );
}
