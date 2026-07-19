import { Box, Stack, Typography } from '@mui/material';
import {
  MessageBubble,
  createPreviewMessage,
} from 'onlyfans-analytics-frontend';

export function ConversationExchange() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 680 }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 2 }}>
        Conversation with Bailey Hart
      </Typography>
      <Stack component="ol" spacing={1.5} sx={{ listStyle: 'none', m: 0, p: 0 }}>
        <MessageBubble
          message={createPreviewMessage(
            'preview-inbound',
            'That behind-the-scenes set was exactly what I hoped for.',
            '2026-07-18T09:58:00.000Z',
            'inbound',
            'positive',
          )}
        />
        <MessageBubble
          message={createPreviewMessage(
            'preview-outbound',
            'I am glad you liked it — there is another set coming Friday.',
            '2026-07-18T10:00:00.000Z',
            'outbound',
            'positive',
          )}
        />
        <MessageBubble
          message={createPreviewMessage(
            'preview-negative',
            'The download link is not opening for me.',
            '2026-07-18T10:04:00.000Z',
            'inbound',
            'negative',
          )}
        />
      </Stack>
    </Box>
  );
}
