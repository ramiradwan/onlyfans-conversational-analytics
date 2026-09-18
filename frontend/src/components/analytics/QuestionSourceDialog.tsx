import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';

import type { QuestionState, QuestionStore } from '../../store/questionStore';

export function QuestionSourceDialog({ state, actions }: { state: QuestionState; actions: QuestionStore['actions'] }) {
  const { source, thread } = state;
  return (
    <Dialog open={state.sourceLoading || source !== null} onClose={actions.closeSource} fullWidth maxWidth="md"
      aria-labelledby="question-source-title" data-journey-state="questions.source">
      <DialogTitle id="question-source-title">Source conversation</DialogTitle>
      <DialogContent dividers>
        {state.sourceLoading && <Typography role="status">Loading the matching message…</Typography>}
        {source && <Stack spacing={2}>
          <Typography variant="subtitle2">Matching message · {source.direction === 'inbound' ? 'Received' : 'Sent'} · {new Date(source.reference.sent_at).toLocaleString()}</Typography>
          <Box component="blockquote" sx={{ m: 0, p: 2, borderLeft: 3, borderColor: 'divider', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {source.text || 'This message has no text.'}
          </Box>
          <Typography variant="body2" color="text.secondary">This is the saved message used by the result. Conversation pages below show your saved history.</Typography>
          <Button variant="outlined" onClick={() => void actions.openConversation()} disabled={state.threadLoading} sx={{ alignSelf: 'flex-start' }}>
            {thread ? 'Show latest messages' : 'Open conversation'}
          </Button>
          {state.threadLoading && <Typography role="status">Loading saved messages…</Typography>}
          {thread && <>
            <Typography component="h3" variant="h6">Saved messages</Typography>
            {thread.conversation_coverage.status !== 'complete' && <Alert severity="info">Only messages saved so far are shown. This may not be the full conversation.</Alert>}
            <Stack component="ol" spacing={1.5} sx={{ pl: 2 }} aria-label="Saved messages">
              {thread.items.map((message) => <Box component="li" key={message.message_id} sx={{ overflowWrap: 'anywhere' }}>
                <Typography variant="caption" color="text.secondary">
                  {message.direction === 'inbound' ? 'Received' : 'Sent'} · {new Date(message.sent_at).toLocaleString()}
                </Typography>
                <Typography sx={{ whiteSpace: 'pre-wrap' }}>{message.text || 'This message has no text.'}</Typography>
              </Box>)}
            </Stack>
            {thread.items.length === 0 && <Typography>No messages are available on this page.</Typography>}
            {thread.older_cursor && <Button onClick={() => void actions.openConversation(true)}>Show older messages</Button>}
          </>}
        </Stack>}
      </DialogContent>
      <DialogActions><Button onClick={actions.closeSource}>Close</Button></DialogActions>
    </Dialog>
  );
}
