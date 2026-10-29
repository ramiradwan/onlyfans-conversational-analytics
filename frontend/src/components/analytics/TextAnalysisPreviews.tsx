import { Alert, Button, MenuItem, Stack, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useStore } from 'zustand';

import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { PreviewSummary, SavedPreview } from '../../analytics/textAnalysisContract';
import { previewEngineAcquisitionApi, type EngineAcquisitionApi } from '../../api/engineAcquisition';
import { nlpDependenciesApi, type NlpDependencyProfile } from '../../services/nlpDependenciesApi';
import { textAnalysisScopeKey, textAnalysisScopeReady } from '../../store/textAnalysisExecutionStore';
import { createTextAnalysisPreviewStore } from '../../store/textAnalysisPreviewStore';
import { bridgeTransportStore } from '../../store/transportStore';
import { useUserRole, useUserStore } from '../../store/userStore';
import { EngineDependencyControls } from '../EngineDependencyControls';
import { approvedTextAnalysisProfile, type TextAnalysisControlProps } from '../TextAnalysisExecutionControls';
import { Panel, SectionHeader } from '../ui';

const number = new Intl.NumberFormat();
const taskLabel = (task: string) => task === 'message_topics' ? 'Topics' : 'Exact mentions';
const date = (value: string) => new Date(value).toLocaleString();
type PreviewProps = TextAnalysisControlProps & { engineApi?: EngineAcquisitionApi };
function Scope({ scope, disclosure }: Pick<SavedPreview, 'scope' | 'disclosure'>) {
  return <Stack spacing={1} data-journey-state="textanalysis.preview_scope">
    <Typography>Frozen sample: {scope.source_count} messages, up to {scope.message_cap}, received and sent.</Typography>
    <Typography>Retention window: after {date(scope.lower_exclusive)} through {date(scope.cutoff)}. The latest eligible messages were selected at this cutoff.</Typography>
    <Typography>Selected message dates: {scope.earliest === null ? 'No eligible messages' : `${date(scope.earliest)} – ${date(scope.latest!)}`}.</Typography>
    <Typography>Supported language: {disclosure.languages.map((language) => language === 'en' ? 'English' : language).join(', ')}. Tasks: {disclosure.tasks.map((task) => taskLabel(task.task)).join(', ')}.</Typography>
  </Stack>;
}
function Summary({ item, offset, onPage, onSource }: { item: PreviewSummary; offset: number; onPage: (offset: number) => void;
  onSource: (reference: PreviewSummary['evidence'][number]['reference']) => void }) {
  const mentions = item.kind === 'mention_category', end = Math.min(item.evidence.length, offset + 50);
  return <Stack component="section" spacing={1} aria-label={mentions ? 'Preview mention winner' : 'Preview topic winner'}>
    <Typography component="h3" variant="subtitle1">{mentions ? 'Exact mentions' : 'Topics'}: {item.label.display_label}</Typography>
    {item.label.label_status === 'id_only' && <Typography>Display label unavailable; the original category ID is shown.</Typography>}
    <Typography>{number.format(item.supporting_conversations)} supporting conversations · {number.format(item.supporting_messages)} distinct messages{mentions && ` · ${number.format(item.occurrences)} exact occurrences`}.</Typography>
    <Typography>Complete / partial supporting messages: {item.complete_supporting_messages} / {item.partial_supporting_messages}.</Typography>
    <Typography>{item.ranking_scope === 'frozen_scope' ? 'Ranked across the entire frozen scope.' : 'Ranked by observed support in the assessed portion; the full ranking is unavailable.'}</Typography>
    {item.additional_categories !== null && <Typography>+{item.additional_categories} additional {mentions ? 'mention' : 'topic'} categories in the fully assessed frozen scope.</Typography>}
    <Typography>Share of completely assessed messages: {item.share_of_complete_messages === null ? 'Unavailable' : `${number.format(item.share_of_complete_messages * 100)}%`}. This is support, not model accuracy.</Typography>
    <Typography>Shown evidence {offset + 1}–{end} of {item.evidence.length}. All retained references belong to this shown category.</Typography>
    {item.evidence.slice(offset, end).map((entry, index) => <Button key={`${entry.ordinal}:${entry.reference.span?.start ?? 'message'}:${entry.reference.span?.end ?? 'message'}`}
      onClick={() => onSource(entry.reference)}>Open {mentions ? 'occurrence' : 'message'} evidence {offset + index + 1} · {entry.assessment} · {date(entry.reference.sent_at)}</Button>)}
    <Stack direction="row" spacing={1}>
      <Button disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}>Previous evidence</Button>
      <Button disabled={end === item.evidence.length} onClick={() => onPage(end)}>Next evidence</Button>
    </Stack>
  </Stack>;
}
export function TextAnalysisPreviews(props: PreviewProps) {
  const transport = props.transport ?? bridgeTransportStore;
  const context = useSyncExternalStore(transport.subscribe, transport.getState, transport.getState), role = useUserRole();
  return <PreviewSession key={textAnalysisScopeKey(context, role, props.creatorAccountId)} {...props} transport={transport} />;
}
function PreviewSession({ creatorAccountId, client, dependenciesApi = nlpDependenciesApi, transport = bridgeTransportStore,
  engineApi = previewEngineAcquisitionApi }: PreviewProps) {
  const store = useMemo(() => createTextAnalysisPreviewStore(client), [client]); const state = useStore(store);
  const [profiles, setProfiles] = useState<NlpDependencyProfile[]>([]), [selected, setSelected] = useState('');
  const [offsets, setOffsets] = useState<Record<string, number>>({});
  useEffect(() => {
    const sync = () => store.actions.setContext(transport.getState(), useUserStore.getState().role, creatorAccountId);
    const unsubscribe = transport.subscribe(sync), unsubscribeRole = useUserStore.subscribe(sync); sync();
    return () => { unsubscribe(); unsubscribeRole(); store.actions.dispose(); };
  }, [creatorAccountId, store, transport]);
  useEffect(() => {
    const controller = new AbortController(), binding = textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId);
    const active = () => !controller.signal.aborted && binding === textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId)
      && textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId);
    if (active()) void dependenciesApi.get(controller.signal).then((value) => { if (active()) setProfiles(value.configured ? value.profiles.filter(approvedTextAnalysisProfile) : []); })
      .catch(() => { if (active()) setProfiles([]); });
    return () => controller.abort();
  }, [creatorAccountId, dependenciesApi, transport]);
  useEffect(() => { setOffsets({}); }, [state.saved?.output.contribution_id]);
  const profile = profiles.find((entry) => entry.profile_sha256 === selected);
  const unresolved = state.observations.some((item) => item.state !== 'resolved');
  const outcome = state.outcome;
  const inProgress = outcome && ['prepared', 'pending', 'running'].includes(outcome.receipt.state)
    || state.observations.some((item) => item.state === 'resolved' && item.intent.kind !== 'enrollment_command'
      && 'receipt' in item.result && ['prepared', 'pending', 'running'].includes(item.result.receipt.state));
  const engineAccount = textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId) ? creatorAccountId : null;
  return <Stack spacing={2}>
    <EngineDependencyControls account={engineAccount} api={engineApi} selection="preview" />
    <Panel component="section" aria-label="Text analysis previews">
    <SectionHeader title="Text analysis previews" />
    <Stack spacing={2} data-journey-state="textanalysis.preview_controls">
      <Typography>Review a frozen message sample before confirming one preview. Saved preview reads do not run analysis or acquire files.</Typography>
      <Button disabled={state.busy} onClick={() => void store.actions.refresh()}>Check retained previews</Button>
      {state.busy && <Typography role="status">Checking preview state…</Typography>}
      {state.message && <Alert severity="info">{state.message}</Alert>}
      {state.recoveryOverflow && <Alert severity="warning">Retained request recovery reached its limit. New previews are unavailable.</Alert>}
      {state.recoveryConflict && <Alert severity="warning">The original request meaning could not be verified. New previews are unavailable.</Alert>}
      <TextField select label="Approved preview profile" value={selected} onChange={(event) => setSelected(event.target.value)}>
        <MenuItem value="">Choose a profile</MenuItem>
        {profiles.map((entry) => <MenuItem key={entry.profile_sha256} value={entry.profile_sha256}>{entry.profile_id} · {entry.revision}</MenuItem>)}
      </TextField>
      <Button disabled={state.busy || unresolved || Boolean(state.message) || Boolean(inProgress) || state.nextIntent !== null || state.recoveryOverflow || state.recoveryConflict || !profile?.installed || !profile.prepared}
        onClick={() => { if (profile) void store.actions.prepare(profile.profile_sha256); }}>Prepare preview scope</Button>
      {(!profile?.installed || !profile.prepared) && <Typography>A prepared approved profile and compatible Engine are required for a new preview. File setup is separate; saved reads remain available.</Typography>}
      {outcome && <Stack spacing={1} aria-label="Original preview request">
        <Typography>Original preview: {({ prepared: 'ready for confirmation', pending: 'pending', running: 'running', committed: 'saved', failed: 'completed without shown output', result_deleted: 'result erased' })[outcome.receipt.state]}.</Typography>
        {outcome.scope && outcome.disclosure && <Scope scope={outcome.scope} disclosure={outcome.disclosure} />}
        {!outcome.detailed_scope_available && <Typography>Detailed original scope has been erased. This request cannot select another sample.</Typography>}
        {outcome.receipt.state === 'prepared' && <>
          <Typography>Preparation has not started inference. Confirming uses this exact sample and original profile.</Typography>
          <Button disabled={state.busy || unresolved || Boolean(state.message) || state.nextIntent !== null || state.recoveryOverflow || state.recoveryConflict || !outcome.confirmation} onClick={() => void store.actions.confirm()}>Confirm original preview</Button>
        </>}
        {['pending', 'running'].includes(outcome.receipt.state) && <Typography data-journey-state="textanalysis.preview_pending">The original request is pending or running. Check its status; do not prepare a replacement.</Typography>}
        {outcome.receipt.state === 'failed' && <Typography data-journey-state="textanalysis.preview_terminal">{outcome.receipt.terminal_reason === 'empty' ? 'The complete assessment produced no showable output; the preview was not spent.'
          : outcome.receipt.terminal_reason === 'incomplete' ? 'Examination was incomplete and produced no showable output; the preview was not spent.'
            : outcome.receipt.terminal_reason === 'overflow' ? 'The selected output exceeded its limit; no output was retained and the preview was not spent.'
              : 'This request has no showable result. Its original terminal status is retained.'}</Typography>}
        {outcome.receipt.state === 'result_deleted' && <Typography data-journey-state="textanalysis.preview_terminal">The saved result was erased. Deletion does not replenish a spent preview.</Typography>}
      </Stack>}
      {state.observations.map((item, index) => <Stack key={`${item.locator.kind}:${item.locator.key}`} spacing={1} data-journey-state="textanalysis.preview_recovery">
        <Typography>Preview request {index + 1} · {item.locator.kind === 'preview_confirm' ? 'confirmation' : 'preparation'}: {item.state === 'resolved' ? 'recorded' : 'needs checking'}.</Typography>
        <Stack direction="row" spacing={1}>
          <Button disabled={state.busy} onClick={() => void store.actions.recover(item.locator)}>Check original preview</Button>
          {item.state !== 'resolved' && item.intent && <Button disabled={state.busy} onClick={() => void store.actions.retry(item)}>Retry exact preview request</Button>}
        </Stack>
      </Stack>)}
      {state.nextIntent && <Button disabled={state.busy} onClick={() => void store.actions.refresh(state.nextIntent!)}>Next retained preview requests</Button>}
      <Typography component="h3" variant="subtitle1">Saved shown outputs</Typography>
      <Typography>Opening a result verifies its original source inventory with current account read access. Current model and module availability are separate.</Typography>
      {state.savedIds.map((id, index) => <Button key={id} disabled={state.busy} onClick={() => void store.actions.openSaved(id)}>Open saved preview {index + 1}</Button>)}
      {state.savedIds.length === 0 && <Typography>No saved preview IDs are listed on this page.</Typography>}
      {state.nextSaved && <Button disabled={state.busy} onClick={() => void store.actions.refresh(undefined, state.nextSaved!)}>Next saved preview IDs</Button>}
      {state.saved && <Stack spacing={2} aria-label="Verified shown preview" data-journey-state="textanalysis.preview_results">
        <Scope scope={state.saved.scope} disclosure={state.saved.disclosure} />
        {state.saved.output.tasks.map((task) => <Stack key={task.task} spacing={1}>
          <Typography component="h3" variant="subtitle1">{taskLabel(task.task)} examination</Typography>
          <Typography>Eligible: {task.eligible} · Completely assessed: {task.assessed} · Partial: {task.partial} · Uncertain: {task.uncertain} · Unsupported: {task.unsupported} · Failed: {task.failed} · Unprocessed: {task.unprocessed}.</Typography>
          <Typography>Uncertain, unsupported and unprocessed messages do not establish absence.</Typography>
          {!state.saved!.output.summaries.some((item) => item.label.task_contract === `${task.task}.v1`) && <Typography>No showable category is retained for this task. Check its examination coverage.</Typography>}
        </Stack>)}
        {state.saved.output.summaries.map((item) => <Summary key={item.kind} item={item} offset={offsets[item.kind] ?? 0}
          onPage={(offset) => { store.actions.closeSource(); setOffsets((value) => ({ ...value, [item.kind]: offset })); }}
          onSource={(reference) => void store.actions.openSource(reference)} />)}
        <Typography>Topics and literal mentions do not establish requests, preferences, intent or performed acts.</Typography>
      </Stack>}
    </Stack>
    <MentionEvidenceDialog state={state} actions={store.actions} />
    </Panel>
  </Stack>;
}
