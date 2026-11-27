import { Alert, Button, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography } from '@mui/material';
import { useEffect, useMemo } from 'react';
import { useStore } from 'zustand';

import { ChartPanel } from './ChartPanel';
import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { FindingsBaseline } from '../../analytics/analyticsReadModel';
import type { FindingsCategoryFilter, FindingsDirection } from '../../analytics/findingsContract';
import { formatCount } from '../../analytics/formatAnalytics';
import { createLiteralsStore, literalsBinding, type LiteralsStore } from '../../store/literalsStore';
import type { BridgeTransportStore } from '../../store/transportStore';
import { FindingsTableSkeleton } from '../ui/Skeletons';

interface Props {
  baseline: FindingsBaseline | null; direction: FindingsDirection; conversation: string | null;
  category?: FindingsCategoryFilter | null; controller?: LiteralsStore;
  transport: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}
export function LiteralFindings({ baseline, direction, conversation, category, controller, transport }: Props) {
  const store = useMemo(() => controller ?? createLiteralsStore(), [controller]);
  const state = useStore(store); const binding = literalsBinding(baseline, direction, conversation, category);
  useEffect(() => {
    const sync = () => store.actions.setContext(baseline, transport.getState(), direction, conversation, category);
    const stop = transport.subscribe(sync); sync();
    return stop;
  }, [baseline, direction, conversation, category, store, transport]);
  useEffect(() => () => store.actions.dispose(), [store]);
  const result = state.binding === binding ? state.result : null;
  const rows = result?.row_page; const evidence = result?.evidence_page;
  return <ChartPanel title="Recurring literal values" description="Exact saved mention spellings, grouped within their declared type. Case is preserved; these are not inferred preferences or requests.">
    <Stack spacing={1.5}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>The selected dates, direction, conversation and category apply. A category and a literal must be supported in the same message. Selecting a literal narrows sources; ranked values and coverage remain unchanged. No prevalence percentage is inferred from incomplete analysis or normalization.</Typography>
      <Stack direction="row" spacing={1}>
        <Button size="small" variant="outlined" disabled={!baseline || state.status === 'loading'} onClick={() => void store.actions.load()}>
          {state.status === 'idle' ? 'Show recurring literal values' : 'Refresh recurring literal values'}
        </Button>
      </Stack>
      {state.status === 'loading' && <FindingsTableSkeleton />}
      {state.message && <Alert severity={state.status === 'error' ? 'error' : 'info'}>{state.message}</Alert>}
      {result?.extraction && <Stack spacing={0.25} sx={{ bgcolor: 'surface.subtle', border: '1px solid', borderColor: 'divider', borderRadius: 1.5, px: 2, py: 1.25 }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Extraction coverage · Eligible: {formatCount(result.extraction.eligible_messages)} · Complete: {formatCount(result.extraction.complete_messages)} · Partial: {formatCount(result.extraction.partial_messages)}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Pending: {formatCount(result.extraction.pending_messages)} · Failed: {formatCount(result.extraction.failed_messages)} · Unsupported: {formatCount(result.extraction.unsupported_messages)} · Uncertain: {formatCount(result.extraction.uncertain_messages)}{!result.extraction.scan_complete && ' · Scan incomplete'}</Typography>
      </Stack>}
      {result?.availability === 'unavailable' && <Alert severity="info">{result.unavailable_reason === 'task_not_declared'
        ? 'Mention extraction is unavailable for this profile.' : result.unavailable_reason === 'capacity'
          ? 'Literal normalization reached its storage bound. Saved extraction findings remain available.' : 'No verified literal grouping is available for this saved analysis. Saved extraction findings remain available.'}</Alert>}
      {result?.availability === 'available' && rows && evidence && <>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Normalization coverage · Eligible occurrences: {formatCount(result.normalization_coverage!.eligible_occurrences)} · Mapped: {formatCount(result.normalization_coverage!.mapped_occurrences)} · Unrepresentable: {formatCount(result.normalization_coverage!.unrepresentable_occurrences)} · Supporting messages: {formatCount(result.normalization_coverage!.supporting_messages)}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Unrepresentable values retain their exact extraction occurrences. Partial support is observed support; uncertain and unassessed messages are not confirmed absence.</Typography>
        <TableContainer sx={{ maxHeight: '24rem' }}><Table stickyHeader size="small" aria-label="Recurring literal values">
          <TableHead><TableRow><TableCell>Literal value</TableCell><TableCell>Type</TableCell><TableCell>Distinct conversations</TableCell><TableCell>Distinct messages</TableCell><TableCell>Exact occurrences</TableCell><TableCell>Complete / partial messages</TableCell><TableCell>Sources</TableCell></TableRow></TableHead>
          <TableBody>{rows.items.map((row) => <TableRow key={row.node_id}>
            <TableCell sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{row.literal_key}</TableCell>
            <TableCell>{row.display_label}{row.label_status === 'id_only' && <Typography variant="caption" component="p">Display name unavailable; category ID shown.</Typography>}</TableCell>
            <TableCell>{formatCount(row.supporting_conversations)}</TableCell><TableCell>{formatCount(row.supporting_messages)}</TableCell><TableCell>{formatCount(row.occurrences)}</TableCell>
            <TableCell>{formatCount(row.complete_supporting_messages)} / {formatCount(row.partial_supporting_messages)}</TableCell>
            <TableCell><Button size="small" variant="outlined" aria-label={`View sources for literal ${row.literal_key} (${row.display_label})`} aria-pressed={result.node_id === row.node_id} onClick={() => void store.actions.selectValue(row.node_id)}>View sources</Button></TableCell>
          </TableRow>)}</TableBody>
        </Table></TableContainer>
        {!rows.total && <Typography variant="body2" sx={{ color: 'text.secondary' }}>No mapped literal values in this selection. Check extraction and normalization coverage above.</Typography>}
        <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" disabled={rows.offset === 0} onClick={() => void store.actions.pageRows(Math.max(0, rows.offset - rows.limit))}>Previous literal values</Button>
          <Button size="small" variant="outlined" disabled={!rows.has_more || rows.offset + rows.limit > 100_000} onClick={() => void store.actions.pageRows(rows.offset + rows.limit)}>Next literal values</Button>
          <Typography variant="caption" sx={{ color: 'text.secondary', ml: 0.5 }}>Values {formatCount(rows.offset + (rows.items.length ? 1 : 0))}–{formatCount(rows.offset + rows.items.length)} of {formatCount(rows.total)}.</Typography>
        </Stack>
        {result.node_id && <Alert severity="info">Showing sources for the selected literal value. <Button size="small" variant="outlined" sx={{ ml: 1 }} onClick={() => void store.actions.selectValue(null)}>Clear literal selection</Button></Alert>}
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Literal sources {formatCount(evidence.offset + (evidence.items.length ? 1 : 0))}–{formatCount(evidence.offset + evidence.items.length)} of {formatCount(evidence.total)}.</Typography>
        {!evidence.items.length && <Typography variant="body2" sx={{ color: 'text.secondary' }}>No literal source references on this page.</Typography>}
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          {evidence.items.map((item, index) => <Button key={`${item.node_id}:${index}`} size="small" variant="outlined" onClick={() => void store.actions.openSource(item.reference)}>
            Literal source {evidence.offset + index + 1}: {result.extraction!.rows.find((r) => r.category_id === item.category_id)?.display_label ?? item.category_id} ({item.assessment})
          </Button>)}
        </Stack>
        <Stack direction="row" spacing={1}>
          <Button size="small" variant="outlined" disabled={evidence.offset === 0} onClick={() => void store.actions.pageEvidence(Math.max(0, evidence.offset - evidence.limit))}>Previous literal sources</Button>
          <Button size="small" variant="outlined" disabled={!evidence.has_more || evidence.offset + evidence.limit > 1_000_000} onClick={() => void store.actions.pageEvidence(evidence.offset + evidence.limit)}>Next literal sources</Button>
        </Stack>
      </>}
      <MentionEvidenceDialog state={state.binding === binding ? state : { source: null, sourceLoading: false, sourceError: null }} actions={store.actions} />
    </Stack>
  </ChartPanel>;
}
