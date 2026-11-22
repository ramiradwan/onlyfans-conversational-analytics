import { keyframes } from '@emotion/react';
import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Skeleton, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography, useMediaQuery } from '@mui/material';
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useStore } from 'zustand';

import { ChartPanel } from './ChartPanel';
import { LiteralFindings } from './LiteralFindings';
import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { FindingsBaseline } from '../../analytics/analyticsReadModel';
import { findingsTaskAvailability, type FindingsCategoryFilter, type FindingsDirection, type FindingsTask, type NavigationDeclaration, type NavigationGroupRow } from '../../analytics/findingsContract';
import { formatCount, formatRatioPercent } from '../../analytics/formatAnalytics';
import { createFindingsStore, findingsBinding, type createFindingsPresentation, type FindingsStore } from '../../store/findingsStore';
import type { LiteralsStore } from '../../store/literalsStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../../store/transportStore';
import { componentTokens, effectTokens } from '../../theme';
import { OnboardingAction, OnboardingSpot, OnboardingStatus } from '../ui/OnboardingCard';

const taskLabel = (task: FindingsTask['task']) => task === 'message_topics' ? 'Topics' : 'Mentions';
const windowLabel = (window: { start?: string | null; end?: string | null }, empty = 'All available saved dates') =>
  window.start && window.end ? `${new Date(window.start).toLocaleString()} – ${new Date(window.end).toLocaleString()}`
    : window.start ? `From ${new Date(window.start).toLocaleString()}` : window.end ? `Through ${new Date(window.end).toLocaleString()}` : empty;

type CategoryAction = (category: FindingsCategoryFilter, label: string) => void;
type TaskNavigation = { declaration: NavigationDeclaration; rows: NavigationGroupRow[] };
const findingBarArrival = keyframes(effectTokens.motion.keyframes.barArrival);
const primaryButtonSx = { minHeight: componentTokens.staticUi.controlHeight, maxWidth: '100%' };

function CategoryTable({ task, rows, title, onCategory, selected }: { task: FindingsTask; rows: FindingsTask['rows'];
  title: string; onCategory?: CategoryAction; selected?: FindingsCategoryFilter | null }) {
  const topics = task.task === 'message_topics';
  return <TableContainer tabIndex={0} role="group" aria-label={`${title} scroll area`} sx={{ maxHeight: '24rem', overflow: 'auto' }}>
    <Table stickyHeader size="small" aria-label={title} sx={{
      overflowWrap: 'normal',
      '& .MuiTableCell-head': { minWidth: '6.5rem' },
      '& .MuiTableCell-body:first-of-type': { maxWidth: '16rem', overflowWrap: 'anywhere' },
    }}>
      <TableHead><TableRow><TableCell>Category</TableCell>{!topics && <TableCell>Exact occurrences</TableCell>}<TableCell>Distinct messages</TableCell><TableCell>Distinct conversations</TableCell><TableCell>Complete / partial support</TableCell><TableCell>Share of complete messages</TableCell>{onCategory && <TableCell>Sources</TableCell>}</TableRow></TableHead>
      <TableBody>{rows.map((row) => <TableRow key={row.category_id}>
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
  </TableContainer>;
}

function TaskSection({ task, previous = false, onCategory, selected, navigation }: { task: FindingsTask; previous?: boolean;
  onCategory?: CategoryAction; selected?: FindingsCategoryFilter | null;
  navigation?: TaskNavigation | null }) {
  const topics = task.task === 'message_topics';
  const title = `${previous ? 'Previous ' : ''}${taskLabel(task.task)} findings`;
  const groups = navigation?.declaration.groups.filter((group) => group.task === task.task) ?? [];
  const assigned = new Set(groups.flatMap((group) => group.member_ids));
  const ungrouped = task.rows.filter((row) => !assigned.has(row.category_id));
  return <ChartPanel title={`${taskLabel(task.task)}${previous ? ' · previous window' : ''}`} description={topics ? 'Categories supported by distinct saved messages and conversations.' : 'Exact mention occurrences, distinct messages, and conversations.'}>
    <Stack spacing={2}>
      <Typography>{task.availability === 'current' ? 'Current saved findings' : task.availability === 'partial' ? 'Partially assessed' : 'Assessment pending'}{!task.scan_complete && ' · Scan incomplete'}</Typography>
      <Typography>Eligible: {formatCount(task.eligible_messages)} · Complete: {formatCount(task.complete_messages)} · Partial: {formatCount(task.partial_messages)} · Pending: {formatCount(task.pending_messages)}</Typography>
      <Typography>Failed: {formatCount(task.failed_messages)} · Unsupported: {formatCount(task.unsupported_messages)} · Uncertain: {formatCount(task.uncertain_messages)}</Typography>
      <Typography variant="body2">Unassessed or uncertain messages do not count as confirmed absence. Shares use only the {formatCount(task.complete_messages)} completely assessed messages; partial support is shown separately.</Typography>
      {task.rows.length === 0 ? <Typography>{task.eligible_messages === 0 && task.scan_complete ? 'No eligible saved messages in this window.' : 'No supported categories are listed. Check the assessment coverage above.'}</Typography>
        : !navigation ? <CategoryTable task={task} rows={task.rows} title={title} onCategory={onCategory} selected={selected} />
          : <>
            {groups.map((group) => {
              const row = navigation.rows.find((item) => item.group_id === group.group_id)!;
              return <Stack key={group.group_id} spacing={1}>
                  <Typography component="h3" variant="subtitle1">{group.display_label}</Typography>
                  <Typography>Union support: {formatCount(row.supporting_messages)} messages · {formatCount(row.supporting_conversations)} conversations · Complete / partial: {formatCount(row.complete_supporting_messages)} / {formatCount(row.partial_supporting_messages)}</Typography>
                  <Typography>Share of complete messages: {row.share_of_complete_messages === null ? 'Unavailable' : formatRatioPercent(row.share_of_complete_messages)}</Typography>
                  <CategoryTable task={task} rows={group.member_ids.map((id) => task.rows.find((item) => item.category_id === id)!)}
                    title={`${title} · ${group.display_label}`} onCategory={onCategory} selected={selected} />
                </Stack>;
            })}
            {ungrouped.length > 0 && <Box><Typography component="h3" variant="subtitle1">Ungrouped categories</Typography>
              <CategoryTable task={task} rows={ungrouped} title={`${title} · Ungrouped categories`} onCategory={onCategory} selected={selected} /></Box>}
          </>}
    </Stack>
  </ChartPanel>;
}

function FindingsCard({ title, first = false, state, children }: {
  title: string; first?: boolean; state?: 'loading' | 'unavailable'; children?: ReactNode;
}) {
  const titleId = useId();
  const busy = state === 'loading';
  return <Box component="section" className="onboarding-surface" data-composition="first-value"
    aria-label={busy ? title : undefined} aria-labelledby={busy ? undefined : titleId} aria-busy={busy || undefined}
    data-visual="findings-summary" data-journey-state={state && `findings.${state}`}
    sx={(theme) => ({ width: '100%', maxWidth: '68rem', fontFamily: theme.typography.fontFamily,
      ...(first && { '&.onboarding-surface': theme.effects.ambientGlow(theme) }),
      '& > .onboarding-surface-head:last-child, & > .onboarding-surface-head:has(+ .onboarding-status)': { mb: 0 },
    })}>
    {busy ? <>
      <Box className="onboarding-surface-head" aria-hidden="true">
        <Skeleton animation={false} variant="rounded" width={40} height={40} sx={{ flexShrink: 0 }} />
        <Skeleton animation={false} variant="rounded" width="6rem" height="1em" />
      </Box>
      <Stack spacing={1} aria-hidden="true">
        <Skeleton animation={false} variant="rounded" className="onboarding-surface-title" width="92%" height="1em" />
        <Skeleton animation={false} variant="rounded" width="58%" height="1em" />
      </Stack>
    </> : <Box className="onboarding-surface-head">
      <OnboardingSpot icon="insights" />
      <Box component="h2" id={titleId} sx={{ m: 0, font: 'inherit', letterSpacing: 'inherit', minWidth: 0 }}>{title}</Box>
    </Box>}
    {children}
  </Box>;
}

function FindingsSummary({ tasks, title, first, navigation, onCategory, selected }: { tasks: FindingsTask[]; title: string; first: boolean;
  navigation: TaskNavigation | null; onCategory: CategoryAction; selected?: FindingsCategoryFilter }) {
  const dialogTitleId = useId();
  const basisId = useId();
  const [open, setOpen] = useState(false);
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const topics = tasks.find(task => task.task === 'message_topics');
  const topicDenominator = topics?.complete_messages || null;
  const pricing = topics?.rows.filter(row => row.label_status === 'resolved' && row.display_label === 'Pricing questions');
  const pricingShare = pricing?.length === 1 ? pricing[0].share_of_complete_messages : null;
  const measuredTask = tasks.find(task => task.task === 'message_topics' && task.rows.some(row => row.complete_supporting_messages > 0))
    ?? tasks.find(task => task.rows.some(row => row.complete_supporting_messages > 0));
  const bars = (measuredTask?.rows ?? [])
    .filter(row => row.share_of_complete_messages !== null && row.complete_supporting_messages > 0)
    .sort((a, b) => b.complete_supporting_messages - a.complete_supporting_messages)
    .slice(0, 3);
  const selectFromDialog: CategoryAction = (filter, label) => {
    setOpen(false);
    onCategory(filter, label);
  };

  return <>
    <FindingsCard title={title} first={first}>
      {pricingShare !== null && pricingShare > 0 && <h3 className="onboarding-surface-title" data-visual="findings-headline">
        Pricing questions come up in {formatRatioPercent(pricingShare)} of fully checked messages
      </h3>}
      {measuredTask && <Stack component="ul" aria-label={`${taskLabel(measuredTask.task)} shares of fully checked messages`}
        spacing={1} sx={{ listStyle: 'none', p: 0, m: 0, '.onboarding-surface-title + &': { mt: 2 } }}>
        {bars.map((row, index) => {
          const share = row.share_of_complete_messages!;
          return <Box component="li" key={row.category_id} sx={{ display: 'grid',
            gridTemplateColumns: { xs: 'minmax(0, 1fr) auto', sm: 'minmax(0, 14rem) minmax(0, 1fr) auto' },
            alignItems: 'center', columnGap: 2, rowGap: 0.5 }}>
            <Typography variant="body2">{row.display_label}</Typography>
            <Box aria-hidden="true" sx={{ gridColumn: { xs: '1 / -1', sm: '2' }, gridRow: { xs: 2, sm: 1 },
              borderRadius: '999px', overflow: 'hidden', bgcolor: 'surface.subtle', height: 8 }}>
              <Box data-visual="finding-bar" sx={{ width: `${share * 100}%`, height: '100%', bgcolor: 'measurement.main',
                borderRadius: 'inherit', transformOrigin: 'left center',
                ...(first && { animation: `${findingBarArrival} 240ms ${effectTokens.motion.easing.enter} both`, animationDelay: `${index * 40}ms` }),
                '@media (prefers-reduced-motion: reduce)': { animation: 'none' } }} />
            </Box>
            <Typography variant="numericBody" sx={{ gridColumn: { xs: 2, sm: 3 }, gridRow: 1, textAlign: 'end' }}>
              {formatRatioPercent(share)}
            </Typography>
          </Box>;
        })}
      </Stack>}
      {topicDenominator !== null && <OnboardingAction>
        <Button variant="contained" aria-haspopup="dialog" sx={primaryButtonSx} onClick={() => setOpen(true)}>See all topics</Button>
      </OnboardingAction>}
    </FindingsCard>
    {topicDenominator !== null && <Dialog open={open} onClose={() => setOpen(false)} maxWidth="lg" fullWidth
      transitionDuration={reducedMotion ? 0 : 200} aria-labelledby={dialogTitleId} aria-describedby={basisId}
      slotProps={{ paper: { sx: { m: { xs: 2, sm: 4 }, width: { xs: 'calc(100% - 32px)', sm: 'calc(100% - 64px)' },
        minWidth: 0, overflowWrap: 'anywhere' } } }}>
      <DialogTitle id={dialogTitleId}
        sx={{ fontSize: { xs: '1.125rem', sm: '1.25rem' }, fontWeight: 600, lineHeight: 1.25, letterSpacing: '-.015em', textWrap: 'balance' }}>
        All topics
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography id={basisId}>Based on {formatCount(topicDenominator)} fully checked messages.</Typography>
          {tasks.map(task => <TaskSection key={task.task} task={task} onCategory={selectFromDialog}
            selected={selected} navigation={navigation} />)}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ justifyContent: 'flex-start', px: 3, py: 2 }}>
        <Button variant="contained" sx={primaryButtonSx} onClick={() => setOpen(false)}>Close</Button>
      </DialogActions>
    </Dialog>}
  </>;
}

interface Props {
  baseline: FindingsBaseline | null; controller?: FindingsStore; literalsController?: LiteralsStore;
  presentation?: ReturnType<typeof createFindingsPresentation>;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
  onRefreshBaseline?: () => Promise<void>;
}
interface RefreshRun {
  ready: boolean; current: () => boolean; invalidate: () => void; finish: () => void;
}
export function TaskFindings(props: Props) {
  const transport = props.transport ?? bridgeTransportStore;
  const account = useSyncExternalStore(transport.subscribe, () => transport.getState().creatorAccountId);
  return <FindingsSession key={account ?? 'unbound'} {...props} transport={transport} />;
}
function FindingsSession({ baseline, controller, literalsController, presentation, transport = bridgeTransportStore, onRefreshBaseline }: Props) {
  const store = useMemo(() => controller ?? createFindingsStore(undefined, { presentation }), [controller, presentation]);
  const state = useStore(store);
  const [direction, setDirection] = useState<FindingsDirection>('all');
  const [conversation, setConversation] = useState<string | null>(null);
  const [category, setCategory] = useState<{ filter: FindingsCategoryFilter; label: string } | null>(null);
  const [view, setView] = useState<'grouped' | null>(store.view);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const categoryHeading = useRef<HTMLHeadingElement>(null);
  const focusCategory = useRef(false);
  const refreshScope = useRef<object | null>(null);
  const pendingRefresh = useRef<RefreshRun | null>(null);
  const requestedWindow = baseline ? JSON.stringify(baseline.requested_window) : null;
  const lastRequestedWindow = useRef(requestedWindow);
  useLayoutEffect(() => {
    if (requestedWindow !== null) {
      if (lastRequestedWindow.current !== requestedWindow) pendingRefresh.current?.invalidate();
      lastRequestedWindow.current = requestedWindow;
    }
  }, [requestedWindow]);
  useLayoutEffect(() => {
    const scope = {};
    refreshScope.current = scope;
    return () => {
      refreshScope.current = null;
      pendingRefresh.current?.invalidate();
    };
  }, [store, transport, onRefreshBaseline, direction, conversation, category, view]);
  const binding = findingsBinding(baseline, direction, conversation, category?.filter, view);
  useEffect(() => {
    const sync = () => store.actions.setContext(baseline, transport.getState(), direction, conversation, category?.filter, view);
    const unsubscribe = transport.subscribe(sync); sync();
    const run = pendingRefresh.current;
    if (run?.ready) {
      run.ready = false;
      if (!run.current()) run.finish();
      else {
        try { void Promise.resolve(store.actions.reload()).then(run.finish, run.finish); }
        catch { run.finish(); }
      }
    }
    return unsubscribe;
  }, [baseline, direction, conversation, category, view, store, transport, refreshRevision]);
  useEffect(() => () => store.actions.dispose(), [store]);
  const result = state.binding === binding ? state.result : null;
  const firstInsight = result !== null && state.firstInsight;
  const insightTitle = firstInsight ? 'Your first insights are ready' : 'Insights';
  const navigation = result?.schema_version === 'analytics-findings.v4' ? result.navigation : null;
  const currentNavigation = navigation?.availability === 'available' && navigation.declaration
    ? { declaration: navigation.declaration, rows: navigation.current } : null;
  const unpinned = navigation?.availability === 'not_declared';
  const selectCategory = (filter: FindingsCategoryFilter | null, label = '') => {
    if (conversation === null && category?.filter.task === filter?.task && category?.filter.category_id === filter?.category_id) {
      focusCategory.current = false;
      return;
    }
    store.actions.invalidate(); setConversation(null); setCategory(filter ? { filter, label } : null);
  };
  useLayoutEffect(() => {
    if (!focusCategory.current) return;
    focusCategory.current = false;
    categoryHeading.current?.focus();
  }, [category]);
  async function refresh() {
    const scope = refreshScope.current;
    if (!scope || pendingRefresh.current) return;
    const identity = () => {
      const value = transport.getState();
      return JSON.stringify([value.creatorAccountId, value.session, value.connection, value.readModelState]);
    };
    let original: string | null = null;
    let invalidated = false;
    let finished = false;
    let queued = false;
    let unsubscribe = () => {};
    const release = () => {
      const dispose = unsubscribe;
      unsubscribe = () => {};
      try { dispose(); } catch { /* Cleanup must not strand the refresh. */ }
    };
    const run: RefreshRun = {
      ready: false,
      current: () => {
        try {
          return !finished && !invalidated && pendingRefresh.current === run
            && refreshScope.current === scope && identity() === original;
        } catch { return false; }
      },
      invalidate: () => { invalidated = true; release(); },
      finish: () => {
        if (finished) return;
        finished = true;
        release();
        if (pendingRefresh.current === run) {
          pendingRefresh.current = null;
          if (refreshScope.current) setRefreshing(false);
        }
      },
    };
    pendingRefresh.current = run;
    setRefreshing(true);
    try {
      original = identity();
      unsubscribe = transport.subscribe(() => { if (!run.current()) run.invalidate(); });
      if (onRefreshBaseline) await onRefreshBaseline();
      if (run.current()) {
        run.ready = true;
        queued = true;
        setRefreshRevision(value => value + 1);
      }
    } catch { /* A failed baseline refresh must not reload its previous identity. */ }
    finally {
      if (!queued) run.finish();
    }
  }
  return <Stack component="section" spacing={2} aria-label="Profile text analysis" data-journey-state="findings.controls"
    sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <Box><Typography component="h2" variant="h6">Profile text analysis</Typography>
      <Typography>Available topic and mention findings from the selected analysis profile. These are separate from the baseline topics in Summary. Viewing results does not download files or run analysis.</Typography></Box>
    <TextField select label="Message direction" value={direction} onChange={(event) => { store.actions.invalidate(); setDirection(event.target.value as FindingsDirection); }} sx={{ maxWidth: '20rem' }}>
      <MenuItem value="all">All messages</MenuItem><MenuItem value="inbound">Received messages</MenuItem><MenuItem value="outbound">Sent messages</MenuItem>
    </TextField>
    <Typography>Requested dates: {windowLabel(baseline?.requested_window ?? {})}</Typography>
    {category && <Alert severity="info"><Typography component="h3" ref={categoryHeading} tabIndex={-1}>Selected category: {taskLabel(category.filter.task)} · {category.label}</Typography>
      <Typography>The category narrows conversation and source lists. Summaries retain the selected dates, direction and conversation population. Choosing or clearing a category returns to all conversations.</Typography>
      <Button onClick={() => selectCategory(null)}>Clear category filter</Button></Alert>}
    {state.established && (baseline || refreshing) && <Button disabled={refreshing || state.status === 'loading'} onClick={() => void refresh()}>Refresh saved findings</Button>}
    {!result && (state.status === 'loading'
      ? <FindingsCard title={insightTitle} state="loading" />
      : <FindingsCard title={insightTitle} state="unavailable">
        <OnboardingStatus tone="attention">Insights aren’t available right now.</OnboardingStatus>
      </FindingsCard>)}
    {result && <Stack spacing={2} data-journey-state="findings.results">
      <FindingsSummary tasks={result.tasks} title={insightTitle} first={firstInsight} navigation={currentNavigation}
        onCategory={(filter, label) => { focusCategory.current = true; selectCategory(filter, label); }} selected={category?.filter} />
      <Typography>Available message dates: {windowLabel(result.effective_window, 'No message dates in this selection')} · {result.direction === 'all' ? 'All directions' : result.direction === 'inbound' ? 'Received messages' : 'Sent messages'}</Typography>
      <Typography>Processing state when saved: {result.processing_mode === 'paused' ? 'paused; these saved findings remain visible' : 'enabled'}.</Typography>
      <TextField select label="Conversation" value={conversation ?? 'all'} onChange={(event) => { store.actions.invalidate(); setConversation(event.target.value === 'all' ? null : event.target.value); }} sx={{ maxWidth: '30rem' }}>
        <MenuItem value="all">All conversations</MenuItem>
        {conversation && !result.conversation_page.items.some((item) => item.conversation_ref === conversation) && <MenuItem value={conversation}>Selected conversation</MenuItem>}
        {result.conversation_page.items.map((item, index) => <MenuItem key={item.conversation_ref} value={item.conversation_ref}>Conversation {result.conversation_page.offset + index + 1} · {formatCount(item.eligible_messages)} eligible messages</MenuItem>)}
      </TextField>
      <Typography variant="body2">Conversation numbers identify this saved list; source messages provide the conversation context. The list counts messages before the conversation filter.</Typography>
      <Typography>Conversations {formatCount(result.conversation_page.offset + (result.conversation_page.items.length ? 1 : 0))}–{formatCount(result.conversation_page.offset + result.conversation_page.items.length)} of {formatCount(result.conversation_page.total)}.</Typography>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Button disabled={unpinned || result.conversation_page.offset === 0} onClick={() => void store.actions.read(0, Math.max(0, result.conversation_page.offset - result.conversation_page.limit))}>Previous conversations</Button>
        <Button disabled={unpinned || !result.conversation_page.has_more || result.conversation_page.offset + result.conversation_page.limit > 1_000_000} onClick={() => void store.actions.read(0, result.conversation_page.offset + result.conversation_page.limit)}>Next conversations</Button>
      </Stack>
      {navigation && <Typography variant="body2">{navigation.availability === 'available'
        ? 'Groups and categories can overlap. Group support counts each supporting message once; shares do not add to a distribution.'
        : 'Declared category groups are unavailable for this saved analysis. Categories remain listed below.'}</Typography>}
      {unpinned && <Box><Typography>Use a flat view to browse additional source or conversation pages.</Typography>
        <Button onClick={() => { store.actions.invalidate(); setView(null); }}>Use flat categories</Button></Box>}
      {findingsTaskAvailability(result).map((item) => item.availability === 'declared'
        ? <TaskSection key={item.task} task={result.tasks.find((task) => task.task === item.task)!} onCategory={selectCategory} selected={category?.filter}
            navigation={currentNavigation} />
        : <ChartPanel key={item.task} title={taskLabel(item.task)}>
          <Typography>{taskLabel(item.task)} are unavailable for this analysis profile.</Typography>
        </ChartPanel>)}
      <Box component="section" aria-label="Previous equal-duration window"><Stack spacing={2}>
        <Typography component="h2" variant="h6">Previous equal-duration window</Typography>
        {result.comparison.availability === 'available' ? <>
          <Typography>Previous dates: {windowLabel(result.comparison.requested_window ?? {})}. The previous window ends immediately before the current window starts. Coverage is assessed separately for each window.</Typography>
          <Typography>Available message dates: {windowLabel(result.comparison.effective_window ?? {}, 'No message dates in this selection')}</Typography>
          {result.comparison.tasks.map((task) => <TaskSection key={task.task} task={task} previous
            navigation={navigation?.availability === 'available' && navigation.previous ? { declaration: navigation.declaration!, rows: navigation.previous } : null} />)}
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
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button disabled={unpinned || result.evidence_page.offset === 0} onClick={() => void store.actions.read(Math.max(0, result.evidence_page.offset - result.evidence_page.limit))}>Previous sources</Button>
            <Button disabled={unpinned || !result.evidence_page.has_more || result.evidence_page.offset + result.evidence_page.limit > 1_000_000} onClick={() => void store.actions.read(result.evidence_page.offset + result.evidence_page.limit)}>Next sources</Button>
          </Stack>
        </Stack>
      </ChartPanel>
    </Stack>}
    {baseline && <LiteralFindings baseline={baseline} direction={direction} conversation={conversation} category={category?.filter} controller={literalsController} transport={transport} />}
    <MentionEvidenceDialog state={state.binding === binding ? state : { ...state, source: null, sourceLoading: false, sourceError: null }} actions={store.actions} />
  </Stack>;
}
