import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';

import { Panel, SectionHeader } from './ui';
import { usePermissions } from '../hooks/usePermissions';
import {
  nlpDependenciesApi, parseNlpDependencyPlan,
  type NlpDependenciesApi, type NlpDependencyCommand, type NlpDependencyObservation,
  type NlpDependencyPlan, type NlpDependencyProfile, type NlpDependencyStatus,
} from '../services/nlpDependenciesApi';
import { bridgeTransportStore } from '../store/transportStore';

interface Props { api?: NlpDependenciesApi; creatorAccountId: string | null }
const number = new Intl.NumberFormat();
const formatBytes = (value: number) => `${number.format(value)} bytes`;
function pendingAcquisition(status: NlpDependencyStatus) {
  const acquisitions = status.acquisitions.filter((item) => item.kind === 'acquire');
  return acquisitions.find((item) => item.in_flight || item.state === 'running')
    ?? acquisitions.find((item) => ['paused', 'interrupted'].includes(item.state));
}

/** Account-bound presentation. Changing accounts discards the prior account's view immediately. */
export function NlpDependencyControls({ api = nlpDependenciesApi, creatorAccountId }: Props) {
  const { isCreator } = usePermissions();
  return <DependencySession key={`${creatorAccountId}:${isCreator}`} api={api} account={isCreator ? creatorAccountId : null} />;
}

function DependencySession({ api, account }: { api: NlpDependenciesApi; account: string | null }) {
  const [status, setStatus] = useState<NlpDependencyStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [review, setReview] = useState<{ profile: NlpDependencyProfile; plan: NlpDependencyPlan } | null>(null);
  const [removal, setRemoval] = useState<NlpDependencyProfile | 'account' | null>(null);
  const [operation, setOperation] = useState<NlpDependencyObservation | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
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
      setStatus(response.status);
      if (command.action === 'plan' && profile) {
        const quote = parseNlpDependencyPlan(response.operation);
        if (quote.profile_sha256 !== profile.profile_sha256) throw new Error('Profile changed');
        setReview({ profile, plan: quote });
      }
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
  const operationState = operation?.state === 'complete' ? 'Download complete' : operation?.state === 'paused' ? 'Download paused'
    : operation?.state === 'cancelled' ? 'Download cancelled' : operation?.state === 'running' ? 'Downloading' : 'Download needs attention';
  return (
    <Panel component="section" aria-label="Optional text analysis">
      <SectionHeader title="Optional text analysis" />
      <Typography>Find topics and mentions in messages. Setup is optional; other analytics remain available.</Typography>
      <Typography>Language support is profile-specific. Review English support before choosing a profile.</Typography>
      {account && <Button disabled={busy} onClick={() => { void checkStatus(); }}>Check status</Button>}
      {busy && <Typography role="status">Updating text analysis settings…</Typography>}
      {error && <Alert severity="error">Text analysis settings could not be updated. Your existing selection may still be active. Check status before trying again.</Alert>}
      {unavailable && status?.selected && <Typography>Saved selection: {status.selected.profile_id} · {status.selected.revision}. Text processing is currently unavailable.</Typography>}
      {account && !status && !error ? <Typography role="status">Checking text analysis availability…</Typography> : unavailable ? <Alert severity="info" data-journey-state="textanalysis.unavailable">Text analysis is unavailable until a qualified profile and compatible installed engine are available.</Alert> : (
        <Stack spacing={2} data-journey-state="textanalysis.controls">
          <Button disabled={busy} onClick={() => { void run({ action: 'refresh' }); }}>Check available profiles</Button>
          {status.profiles.length === 0 && <Typography>No qualified profiles are currently listed.</Typography>}
          {status.profiles.map((profile) => (
            <Stack spacing={1} key={profile.profile_sha256}>
              <Typography component="h3" variant="subtitle1">{profile.profile_id} · {profile.revision}</Typography>
              <Typography>{profile.withdrawn ? 'Unavailable profile' : profile.prepared && profile.installed ? 'Prepared for this session; available offline while the engine is ready' : profile.installed ? 'Files installed; preparation required before use' : 'Not installed'}</Typography>
              <Typography>English support: {profile.languages === null ? 'not yet verified' : profile.languages.includes('en') ? 'supported' : 'not supported'}. Other supported languages: {profile.languages?.filter((language) => language !== 'en').join(', ') || 'none listed'}.</Typography>
              <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
                <Button disabled={busy || profile.withdrawn || !profile.catalogued} onClick={() => { void run({ action: 'plan', profile_id: profile.profile_id, revision: profile.revision }, profile); }}>Review download for {profile.profile_id}</Button>
                {profile.installed && <Button disabled={busy || profile.prepared || profile.withdrawn} onClick={() => { void run({ action: 'prepare', profile_sha256: profile.profile_sha256 }); }}>Prepare {profile.profile_id}</Button>}
                <Button disabled={busy || !profile.prepared || !profile.installed || profile.withdrawn || !profile.catalogued} onClick={() => { void run({ action: 'select', profile_id: profile.profile_id, revision: profile.revision }); }}>Use {profile.profile_id}</Button>
                {profile.installed && <Button disabled={busy} onClick={() => setRemoval(profile)}>Remove {profile.profile_id}</Button>}
              </Stack>
            </Stack>
          ))}
          {status.selected && <Stack spacing={1}>
            <Typography>Selected: {status.selected.profile_id} · {status.selected.revision}. Processing: {status.selected.mode === 'enabled' ? 'enabled' : status.selected.mode === 'paused' ? 'paused' : status.selected.mode === 'removed' ? 'removed for this account' : 'unavailable'}.</Typography>
            <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
              <Button disabled={busy || status.selected.mode !== 'enabled'} onClick={() => { void run({ action: 'account_pause' }); }}>Pause processing</Button>
              <Button disabled={busy || status.selected.mode !== 'paused'} onClick={() => { void run({ action: 'account_enable' }); }}>Enable processing</Button>
              <Button disabled={busy} onClick={() => { void run({ action: 'rollback' }); }}>Use previous profile</Button>
              <Button disabled={busy || status.selected.mode === 'removed'} onClick={() => setRemoval('account')}>Remove text analysis for this account</Button>
            </Stack>
            <Typography>Processing controls apply to this account. Pausing a download does not pause processing.</Typography>
          </Stack>}
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
            <Typography>This plan downloads data files. Engine installation must be available separately.</Typography>
            {([
              ['Download now', review.plan.incremental_data_download_bytes], ['Complete data download', review.plan.data_download_bytes],
              ['Complete download including engine and runtime', review.plan.declared_full_artifact_download_bytes], ['Profile metadata already retrieved', review.plan.profile_metadata_bytes],
              ['Installed size', review.plan.installed_bytes], ['Temporary disk space', review.plan.acquisition_temporary_bytes],
              ['Rollback space', review.plan.rollback_bytes], ['Peak memory during transition', review.plan.transition_peak_memory_bytes],
            ] as const).map(([label, value]) => <Typography key={label}>{label}: {formatBytes(value)}</Typography>)}
            <Typography>Processing threads: {number.format(review.plan.inference_threads)}</Typography>
            <Typography>Downloading does not enable processing. Prepare the installed files and explicitly choose the profile afterward.</Typography>
          </Stack>}
        </DialogContent>
        <DialogActions><Button onClick={() => setReview(null)}>Back</Button><Button disabled={busy || unavailable || !review} onClick={() => { if (review) void run({ action: 'acquire', profile_id: review.profile.profile_id, revision: review.profile.revision, operation_id: review.plan.operation_id }); }}>Download reviewed files</Button></DialogActions>
      </Dialog>
      <Dialog open={removal !== null} onClose={() => setRemoval(null)} aria-labelledby="text-analysis-remove-title">
        <DialogTitle id="text-analysis-remove-title">{removal === 'account' ? 'Remove text analysis for this account?' : 'Remove installed profile?'}</DialogTitle>
        <DialogContent>{removal === 'account' && 'Text processing stops for this account. '}Files still used by an account or an active operation are retained. Other analytics and stored messages are preserved.</DialogContent>
        <DialogActions><Button onClick={() => setRemoval(null)}>Keep profile</Button><Button disabled={busy || unavailable} onClick={() => { if (removal === 'account') void run({ action: 'remove' }); else if (removal) void run({ action: 'remove', profile_id: removal.profile_id, revision: removal.revision }); setRemoval(null); }}>{removal === 'account' ? 'Remove for this account' : 'Remove profile'}</Button></DialogActions>
      </Dialog>
    </Panel>
  );
}
