import { Alert, Box, Button, MenuItem, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useStore } from 'zustand';

import { ChartPanel } from './ChartPanel';
import { LiteralFindings } from './LiteralFindings';
import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { FindingsBaseline } from '../../analytics/analyticsReadModel';
import { findingsTaskAvailability, type FindingsCategoryFilter, type FindingsDirection, type FindingsTask } from '../../analytics/findingsContract';
import { formatCount, formatRatioPercent } from '../../analytics/formatAnalytics';
import { createFindingsStore, findingsBinding, type FindingsStore } from '../../store/findingsStore';
import type { LiteralsStore } from '../../store/literalsStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../../store/transportStore';

const taskLabel = (task: FindingsTask['task']) => task === 'message_topics' ? 'Topics' : 'Mentions';
const windowLabel = (window: { start?: string | null; end?: string | null }, empty = 'All available saved dates') =>
  window.start && window.end ? `${new Date(window.start).toLocaleString()} – ${new Date(window.end).toLocaleString()}`
    : window.start ? `From ${new Date(window.start).toLocaleString()}` : window.end ? `Through ${new Date(window.end).toLocaleString()}` : empty;

function TaskSection({ task, previous = false, onCategory, selected }: { task: FindingsTask; previous?: boolean;
  onCategory?: (category: FindingsCategoryFilter, label: string) => void; selected?: FindingsCategoryFilter | null }) {
  const topics = task.task === 'message_topics';
  return <ChartPanel title={`${taskLabel(task.task)}${previous ? ' · previous window' : ''}`} description={topics ? 'Categories supported by distinct saved messages and conversations.' : 'Exact mention occurrences, distinct messages, and conversations.'}>
    <Stack spacing={2}>
      <Typography>{task.availability === 'current' ? 'Current saved findings' : task.availability === 'partial' ? 'Partially assessed' : 'Assessment pending'}{!task.scan_complete && ' · Scan incomplete'}</Typography>
      <Typography>Eligible: {formatCount(task.eligible_messages)} · Complete: {formatCount(task.complete_messages)} · Partial: {formatCount(task.partial_messages)} · Pending: {formatCount(task.pending_messages)}</Typography>
      <Typography>Failed: {formatCount(task.failed_messages)} · Unsupported: {formatCount(task.unsupported_messages)} · Uncertain: {formatCount(task.uncertain_messages)}</Typography>
      <Typography variant="body2">Unassessed or uncertain messages do not count as confirmed absence. Shares use only the {formatCount(task.complete_messages)} completely assessed messages; partial support is shown separately.</Typography>
      {task.rows.length === 0 ? <Typography>{task.eligible_messages === 0 && task.scan_complete ? 'No eligible saved messages in this window.' : 'No supported categories are listed. Check the assessment coverage above.'}</Typography> : <TableContainer sx={{ maxHeight: '24rem' }}>
        <Table stickyHeader size="small" aria-label={`${previous ? 'Previous ' : ''}${taskLabel(task.task)} findings`}>
          <TableHead><TableRow><TableCell>Category</TableCell>{!topics && <TableCell>Exact occurrences</TableCell>}<TableCell>Distinct messages</TableCell><TableCell>Distinct conversations</TableCell><TableCell>Complete / partial support</TableCell><TableCell>Share of complete messages</TableCell>{onCategory && <TableCell>Sources</TableCell>}</TableRow></TableHead>
          <TableBody>{task.rows.map((row) => <TableRow key={row.category_id}>
            <TableCell>{row.display_label}{row.label_status === 'id_only' && <Typography variant="caption" component="p">Display name unavailable; category ID shown.</Typography>}</TableCell>
            {!topics && <TableCell>{formatCount(row.occurrences)}</TableCell>}
            <TableCell>{formatCount(row.supporting_messages)}</TableCell><TableCell>{formatCount(row.supporting_conversations)}</TableCell>
            <TableCell>{formatCount(row.complete_supporting_messages)} / {formatCount(row.partial_supporting_messages)}</TableCell>
            <TableCell>{row.share_of_complete_messages === null ? 'Unavailable' : formatRatioPercent(row.share_of_complete_messages)}</TableCell>
            {onCategory && <TableCell><Button aria-label={`View sources for ${taskLabel(task.task)}: ${row.display_label}`}
              aria-pressed={selected?.task === task.task && selected.category_id === row.category_id}
              onClick={() => onCategory({ task: task.task, category_id: row.category_id }, row.display_label)}>View sources</Button></TableCell>}
          </TableRow>)}</TableBody>
        </Table>
      </TableContainer>}
    </Stack>
  </ChartPanel>;
}

interface Props {
  baseline: FindingsBaseline | null; controller?: FindingsStore; literalsController?: LiteralsStore;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}
export function TaskFindings(props: Props) {
  const transport = props.transport ?? bridgeTransportStore;
  const account = useSyncExternalStore(transport.subscribe, () => transport.getState().creatorAccountId);
  return <FindingsSession key={account ?? 'unbound'} {...props} transport={transport} />;
}
function FindingsSession({ baseline, controller, literalsController, transport = bridgeTransportStore }: Props) {
  const store = useMemo(() => controller ?? createFindingsStore(), [controller]);
  const state = useStore(store);
  const [direction, setDirection] = useState<FindingsDirection>('all');
  const [conversation, setConversation] = useState<string | null>(null);
  const [category, setCategory] = useState<{ filter: FindingsCategoryFilter; label: string } | null>(null);
  const binding = findingsBinding(baseline, direction, conversation, category?.filter);
  useEffect(() => {
    const sync = () => store.actions.setContext(baseline, transport.getState(), direction, conversation, category?.filter);
    const unsubscribe = transport.subscribe(sync); sync();
    return () => { unsubscribe(); store.actions.dispose(); };
  }, [baseline, direction, conversation, category, store, transport]);
  const result = state.binding === binding ? state.result : null;
  const selectCategory = (filter: FindingsCategoryFilter | null, label = '') => {
    store.actions.invalidate(); setConversation(null); setCategory(filter ? { filter, label } : null);
  };
  return <Stack component="section" spacing={2} aria-label="Profile text analysis" data-journey-state="findings.controls">
    <Box><Typography component="h2" variant="h6">Profile text analysis</Typography>
      <Typography>Available topic and mention findings from the selected analysis profile. These are separate from the baseline topics in Summary. Viewing results does not download files or run analysis.</Typography></Box>
    <TextField select label="Message direction" value={direction} onChange={(event) => { store.actions.invalidate(); setDirection(event.target.value as FindingsDirection); }} sx={{ maxWidth: '20rem' }}>
      <MenuItem value="all">All messages</MenuItem><MenuItem value="inbound">Received messages</MenuItem><MenuItem value="outbound">Sent messages</MenuItem>
    </TextField>
    <Typography>Requested dates: {windowLabel(baseline?.requested_window ?? {})}</Typography>
    {category && <Alert severity="info"><Typography>Selected category: {taskLabel(category.filter.task)} · {category.label}</Typography>
      <Typography>The category narrows conversation and source lists. Summaries retain the selected dates, direction and conversation population. Choosing or clearing a category returns to all conversations.</Typography>
      <Button onClick={() => selectCategory(null)}>Clear category filter</Button></Alert>}
    {state.status === 'loading' && <Typography role="status">Loading saved text analysis…</Typography>}
    {!result && state.status !== 'loading' && <Alert severity={state.status === 'error' ? 'error' : 'info'} data-journey-state="findings.unavailable">{state.message ?? 'Text analysis is unavailable for the current analytics view.'}</Alert>}
    {baseline && <Button disabled={state.status === 'loading'} onClick={() => void store.actions.read()}>Refresh saved findings</Button>}
    {result && <Stack spacing={2} data-journey-state="findings.results">
      <Typography>Available message dates: {windowLabel(result.effective_window, 'No message dates in this selection')} · {result.direction === 'all' ? 'All directions' : result.direction === 'inbound' ? 'Received messages' : 'Sent messages'}</Typography>
      <Typography>Processing state when saved: {result.processing_mode === 'paused' ? 'paused; these saved findings remain visible' : 'enabled'}.</Typography>
      <TextField select label="Conversation" value={conversation ?? 'all'} onChange={(event) => { store.actions.invalidate(); setConversation(event.target.value === 'all' ? null : event.target.value); }} sx={{ maxWidth: '30rem' }}>
        <MenuItem value="all">All conversations</MenuItem>
        {conversation && !result.conversation_page.items.some((item) => item.conversation_ref === conversation) && <MenuItem value={conversation}>Selected conversation</MenuItem>}
        {result.conversation_page.items.map((item, index) => <MenuItem key={item.conversation_ref} value={item.conversation_ref}>Conversation {result.conversation_page.offset + index + 1} · {formatCount(item.eligible_messages)} eligible messages</MenuItem>)}
      </TextField>
      <Typography variant="body2">Conversation numbers identify this saved list; source messages provide the conversation context. The list counts messages before the conversation filter.</Typography>
      <Typography>Conversations {formatCount(result.conversation_page.offset + (result.conversation_page.items.length ? 1 : 0))}–{formatCount(result.conversation_page.offset + result.conversation_page.items.length)} of {formatCount(result.conversation_page.total)}.</Typography>
      <Stack direction="row" spacing={1}>
        <Button disabled={result.conversation_page.offset === 0} onClick={() => void store.actions.read(0, Math.max(0, result.conversation_page.offset - result.conversation_page.limit))}>Previous conversations</Button>
        <Button disabled={!result.conversation_page.has_more || result.conversation_page.offset + result.conversation_page.limit > 1_000_000} onClick={() => void store.actions.read(0, result.conversation_page.offset + result.conversation_page.limit)}>Next conversations</Button>
      </Stack>
      {findingsTaskAvailability(result).map((item) => item.availability === 'declared'
        ? <TaskSection key={item.task} task={result.tasks.find((task) => task.task === item.task)!} onCategory={selectCategory} selected={category?.filter} />
        : <ChartPanel key={item.task} title={taskLabel(item.task)}>
          <Typography>{taskLabel(item.task)} are unavailable for this analysis profile.</Typography>
        </ChartPanel>)}
      <Box component="section" aria-label="Previous equal-duration window"><Stack spacing={2}>
        <Typography component="h2" variant="h6">Previous equal-duration window</Typography>
        {result.comparison.availability === 'available' ? <>
          <Typography>Previous dates: {windowLabel(result.comparison.requested_window ?? {})}. The previous window ends immediately before the current window starts. Coverage is assessed separately for each window.</Typography>
          <Typography>Available message dates: {windowLabel(result.comparison.effective_window ?? {}, 'No message dates in this selection')}</Typography>
          {result.comparison.tasks.map((task) => <TaskSection key={task.task} task={task} previous />)}
        </> : <Alert severity="info">{result.comparison.unavailable_reason === 'unbounded_window' ? 'Choose both a start and an end date to compare equal-duration windows.' : result.comparison.unavailable_reason === 'zero_width_window' ? 'Choose a window with a positive duration to compare.' : 'The previous comparison window is outside the supported date range.'}</Alert>}
      </Stack></Box>
      <ChartPanel title="Supporting sources" description="Open a saved source message to verify an individual finding.">
        <Stack spacing={1}>
          <Typography>Evidence {formatCount(result.evidence_page.offset + (result.evidence_page.items.length ? 1 : 0))}–{formatCount(result.evidence_page.offset + result.evidence_page.items.length)} of {formatCount(result.evidence_page.total)}.</Typography>
          {result.evidence_page.items.length === 0 && <Typography>No supporting source references are available on this page.</Typography>}
          {result.evidence_page.items.map((item, index) => {
            const row = result.tasks.find((task) => task.task === item.task)?.rows.find((r) => r.category_id === item.category_id);
            return <Button key={`${item.task}:${item.category_id}:${index}`} onClick={() => void store.actions.openSource(item.reference)}>
              {taskLabel(item.task)} source {result.evidence_page.offset + index + 1}: {row?.display_label ?? item.category_id} ({item.assessment})
            </Button>;
          })}
          <Stack direction="row" spacing={1}>
            <Button disabled={result.evidence_page.offset === 0} onClick={() => void store.actions.read(Math.max(0, result.evidence_page.offset - result.evidence_page.limit))}>Previous sources</Button>
            <Button disabled={!result.evidence_page.has_more || result.evidence_page.offset + result.evidence_page.limit > 1_000_000} onClick={() => void store.actions.read(result.evidence_page.offset + result.evidence_page.limit)}>Next sources</Button>
          </Stack>
        </Stack>
      </ChartPanel>
    </Stack>}
    {baseline && <LiteralFindings baseline={baseline} direction={direction} conversation={conversation} category={category?.filter} controller={literalsController} transport={transport} />}
    <MentionEvidenceDialog state={state.binding === binding ? state : { ...state, source: null, sourceLoading: false, sourceError: null }} actions={store.actions} />
  </Stack>;
}
