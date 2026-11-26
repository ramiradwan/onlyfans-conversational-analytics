import { Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Stack, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useStore } from 'zustand';

import { Panel, SectionHeader, type SectionStatus } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import type { EnrollmentCommand } from '../analytics/textAnalysisContract';
import { nlpDependenciesApi, type NlpDependenciesApi, type NlpDependencyProfile } from '../services/nlpDependenciesApi';
import type { TextAnalysisClient } from '../services/textAnalysisClient';
import { createTextAnalysisExecutionStore, textAnalysisScopeKey, textAnalysisScopeReady } from '../store/textAnalysisExecutionStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../store/transportStore';
import { useUserRole, useUserStore } from '../store/userStore';

export interface TextAnalysisControlProps {
  creatorAccountId: string | null;
  client?: TextAnalysisClient;
  dependenciesApi?: NlpDependenciesApi;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}
export const approvedTextAnalysisProfile = (profile: NlpDependencyProfile) => profile.catalogued && !profile.withdrawn
  && profile.languages?.includes('en') && profile.tasks !== null && profile.tasks.length > 0
  && profile.tasks.every((task) => ['message_topics', 'concept_mentions'].includes(task));

export function TextAnalysisExecutionControls(props: TextAnalysisControlProps) {
  const transport = props.transport ?? bridgeTransportStore;
  const context = useSyncExternalStore(transport.subscribe, transport.getState, transport.getState);
  const role = useUserRole();
  const binding = textAnalysisScopeKey(context, role, props.creatorAccountId);
  return <ExecutionSession key={binding} {...props} transport={transport} />;
}
function ExecutionSession({ creatorAccountId, client, dependenciesApi = nlpDependenciesApi, transport = bridgeTransportStore }: TextAnalysisControlProps) {
  const store = useMemo(() => createTextAnalysisExecutionStore(client), [client]);
  const state = useStore(store);
  const [profiles, setProfiles] = useState<NlpDependencyProfile[]>([]);
  const [profile, setProfile] = useState('');
  const [review, setReview] = useState<EnrollmentCommand | null>(null);
  useEffect(() => {
    const sync = () => store.actions.setContext(transport.getState(), useUserStore.getState().role, creatorAccountId);
    const unsubscribe = transport.subscribe(sync), unsubscribeRole = useUserStore.subscribe(sync); sync();
    return () => { unsubscribe(); unsubscribeRole(); store.actions.dispose(); };
  }, [creatorAccountId, store, transport]);
  useEffect(() => {
    const controller = new AbortController();
    const binding = textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId);
    const active = () => !controller.signal.aborted && binding === textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId)
      && textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId);
    if (active()) void dependenciesApi.get(controller.signal).then((value) => {
      if (active()) setProfiles(value.configured ? value.profiles.filter(approvedTextAnalysisProfile) : []);
    }).catch(() => { if (active()) setProfiles([]); });
    return () => controller.abort();
  }, [creatorAccountId, dependenciesApi, transport]);
  const current = state.view?.current;
  const selected = profiles.find((entry) => entry.profile_sha256 === profile);
  const blocked = state.busy || !current || Boolean(state.message) || state.nextAfter !== null || state.recoveryOverflow || state.recoveryConflict
    || state.observations.some((item) => item.state !== 'resolved');
  const feedback = [
    state.busy ? 'Checking account processing…' : null,
    state.message,
    state.recoveryOverflow ? 'Retained request recovery reached its limit. New processing changes are unavailable.' : null,
    state.recoveryConflict ? 'The original request meaning could not be verified. New processing changes are unavailable.' : null,
  ].filter(Boolean).join(' ');
  const headerStatus: SectionStatus | null = state.status === 'error' || state.recoveryOverflow || state.recoveryConflict
    ? { label: 'Needs attention', tone: 'error' }
    : state.busy
      ? { label: 'Checking…', tone: 'default' }
      : !current
        ? null
        : current.enrollment_id === null
          ? { label: 'Not enrolled', tone: 'default' }
          : current.state === 'enabled'
            ? { label: 'Enabled', tone: 'success' }
            : current.state === 'paused'
              ? { label: 'Paused', tone: 'warning' }
              : { label: 'Retired', tone: 'default' };
  function open(action: EnrollmentCommand['action']) { setReview(store.actions.buildCommand(action, selected?.profile_sha256)); }
  return <Panel component="section" aria-label="Account text processing">
    <SectionHeader
      title="Account text processing"
      summary="Enable automatic processing for this account, pause it, or retire its enrollment. Downloads and Engine installation are separate."
      status={headerStatus}
    />
    <Stack spacing={1.75} data-journey-state="textanalysis.processing_controls">
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.refresh()}>Check processing status</Button>
      </Stack>
        <StatusLine id="processing-feedback" text={feedback || null}
          summary={state.status === 'error' || state.recoveryOverflow || state.recoveryConflict ? 'Needs attention' : state.busy ? 'Checking…' : 'Status'} detailsOnly
          tone={state.status === 'error' || state.recoveryOverflow || state.recoveryConflict ? 'error' : 'secondary'} />
      {current && <>
        <Box
          sx={{
            bgcolor: 'surface.subtle',
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1.5,
            display: 'grid',
            gap: 1.5,
            gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(3, minmax(0, 1fr))' },
            p: 2,
          }}
        >
          <Box>
            <Typography variant="body2" sx={{ fontWeight: 500 }}>
              Processing: {current.enrollment_id === null ? 'not enrolled' : current.state}.
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>Retained profile</Typography>
            <Typography variant="body2" sx={{ fontWeight: 500 }}>
              {state.view?.profile ? `${state.view.profile.profile_id} · ${state.view.profile.revision}` : 'Unavailable'}
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>Supported language & tasks</Typography>
            <Typography variant="body2">
              {state.view?.profile
                ? `English · ${state.view.profile.tasks.map((task) => task.task === 'message_topics' ? 'Topics' : 'Exact mentions').join(', ')}`
                : 'Resuming requires the original verified profile.'}
            </Typography>
          </Box>
        </Box>
        <TextField
          select
          size="small"
          label="Approved processing profile"
          value={profile}
          sx={{ maxWidth: 360 }}
          onChange={(event) => { setProfile(event.target.value); setReview(null); }}
        >
          <MenuItem value="">Choose a profile</MenuItem>
          {profiles.map((entry) => <MenuItem key={entry.profile_sha256} value={entry.profile_sha256}>{entry.profile_id} · {entry.revision}</MenuItem>)}
        </TextField>
        {profiles.length === 0 && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            No approved profiles are currently available. Saved results remain separate.
          </Typography>
        )}
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" disabled={blocked || !selected} onClick={() => open(current.enrollment_id === null ? 'enable' : 'replace')}>{current.enrollment_id === null ? 'Review enablement' : 'Review profile replacement'}</Button>
          <Button size="small" variant="outlined" disabled={blocked || !current.enrollment_id || current.state !== 'enabled'} onClick={() => open('pause')}>Pause processing</Button>
          <Button size="small" variant="outlined" disabled={blocked || current.state !== 'paused' || !current.capsule_available} onClick={() => open('resume')}>Resume processing</Button>
          <Button size="small" color="error" disabled={blocked || !current.enrollment_id || current.state === 'retired'} onClick={() => open('retire')}>Retire processing</Button>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Pausing and retirement preserve saved results. Source deletion and retention still apply. Replacing a profile creates a separate enrollment.
        </Typography>
      </>}
      {state.observations.map((item, index) => (
        <Stack
          key={`${item.locator.kind}:${item.locator.key}`}
          spacing={1}
          data-journey-state="textanalysis.processing_recovery"
          sx={{
            bgcolor: 'surface.subtle',
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1.5,
            p: 2,
          }}
        >
          <Typography variant="body2">Processing request {index + 1}: {item.state === 'resolved' ? 'recorded; current state shown separately' : item.state === 'absent' ? 'no recorded outcome' : 'outcome needs checking'}.</Typography>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.recover(item.locator)}>Check original command</Button>
            {item.state !== 'resolved' && item.intent && <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.retry(item)}>Retry exact command</Button>}
          </Stack>
        </Stack>
      ))}
      {state.nextAfter && (
        <Button size="small" variant="outlined" sx={{ alignSelf: 'flex-start' }} disabled={state.busy} onClick={() => void store.actions.refresh(state.nextAfter!)}>
          Next retained requests
        </Button>
      )}
    </Stack>
    <Dialog open={review !== null} onClose={() => setReview(null)} aria-labelledby="processing-command-title">
      <DialogTitle id="processing-command-title">Review processing change</DialogTitle>
      <DialogContent data-journey-state="textanalysis.processing_review">{review && <Stack spacing={1}>
        <Typography variant="body2">{({ enable: 'Enable processing', replace: 'Replace the processing profile', pause: 'Pause processing', resume: 'Resume processing', retire: 'Retire processing' })[review.action]}.</Typography>
        {review.expected_profile_sha256 && <Typography variant="body2">Approved profile: {profiles.find((entry) => entry.profile_sha256 === review.expected_profile_sha256)?.profile_id} · {profiles.find((entry) => entry.profile_sha256 === review.expected_profile_sha256)?.revision}.</Typography>}
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Enabling does not download files or promise offline readiness. Current execution rights are checked by the app.</Typography>
        {review.action === 'retire' && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Retirement stops further processing under this enrollment and preserves saved results.</Typography>}
      </Stack>}</DialogContent>
      <DialogActions><Button size="small" onClick={() => setReview(null)}>Back</Button><Button size="small" variant="contained" disabled={state.busy || !review} onClick={() => {
        if (review) { const command = review; setReview(null); void store.actions.submit(command); }
      }}>Confirm processing change</Button></DialogActions>
    </Dialog>
  </Panel>;
}
