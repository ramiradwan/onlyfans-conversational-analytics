import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';

import type { FindingsState, FindingsStore } from '../../store/findingsStore';

export function MentionEvidenceDialog({ state, actions }: {
  state: Pick<FindingsState, 'source' | 'sourceLoading' | 'sourceError'> & Partial<Pick<FindingsState, 'sourceContext' | 'contextLoading' | 'contextError'>>;
  actions: Pick<FindingsStore['actions'], 'closeSource'> & Partial<Pick<FindingsStore['actions'], 'showContext'>>;
}) {
  const source = state.source;
  const span = source?.browser_span;
  return <Dialog open={state.sourceLoading || source !== null || state.sourceError !== null} onClose={actions.closeSource}
    fullWidth maxWidth="md" aria-labelledby="finding-source-title" data-journey-state="findings.source">
    <DialogTitle id="finding-source-title">Finding source</DialogTitle>
    <DialogContent dividers>
      {state.sourceLoading && <Typography role="status">Verifying the saved source message...</Typography>}
      {state.sourceError && <Alert severity="warning">{state.sourceError}</Alert>}
      {source && <Stack spacing={2}>
        <Typography>{source.direction === 'inbound' ? 'Received' : 'Sent'}  / {new Date(source.reference.sent_at).toLocaleString()}</Typography>
        {!state.sourceContext && <Box component="blockquote" sx={{ m: 0, p: 2, borderLeft: 3, borderColor: 'divider', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {span ? <>{source.text.slice(0, span.start)}<mark>{source.text.slice(span.start, span.end)}</mark>{source.text.slice(span.end)}</> : source.text || 'This message has no text.'}
        </Box>}
        <Typography variant="body2">This saved source version was verified when opened. A highlight identifies the exact mention occurrence; a topic finding applies to the message.</Typography>
        {actions.showContext && <Button onClick={actions.showContext} disabled={state.contextLoading}>Show surrounding messages</Button>}
        {state.contextLoading && <Typography role="status">Verifying surrounding saved messages...</Typography>}
        {state.contextError && <Alert severity="warning">{state.contextError}</Alert>}
        {state.sourceContext && <Stack spacing={2} aria-label="Surrounding saved messages">
          <Typography variant="h6">Surrounding saved messages</Typography>
          <Typography variant="body2">These are retained messages around the cited source. They are raw conversation context, and may not represent the complete conversation history or classified findings.</Typography>
          <Typography>{state.sourceContext.has_earlier_stored_items ? 'More earlier saved messages exist.' : 'No additional earlier saved messages.'}</Typography>
          {state.sourceContext.items.map((item, index) => <Box key={item.reference.message_ref} sx={{ p: 2, border: 1, borderColor: index === state.sourceContext!.anchor_index ? 'primary.main' : 'divider' }}>
            <Typography variant="body2">{index === state.sourceContext!.anchor_index ? 'Cited source / ' : ''}{item.direction === 'inbound' ? 'Received' : 'Sent'} / {new Date(item.reference.sent_at).toLocaleString()}</Typography>
            <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.browser_span ? <>{item.text.slice(0, item.browser_span.start)}<mark>{item.text.slice(item.browser_span.start, item.browser_span.end)}</mark>{item.text.slice(item.browser_span.end)}</> : item.text || 'This message has no text.'}</Typography>
          </Box>)}
          <Typography>{state.sourceContext.has_later_stored_items ? 'More later saved messages exist.' : 'No additional later saved messages.'}</Typography>
        </Stack>}
      </Stack>}
    </DialogContent>
    <DialogActions><Button onClick={actions.closeSource}>Close</Button></DialogActions>
  </Dialog>;
}
