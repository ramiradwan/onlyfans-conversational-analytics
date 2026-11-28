import { Alert, Button, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';

import { MentionEvidenceDialog } from './MentionEvidenceDialog';
import type { FindingsBaseline } from '../../analytics/analyticsReadModel';
import { literalKey } from '../../analytics/literalsContract';
import { evidenceKey, expectedAccountRef, type QuestionEvidence, type SourceEvidence } from '../../analytics/questionContract';
import { exportRecurringReport, type RecurringMentionsClient, type RecurringReport, type RecurringReportRequest, type RecurringRow } from '../../analytics/recurringMentions';
import { exportSavedJson } from '../../services/savedDataExport';
import { Panel, SectionHeader } from '../ui';

const pageSize = 20;
const number = new Intl.NumberFormat();
const date = (value: string) => new Date(value).toLocaleString();
const firstPage = { phraseId: null as string | null, normalization: null as string | null, rowOffset: 0, evidenceOffset: 0 };
type Page = typeof firstPage;
type SourceState = { reference: QuestionEvidence; source: SourceEvidence | null; error: boolean };
type Props = {
  account: string; baseline: FindingsBaseline | null; client: RecurringMentionsClient;
  onRefreshBaseline: () => void; selection?: { current: string | null };
};

function Coverage({ report }: { report: RecurringReport }) {
  const c = report.coverage;
  if (!c) return null;
  return <Stack spacing={0.5}>
    <Typography variant="body2">
      {number.format(c.complete_messages)} of {number.format(c.eligible_messages)} received messages fully assessed
      {c.partial_messages > 0 && ` · ${number.format(c.partial_messages)} partly assessed`}.
    </Typography>
    {c.pending_messages + c.failed_messages + c.uncertain_messages + c.unsupported_messages > 0 &&
      <Typography variant="body2" color="text.secondary">
        Pending {number.format(c.pending_messages)} · Failed {number.format(c.failed_messages)} ·
        {' '}Uncertain {number.format(c.uncertain_messages)} · Unsupported {number.format(c.unsupported_messages)}
      </Typography>}
    {(!c.scan_complete || c.unrepresentable_occurrences > 0) && <Typography variant="body2" color="text.secondary">
      {!c.scan_complete && 'The scan covers part of the saved history. '}
      {c.unrepresentable_occurrences > 0 && `${number.format(c.unrepresentable_occurrences)} extracted occurrences could not be grouped.`}
    </Typography>}
  </Stack>;
}

/** Entry is keyed by the authenticated desktop session. The snapshot key also excludes stale report or source reads. */
export function RecurringMentionsReport({ account, baseline, client, onRefreshBaseline, selection }: Props) {
  const [requested, setRequested] = useState(false);
  const [page, setPage] = useState<Page>(firstPage);
  const [revision, setRevision] = useState(0);
  const [read, setRead] = useState<{ key: string; report: RecurringReport | null; error?: string } | null>(null);
  const [source, setSource] = useState<SourceState | null>(null);
  const sourceController = useRef<AbortController | null>(null);
  const exportController = useRef<AbortController | null>(null);
  const [exportStatus, setExportStatus] = useState<'idle' | 'exporting' | 'error'>('idle');
  const localSelection = useRef<string | null>(null);
  const remembered = selection ?? localSelection;
  const snapshotKey = baseline && JSON.stringify([baseline.account_ref, baseline.projection_generation, baseline.projection_digest]);
  const previousSnapshot = useRef<string | null>(null);
  const key = JSON.stringify([snapshotKey, page, revision]);
  const report = read?.key === key ? read.report : null;
  const readError = read?.key === key ? read.error : null;
  const loading = requested && Boolean(baseline) && !report && !readError;

  useEffect(() => {
    const controller = new AbortController();
    sourceController.current?.abort();
    exportController.current?.abort();
    setExportStatus('idle');
    setSource(null);
    const changed = snapshotKey !== previousSnapshot.current;
    previousSnapshot.current = snapshotKey;
    if (!requested || !baseline || !snapshotKey) return;
    if (changed && (page.normalization !== null || page.rowOffset || page.evidenceOffset)) {
      setPage(firstPage);
      return;
    }
    const request: RecurringReportRequest = { ...page, baseline, limit: pageSize };
    void (async () => {
      if (await expectedAccountRef(account) !== baseline.account_ref) throw new Error('The account changed.');
      if (controller.signal.aborted) return;
      const result = await client.report(request, controller.signal);
      if (controller.signal.aborted) return;
      const priorId = remembered.current;
      if (page.normalization === null && priorId !== null && result.normalization_sha256 !== null) {
        let rows = result.row_page;
        while (true) {
          const index = rows.items.findIndex((row) => row.phrase_id === priorId);
          if (index !== -1) {
            const rowOffset = Math.floor((rows.offset + index) / pageSize) * pageSize;
            setPage({ ...firstPage, rowOffset, phraseId: priorId, normalization: result.normalization_sha256 });
            return;
          }
          if (!rows.has_more) break;
          const next = await client.report({ ...request, normalization: result.normalization_sha256,
            rowOffset: rows.offset + rows.items.length, limit: 100 }, controller.signal);
          if (controller.signal.aborted) return;
          if (next.snapshot_sha256 !== result.snapshot_sha256 || next.row_page.total !== result.row_page.total) {
            throw new Error('The saved report changed.');
          }
          rows = next.row_page;
        }
        remembered.current = null;
      }
      setRead({ key, report: result });
    })().catch(() => {
      if (!controller.signal.aborted) setRead({ key, report: null, error: 'The saved report could not be verified. Check for updates to try again.' });
    });
    return () => controller.abort();
  }, [account, baseline, client, key, page, remembered, requested, snapshotKey]);
  useEffect(() => () => { sourceController.current?.abort(); exportController.current?.abort(); }, []);

  function refresh() {
    remembered.current = report?.phrase_id ?? remembered.current;
    setPage(firstPage);
    setRequested(true);
    setRevision((value) => value + 1);
    onRefreshBaseline();
  }
  function select(row: RecurringRow) {
    if (!report?.normalization_sha256) return;
    remembered.current = row.phrase_id;
    setPage({ ...page, phraseId: row.phrase_id, normalization: report.normalization_sha256, evidenceOffset: 0 });
  }
  async function openQuote(reference: QuestionEvidence, row: RecurringRow) {
    if (!report?.evidence_page.items.some((item) => item.phrase_id === row.phrase_id && evidenceKey(item.reference) === evidenceKey(reference))) return;
    sourceController.current?.abort();
    const controller = new AbortController();
    sourceController.current = controller;
    setSource({ reference, source: null, error: false });
    try {
      const resolved = await client.evidence(reference, controller.signal), span = resolved.browser_span;
      if (!span || resolved.direction !== 'inbound' || literalKey(resolved.text.slice(span.start, span.end)) !== row.literal_key) {
        throw new Error('The saved quote changed.');
      }
      if (!controller.signal.aborted) setSource({ reference, source: resolved, error: false });
    } catch {
      if (!controller.signal.aborted) setSource({ reference, source: null, error: true });
    }
  }
  const selected = report?.row_page.items.find((row) => row.phrase_id === report.phrase_id);
  const activeSource = report && source && report.evidence_page.items.some((item) => evidenceKey(item.reference) === evidenceKey(source.reference)) ? source : null;
  async function exportReport() {
    if (!baseline || report?.availability !== 'available' || exportController.current) return;
    const controller = new AbortController(); exportController.current = controller; setExportStatus('exporting');
    try {
      await exportSavedJson('recurring-mentions.json', async () => {
        const saved = await exportRecurringReport(client, baseline, report, controller.signal);
        return new Response(JSON.stringify(saved, null, 2), { headers: { 'Content-Type': 'application/json' } });
      }, controller.signal);
      setExportStatus('idle');
    } catch (error) {
      if (!controller.signal.aborted) setExportStatus(error instanceof DOMException && error.name === 'AbortError' ? 'idle' : 'error');
    }
    finally { if (exportController.current === controller) exportController.current = null; }
  }
  return <Panel component="section" aria-label="Saved recurring mentions">
    <SectionHeader title="Saved report" summary="Exact phrases used by at least two senders in the last 90 days of the report." />
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}><Button variant="outlined" disabled={loading} onClick={refresh}>
        {requested ? 'Check for report updates' : 'Open saved report'}
      </Button>{report?.availability === 'available' && <Button variant="outlined" disabled={exportStatus === 'exporting'} onClick={() => void exportReport()}>
        {exportStatus === 'exporting' ? 'Preparing export…' : 'Export saved report'}
      </Button>}</Stack>
      {exportStatus === 'error' && <Alert severity="warning">The complete report could not be exported. Check the report for updates and try again.</Alert>}
      {loading && <Typography role="status">Checking saved findings…</Typography>}
      {requested && !baseline && <Typography role="status">Waiting for a saved report for this account. You can check again here.</Typography>}
      {readError && <Alert severity="warning">{readError}</Alert>}
      {report && <>
        <Typography variant="body2">Report period · {date(report.requested_window.start!)} – {date(report.requested_window.end!)}</Typography>
        <Coverage report={report} />
        {report.availability === 'unavailable' ? <Typography>No saved phrase grouping is available for this report.</Typography> :
          report.row_page.total === 0 ? <Typography>No recurring phrase was found in the assessed messages.</Typography> : <>
            {report.row_page.items.map((row) => <Stack key={row.phrase_id} spacing={0.5} sx={{ alignItems: 'flex-start' }}>
              <Typography component="h3" variant="subtitle1" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{row.literal_key}</Typography>
              <Typography variant="body2">{number.format(row.supporting_senders)} senders · {number.format(row.supporting_messages)} messages</Typography>
              <Button size="small" aria-label={`Show quotes for ${row.literal_key}`} aria-pressed={report.phrase_id === row.phrase_id} onClick={() => select(row)}>Show quotes</Button>
            </Stack>)}
            {report.row_page.total > pageSize && <Stack direction="row" spacing={1}>
              <Button size="small" disabled={page.rowOffset === 0} onClick={() => {
                remembered.current = null;
                setPage({ ...firstPage, normalization: report.normalization_sha256, rowOffset: Math.max(0, page.rowOffset - pageSize) });
              }}>Previous phrases</Button>
              <Button size="small" disabled={!report.row_page.has_more} onClick={() => {
                remembered.current = null;
                setPage({ ...firstPage, normalization: report.normalization_sha256, rowOffset: page.rowOffset + pageSize });
              }}>Next phrases</Button>
            </Stack>}
          </>}
        {selected && <Stack component="section" spacing={1} aria-label={`Quotes for ${selected.literal_key}`}>
          <Typography component="h3" variant="subtitle2">Quotes for “{selected.literal_key}”</Typography>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            {report.evidence_page.items.map((item, index) => <Button key={evidenceKey(item.reference)} size="small" variant="outlined"
              onClick={() => void openQuote(item.reference, selected)}>Open quote {page.evidenceOffset + index + 1} · {date(item.reference.sent_at)}</Button>)}
          </Stack>
          {report.evidence_page.total > pageSize && <Stack direction="row" spacing={1}>
            <Button size="small" disabled={page.evidenceOffset === 0} onClick={() => setPage({ ...page, evidenceOffset: Math.max(0, page.evidenceOffset - pageSize) })}>Previous quotes</Button>
            <Button size="small" disabled={!report.evidence_page.has_more} onClick={() => setPage({ ...page, evidenceOffset: page.evidenceOffset + pageSize })}>Next quotes</Button>
          </Stack>}
        </Stack>}
      </>}
    </Stack>
    <MentionEvidenceDialog state={{ source: activeSource?.source ?? null, sourceLoading: Boolean(activeSource && !activeSource.error && !activeSource.source),
      sourceError: activeSource?.error ? 'This saved quote could not be verified. Check the report for updates.' : null }}
      actions={{ closeSource: () => { sourceController.current?.abort(); setSource(null); } }} />
  </Panel>;
}
