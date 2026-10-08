import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';

import type { QuestionState, QuestionStore } from '../../store/questionStore';

export function QuestionSourceDialog({ state, actions }: { state: QuestionState; actions: QuestionStore['actions'] }) {
  const { source, thread } = state;
  return (
    <Dialog open={state.sourceLoading || source !== null} onClose={actions.closeSource} fullWidth maxWidth="md"
      slotProps={{ paper: { sx: { height: 'min(36rem, calc(100% - 64px))' } } }}
      aria-labelledby="question-source-title" data-journey-state="questions.source">
      <DialogTitle id="question-source-title">Source conversation</DialogTitle>
      <DialogContent dividers sx={{ scrollbarGutter: 'stable' }}>
        {state.sourceLoading && <Typography role="status">Loading the matching message…</Typography>}
        {source && <Stack spacing={2}>
          <Typography variant="subtitle2">Matching message · {source.direction === 'inbound' ? 'Received' : 'Sent'} · {new Date(source.reference.sent_at).toLocaleString()}</Typography>
          <Box component="blockquote" sx={{ m: 0, p: 2, borderLeft: 3, borderColor: 'divider', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {source.text || 'This message has no text.'}
          </Box>
          <Typography variant="body2" color="text.secondary">This is the saved message used by the result. Conversation pages below show your saved history.</Typography>
          <Button variant="outlined" onClick={() => void actions.openConversation()} disabled={state.threadLoading} sx={{ alignSelf: 'flex-start' }}>
            <Box component="span" sx={{ display: 'grid' }}>
              <Box component="span" aria-hidden={Boolean(thread)} sx={{ gridArea: '1 / 1', visibility: thread ? 'hidden' : 'visible' }}>Open conversation</Box>
              <Box component="span" aria-hidden={!thread} sx={{ gridArea: '1 / 1', visibility: thread ? 'visible' : 'hidden' }}>Show latest messages</Box>
            </Box>
          </Button>
          <Typography variant="body2" role="status" data-question-status="history"
            sx={{ minHeight: '1.5em', lineHeight: 1.5 }}>{state.threadLoading ? 'Loading saved messages…' : '\u00a0'}</Typography>
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
