import { Alert, Box, Button, MenuItem, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { useStore } from 'zustand';

import { QuestionSourceDialog } from './QuestionSourceDialog';
import type { QuestionId } from '../../analytics/questionContract';
import { initialQuestionDates, localDate, makeQuestionPlan } from '../../analytics/questionWindow';
import { createQuestionStore, type QuestionStore } from '../../store/questionStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../../store/transportStore';

const labels: Record<QuestionId, string> = {
  'no_later_creator_reply.v1': 'No later reply from you',
  'pricing_discussions.v1': 'Pricing discussions',
};
export function ConversationQuestions({ controller, transport = bridgeTransportStore }: {
  controller?: QuestionStore;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}) {
  const questions = useMemo(() => controller ?? createQuestionStore(), [controller]);
  const state = useStore(questions);
  const [question, setQuestion] = useState<QuestionId>('no_later_creator_reply.v1');
  const [dates, setDates] = useState(initialQuestionDates);
  const [validation, setValidation] = useState<string | null>(null);
  useEffect(() => {
    const sync = () => questions.actions.setContext(transport.getState());
    const unsubscribe = transport.subscribe(sync);
    sync();
    return () => { unsubscribe(); questions.actions.dispose(); };
  }, [questions, transport]);
  const enabled = state.catalog?.questions.find((item) => item.question === question)?.enabled === true;
  const busy = state.status === 'running' || state.status === 'loading';
  const result = state.result;
  function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      const plan = makeQuestionPlan(question, dates.start, dates.end);
      setValidation(null);
      void questions.actions.run(plan);
    } catch (error) { setValidation(error instanceof Error ? error.message : 'Choose a valid date range.'); }
  }
  function changeDates(field: 'start' | 'end', value: string) {
    setDates((previous) => ({ ...previous, [field]: value }));
    setValidation(null);
    questions.actions.invalidate('Dates changed. Run the question again.');
  }
  return (
    <Paper variant="outlined" component="section" aria-labelledby="conversation-questions-title" sx={{ p: { xs: 2, sm: 3 }, minWidth: 0 }}
      data-journey-state="questions.controls">
      <Stack spacing={2}>
        <Box><Typography id="conversation-questions-title" component="h2" variant="h6">Conversation questions</Typography>
          <Typography color="text.secondary">Find saved conversations and inspect the messages behind each result.</Typography></Box>
        <Stack component="form" onSubmit={submit} spacing={2}>
          <TextField select label="Question" value={question} onChange={(event) => {
            setQuestion(event.target.value as QuestionId); questions.actions.invalidate('Question changed. Run it again.');
          }}>
            {Object.entries(labels).map(([id, label]) => <MenuItem key={id} value={id}
              disabled={state.catalog?.questions.find((item) => item.question === id)?.enabled !== true}>
              {label}{state.catalog?.questions.find((item) => item.question === id)?.enabled !== true ? ' — not available yet' : ''}
            </MenuItem>)}
          </TextField>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField type="date" label="Start date" value={dates.start} onChange={(event) => changeDates('start', event.target.value)}
              slotProps={{ inputLabel: { shrink: true }, htmlInput: { max: localDate(new Date()) } }} fullWidth />
            <TextField type="date" label="End date" value={dates.end} onChange={(event) => changeDates('end', event.target.value)}
              slotProps={{ inputLabel: { shrink: true }, htmlInput: { max: localDate(new Date()) } }} fullWidth />
          </Stack>
          <Typography variant="body2" color="text.secondary">Dates use {Intl.DateTimeFormat().resolvedOptions().timeZone}. These filters apply only to conversation questions.</Typography>
          {validation && <Alert severity="error">{validation}</Alert>}
          <Stack direction="row" spacing={1}>
            <Button type="submit" variant="contained" disabled={!enabled || !state.canRun || busy}>{busy ? 'Loading…' : 'Run question'}</Button>
            {!state.catalog && state.canRun && !busy && <Button onClick={() => void questions.actions.catalog()}>Retry</Button>}
          </Stack>
        </Stack>
        <Typography variant="body2" color="text.secondary">This checks saved messages only. A message without a later reply does not necessarily need one. Some messages do not include enough information to determine reply status.</Typography>
        {state.catalog?.questions.some((item) => item.question === 'pricing_discussions.v1' && !item.enabled) &&
          <Typography variant="body2" color="text.secondary">Pricing discussions are not available yet.</Typography>}
        {busy && <Typography role="status" aria-live="polite">{state.status === 'running' ? 'Checking saved conversations…' : 'Loading available questions…'}</Typography>}
        {state.message && <Alert severity={state.status === 'error' ? 'error' : 'info'}
          data-journey-state="questions.unavailable">{state.message}</Alert>}
        {result && <Stack spacing={1.5} data-journey-state="questions.results" aria-live="polite">
          <Typography variant="body2">Checked {new Date(result.checked_at).toLocaleString()}. Follow-up checked through {new Date(result.question.cutoff).toLocaleString()}.</Typography>
          {result.question.selection_clipped_by_retention && <Alert severity="info">Only messages within the available 90-day analysis history were checked.</Alert>}
          {result.page.coverage.history !== 'complete' && <Alert severity="info">The saved history may be incomplete. These results describe only the messages checked.</Alert>}
          {result.page.undetermined_conversation_count > 0 && <Alert severity="warning" data-journey-state="questions.undetermined">
            {result.question.plan.question === 'pricing_discussions.v1' ? 'Pricing status' : 'Reply status'} could not be determined for {result.page.undetermined_conversation_count} conversations. They are not counted as matches.
          </Alert>}
          {result.page.rows.length === 0 && <Typography>
            {result.page.undetermined_conversation_count > 0 ? 'No confirmed matches. Some conversations could not be assessed.' : 'No matches in the messages checked.'}
          </Typography>}
          {result.page.truncated && <Alert severity="warning">Only part of the data was checked. This is not a complete result.</Alert>}
          {result.page.rows.length > 0 && <>
            <Typography>{result.page.total_matching_conversations ?? result.page.rows.length} {result.page.total_matching_conversations == null ? 'matches on this page' : 'matching conversations'}</Typography>
            <TableContainer><Table size="small" aria-label="Conversation question results">
              <TableHead><TableRow><TableCell>Conversation</TableCell><TableCell>Matching message</TableCell><TableCell>Source</TableCell></TableRow></TableHead>
              <TableBody>{result.page.rows.map((row, index) => <TableRow key={row.conversation_ref}>
                <TableCell>Conversation {index + 1}
                  <Typography variant="caption" component="p">{row.reason === 'pricing_discussion' ? 'Pricing discussion' : 'No later reply in saved messages'}</Typography>
                </TableCell>
                <TableCell>{new Date(row.latest_evidence_at).toLocaleString()}</TableCell>
                <TableCell>{row.evidence.map((reference, n) => <Button key={reference.message_ref} size="small"
                  aria-label={`View source ${n + 1} for conversation ${index + 1}`} onClick={() => void questions.actions.openSource(reference)}>
                  {row.evidence.length > 1 ? `Message ${n + 1}` : 'View message'}
                </Button>)}{row.evidence_truncated && <Typography variant="caption">Some matching messages are not shown.</Typography>}</TableCell>
              </TableRow>)}</TableBody>
            </Table></TableContainer>
          </>}
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Button disabled={state.pageNumber <= 1} onClick={() => void questions.actions.previous()}>Previous page</Button>
            <Typography variant="body2">Page {state.pageNumber}</Typography>
            <Button disabled={!result.next_cursor || state.pageNumber >= 50} onClick={() => void questions.actions.next()}>Next page</Button>
          </Stack>
        </Stack>}
      </Stack>
      <QuestionSourceDialog state={state} actions={questions.actions} />
    </Paper>
  );
}
