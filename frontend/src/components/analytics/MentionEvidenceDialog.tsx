import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';

import type { FindingsState, FindingsStore } from '../../store/findingsStore';

export function MentionEvidenceDialog({ state, actions }: { state: FindingsState; actions: FindingsStore['actions'] }) {
  const source = state.source;
  const span = source?.browser_span;
  return <Dialog open={state.sourceLoading || source !== null || state.sourceError !== null} onClose={actions.closeSource}
    fullWidth maxWidth="md" aria-labelledby="finding-source-title" data-journey-state="findings.source">
    <DialogTitle id="finding-source-title">Finding source</DialogTitle>
    <DialogContent dividers>
      {state.sourceLoading && <Typography role="status">Verifying the saved source message…</Typography>}
      {state.sourceError && <Alert severity="warning">{state.sourceError}</Alert>}
      {source && <Stack spacing={2}>
        <Typography>{source.direction === 'inbound' ? 'Received' : 'Sent'} · {new Date(source.reference.sent_at).toLocaleString()}</Typography>
        <Box component="blockquote" sx={{ m: 0, p: 2, borderLeft: 3, borderColor: 'divider', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {span ? <>{source.text.slice(0, span.start)}<mark>{source.text.slice(span.start, span.end)}</mark>{source.text.slice(span.end)}</> : source.text || 'This message has no text.'}
        </Box>
        <Typography variant="body2">This saved source version was verified when opened. A highlight identifies the exact mention occurrence; a topic finding applies to the message.</Typography>
      </Stack>}
    </DialogContent>
    <DialogActions><Button onClick={actions.closeSource}>Close</Button></DialogActions>
  </Dialog>;
}
