import { Alert, Button, MenuItem, Stack, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useStore } from 'zustand';

import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { PreviewSummary, RecurringPreviewSummary, SavedPreview } from '../../analytics/textAnalysisContract';
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
type PreviewProps = TextAnalysisControlProps & {
  engineApi?: EngineAcquisitionApi; recurringProfile?: string; canPrepare?: boolean; onPreviewShown?: () => void;
};
function Scope({ scope, disclosure }: Pick<SavedPreview, 'scope' | 'disclosure'>) {
  return <Stack
    spacing={0.5}
    data-journey-state="textanalysis.preview_scope"
    sx={{
      bgcolor: 'surface.subtle',
      border: '1px solid',
      borderColor: 'divider',
      borderRadius: 1.5,
      p: 2,
    }}
  >
    {scope.directions === 'inbound'
      ? <Typography variant="body2">This preview covers {number.format(scope.source_count)} received messages. It can examine up to {scope.message_cap}.</Typography>
      : <Typography variant="body2">Frozen sample: {scope.source_count} messages, up to {scope.message_cap}, received and sent.</Typography>}
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>Retention window: after {date(scope.lower_exclusive)} through {date(scope.cutoff)}. The latest eligible messages were selected at this cutoff.</Typography>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>Selected message dates: {scope.earliest === null ? 'No eligible messages' : `${date(scope.earliest)} – ${date(scope.latest!)}`}.</Typography>
    <Typography variant="caption" sx={{ color: 'text.secondary' }}>Supported language: {disclosure.languages.map((language) => language === 'en' ? 'English' : language).join(', ')}. Tasks: {disclosure.tasks.map((task) => taskLabel(task.task)).join(', ')}.</Typography>
  </Stack>;
}
function RecurringSummary({ item, offset, onPage, onSource }: { item: RecurringPreviewSummary; offset: number; onPage: (offset: number) => void;
  onSource: (reference: RecurringPreviewSummary['evidence'][number]['reference']) => void }) {
  const end = Math.min(item.evidence.length, offset + 50);
  return <Stack component="section" spacing={1} aria-label="Recurring mention preview">
    <Typography component="h3" variant="h6" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.literal_key}</Typography>
    <Typography>{number.format(item.supporting_senders)} senders · {number.format(item.supporting_messages)} messages</Typography>
    {item.ranking_scope === 'assessed_portion' && <Typography variant="body2">Some messages could only be partly assessed. The counts cover the evidence found.</Typography>}
    {item.additional_phrases !== null && item.additional_phrases > 0 &&
      <Typography variant="body2">+{number.format(item.additional_phrases)} more recurring {item.additional_phrases === 1 ? 'phrase' : 'phrases'} in this sample.</Typography>}
    <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
      {item.evidence.slice(offset, end).map((entry, index) => <Button key={`${entry.ordinal}:${entry.reference.span?.start}:${entry.reference.span?.end}`}
        size="small" variant="outlined" onClick={() => onSource(entry.reference)}>
        Open quote {offset + index + 1} · {date(entry.reference.sent_at)}
      </Button>)}
    </Stack>
    {item.evidence.length > 50 && <Stack direction="row" spacing={1}>
      <Button size="small" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}>Previous quotes</Button>
      <Button size="small" disabled={end === item.evidence.length} onClick={() => onPage(end)}>Next quotes</Button>
    </Stack>}
  </Stack>;
}
function Summary({ item, offset, onPage, onSource }: { item: PreviewSummary; offset: number; onPage: (offset: number) => void;
  onSource: (reference: PreviewSummary['evidence'][number]['reference']) => void }) {
  const mentions = item.kind === 'mention_category', end = Math.min(item.evidence.length, offset + 50);
  return <Stack
    component="section"
    spacing={1}
    aria-label={mentions ? 'Preview mention winner' : 'Preview topic winner'}
    sx={{
      bgcolor: 'surface.subtle',
      border: '1px solid',
      borderColor: 'divider',
      borderRadius: 1.5,
      p: 2,
    }}
  >
    <Typography component="h3" variant="subtitle2">{mentions ? 'Exact mentions' : 'Topics'}: {item.label.display_label}</Typography>
    {item.label.label_status === 'id_only' && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Display label unavailable; the original category ID is shown.</Typography>}
    <Typography variant="body2">{number.format(item.supporting_conversations)} supporting conversations · {number.format(item.supporting_messages)} distinct messages{mentions && ` · ${number.format(item.occurrences)} exact occurrences`}.</Typography>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>Complete / partial supporting messages: {item.complete_supporting_messages} / {item.partial_supporting_messages}.</Typography>
    <Typography variant="caption" sx={{ color: 'text.secondary' }}>{item.ranking_scope === 'frozen_scope' ? 'Ranked across the entire frozen scope.' : 'Ranked by observed support in the assessed portion; the full ranking is unavailable.'}</Typography>
    {item.additional_categories !== null && <Typography variant="caption" sx={{ color: 'text.secondary' }}>+{item.additional_categories} additional {mentions ? 'mention' : 'topic'} categories in the fully assessed frozen scope.</Typography>}
    <Typography variant="caption" sx={{ color: 'text.secondary' }}>Share of completely assessed messages: {item.share_of_complete_messages === null ? 'Unavailable' : `${number.format(item.share_of_complete_messages * 100)}%`}. This is support, not model accuracy.</Typography>
    <Typography variant="caption" sx={{ color: 'text.secondary' }}>Shown evidence {offset + 1}–{end} of {item.evidence.length}. All retained references belong to this shown category.</Typography>
    <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
      {item.evidence.slice(offset, end).map((entry, index) => <Button key={`${entry.ordinal}:${entry.reference.span?.start ?? 'message'}:${entry.reference.span?.end ?? 'message'}`}
        size="small" variant="outlined"
        onClick={() => onSource(entry.reference)}>Open {mentions ? 'occurrence' : 'message'} evidence {offset + index + 1} · {entry.assessment} · {date(entry.reference.sent_at)}</Button>)}
    </Stack>
    <Stack direction="row" spacing={1}>
      <Button size="small" variant="outlined" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}>Previous evidence</Button>
      <Button size="small" variant="outlined" disabled={end === item.evidence.length} onClick={() => onPage(end)}>Next evidence</Button>
    </Stack>
  </Stack>;
}
export function TextAnalysisPreviews(props: PreviewProps) {
  const transport = props.transport ?? bridgeTransportStore;
  const context = useSyncExternalStore(transport.subscribe, transport.getState, transport.getState), role = useUserRole();
  return <PreviewSession key={textAnalysisScopeKey(context, role, props.creatorAccountId)} {...props} transport={transport} />;
}
function PreviewSession({ creatorAccountId, client, dependenciesApi = nlpDependenciesApi, transport = bridgeTransportStore,
  engineApi = previewEngineAcquisitionApi, recurringProfile, canPrepare = true, modelPrepared, onPreviewShown }: PreviewProps) {
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
  }, [creatorAccountId, dependenciesApi, transport, modelPrepared]);
  useEffect(() => { setOffsets({}); }, [state.saved?.output.contribution_id]);
  const profile = profiles.find((entry) => entry.profile_sha256 === (recurringProfile ?? selected)
    && (!recurringProfile || (entry.tasks?.length === 1 && entry.tasks[0] === 'concept_mentions')));
  const unresolved = state.observations.some((item) => item.state !== 'resolved');
  const outcome = state.outcome;
  const shown = state.saved?.schema_version === 'text-analysis-preview-saved.v2' && outcome?.receipt.state === 'committed'
    && outcome.receipt.contribution_id === state.saved.output.contribution_id ? state.saved.output.contribution_id : null;
  useEffect(() => { if (shown) onPreviewShown?.(); }, [shown, onPreviewShown]);
  const inProgress = outcome && ['prepared', 'pending', 'running'].includes(outcome.receipt.state)
    || state.observations.some((item) => item.state === 'resolved' && item.intent.kind !== 'enrollment_command'
      && 'receipt' in item.result && ['prepared', 'pending', 'running'].includes(item.result.receipt.state));
  const engineAccount = textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId) ? creatorAccountId : null;
  return <Stack spacing={2}>
    <EngineDependencyControls account={engineAccount} api={engineApi} selection={recurringProfile ? engineApi.selection : 'preview'} />
    <Panel component="section" aria-label="Text analysis previews">
      <SectionHeader
        title={recurringProfile ? 'Preview recurring mentions' : 'Text analysis previews'}
        summary={recurringProfile ? 'See one exact phrase used by at least two senders. Review the message sample before starting.' : 'Review a frozen message sample before confirming one preview. Saved preview reads do not run analysis or acquire files.'}
      />
      <Stack spacing={1.75} data-journey-state="textanalysis.preview_controls">
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.refresh()}>Check retained previews</Button>
        </Stack>
        {state.busy && <Typography role="status" variant="body2" sx={{ color: 'text.secondary' }}>Checking preview state…</Typography>}
        {state.message && <Alert severity="info">{state.message}</Alert>}
        {state.recoveryOverflow && <Alert severity="warning">Retained request recovery reached its limit. New previews are unavailable.</Alert>}
        {state.recoveryConflict && <Alert severity="warning">The original request meaning could not be verified. New previews are unavailable.</Alert>}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' }, flexWrap: 'wrap' }} useFlexGap>
          {!recurringProfile && <TextField select size="small" label="Approved preview profile" value={selected} onChange={(event) => setSelected(event.target.value)} sx={{ minWidth: 260, maxWidth: 360 }}>
            <MenuItem value="">Choose a profile</MenuItem>
            {profiles.map((entry) => <MenuItem key={entry.profile_sha256} value={entry.profile_sha256}>{entry.profile_id} · {entry.revision}</MenuItem>)}
          </TextField>}
          <Button size="small" variant="outlined" disabled={!canPrepare || state.busy || unresolved || Boolean(state.message) || Boolean(inProgress) || state.nextIntent !== null || state.recoveryOverflow || state.recoveryConflict || !profile?.installed || !profile.prepared}
            onClick={() => { if (profile) void store.actions.prepare(profile.profile_sha256, recurringProfile ? 'text-analysis-preview-prepare.v2' : 'text-analysis-preview-prepare.v1'); }}>
            {recurringProfile ? 'Review preview sample' : 'Prepare preview scope'}
          </Button>
        </Stack>
        {(!profile?.installed || !profile.prepared) && <Typography variant="caption" sx={{ color: 'text.secondary' }}>A prepared approved profile and compatible Engine are required for a new preview. File setup is separate; saved reads remain available.</Typography>}
        {outcome && <Stack spacing={1} aria-label="Original preview request" sx={{ bgcolor: 'surface.subtle', border: '1px solid', borderColor: 'divider', borderRadius: 1.5, p: 2 }}>
          <Typography variant="subtitle2">Original preview: {({ prepared: 'ready for confirmation', pending: 'pending', running: 'running', committed: 'saved', failed: 'completed without shown output', result_deleted: 'result erased' })[outcome.receipt.state]}.</Typography>
          {outcome.scope && outcome.disclosure && <Scope scope={outcome.scope} disclosure={outcome.disclosure} />}
          {!outcome.detailed_scope_available && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Detailed original scope has been erased. This request cannot select another sample.</Typography>}
          {outcome.receipt.state === 'prepared' && <>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>Preparation has not started inference. Confirming uses this exact sample and original profile.</Typography>
            <Stack direction="row" spacing={1}>
              <Button size="small" variant="contained" disabled={state.busy || unresolved || Boolean(state.message) || state.nextIntent !== null || state.recoveryOverflow || state.recoveryConflict || !outcome.confirmation} onClick={() => void store.actions.confirm()}>Confirm original preview</Button>
            </Stack>
          </>}
          {['pending', 'running'].includes(outcome.receipt.state) && <Typography variant="body2" sx={{ color: 'text.secondary' }} data-journey-state="textanalysis.preview_pending">The original request is pending or running. Check its status; do not prepare a replacement.</Typography>}
          {outcome.receipt.state === 'failed' && <Typography variant="body2" sx={{ color: 'text.secondary' }} data-journey-state="textanalysis.preview_terminal">{outcome.receipt.terminal_reason === 'empty' ? 'The complete assessment produced no showable output; the preview was not spent.'
            : outcome.receipt.terminal_reason === 'incomplete' ? 'Examination was incomplete and produced no showable output; the preview was not spent.'
              : outcome.receipt.terminal_reason === 'overflow' ? 'The selected output exceeded its limit; no output was retained and the preview was not spent.'
                : 'This request has no showable result. Its original terminal status is retained.'}</Typography>}
          {outcome.receipt.state === 'result_deleted' && <Typography variant="body2" sx={{ color: 'text.secondary' }} data-journey-state="textanalysis.preview_terminal">The saved result was erased. Deletion does not replenish a spent preview.</Typography>}
        </Stack>}
        {state.observations.map((item, index) => <Stack key={`${item.locator.kind}:${item.locator.key}`} spacing={1} data-journey-state="textanalysis.preview_recovery" sx={{ bgcolor: 'surface.subtle', border: '1px solid', borderColor: 'divider', borderRadius: 1.5, p: 2 }}>
          <Typography variant="body2">Preview request {index + 1} · {item.locator.kind === 'preview_confirm' ? 'confirmation' : 'preparation'}: {item.state === 'resolved' ? 'recorded' : 'needs checking'}.</Typography>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.recover(item.locator)}>Check original preview</Button>
            {item.state !== 'resolved' && item.intent && <Button size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.retry(item)}>Retry exact preview request</Button>}
          </Stack>
        </Stack>)}
        {state.nextIntent && <Button size="small" variant="outlined" sx={{ alignSelf: 'flex-start' }} disabled={state.busy} onClick={() => void store.actions.refresh(state.nextIntent!)}>Next retained preview requests</Button>}
        <Stack spacing={0.5} sx={{ pt: 0.5 }}>
          <Typography component="h3" variant="subtitle2">Saved shown outputs</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Opening a result verifies its original source inventory with current account read access. Current model and module availability are separate.</Typography>
        </Stack>
        {state.savedIds.length > 0 && (
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            {state.savedIds.map((id, index) => <Button key={id} size="small" variant="outlined" disabled={state.busy} onClick={() => void store.actions.openSaved(id)}>Open saved preview {index + 1}</Button>)}
          </Stack>
        )}
        {state.savedIds.length === 0 && <Typography variant="body2" sx={{ color: 'text.secondary' }}>No saved preview IDs are listed on this page.</Typography>}
        {state.nextSaved && <Button size="small" variant="outlined" sx={{ alignSelf: 'flex-start' }} disabled={state.busy} onClick={() => void store.actions.refresh(undefined, state.nextSaved!)}>Next saved preview IDs</Button>}
        {state.saved && <Stack spacing={1.5} aria-label="Verified shown preview" data-journey-state="textanalysis.preview_results">
          <Scope scope={state.saved.scope} disclosure={state.saved.disclosure} />
          {state.saved.output.tasks.map((task) => <Stack key={task.task} spacing={0.5}>
            <Typography component="h3" variant="subtitle2">{taskLabel(task.task)} examination</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>Eligible: {task.eligible} · Completely assessed: {task.assessed} · Partial: {task.partial} · Uncertain: {task.uncertain} · Unsupported: {task.unsupported} · Failed: {task.failed} · Unprocessed: {task.unprocessed}.</Typography>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>Uncertain, unsupported and unprocessed messages do not establish absence.</Typography>
            {!state.saved!.output.summaries.some((item) => item.kind === 'recurring_mention' ? task.task === 'concept_mentions' : item.label.task_contract === `${task.task}.v1`) && <Typography variant="body2" sx={{ color: 'text.secondary' }}>No showable category is retained for this task. Check its examination coverage.</Typography>}
          </Stack>)}
          {state.saved.output.summaries.map((item) => {
            const page = { offset: offsets[item.kind] ?? 0, onPage: (offset: number) => {
              store.actions.closeSource(); setOffsets((value) => ({ ...value, [item.kind]: offset }));
            }, onSource: (reference: PreviewSummary['evidence'][number]['reference']) => void store.actions.openSource(reference) };
            return item.kind === 'recurring_mention' ? <RecurringSummary key={item.kind} item={item} {...page} /> : <Summary key={item.kind} item={item} {...page} />;
          })}
          {state.saved.schema_version === 'text-analysis-preview-saved.v1' && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Topics and literal mentions do not establish requests, preferences, intent or performed acts.</Typography>}
        </Stack>}
      </Stack>
      <MentionEvidenceDialog state={state} actions={store.actions} />
    </Panel>
  </Stack>;
}
