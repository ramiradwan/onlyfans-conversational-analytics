import { Box } from '@mui/material';
import {
  MessageStreamPane,
  previewConversations,
} from 'onlyfans-analytics-frontend';

export function ActiveConversation() {
  return (
    <Box sx={{ height: 480, maxWidth: 760 }}>
      <MessageStreamPane
        conversation={previewConversations[0]}
        isLoading={false}
        isOnline
      />
    </Box>
  );
}

export function NoSelection() {
  return (
    <Box sx={{ height: 320, maxWidth: 760 }}>
      <MessageStreamPane conversation={null} isLoading={false} isOnline={false} />
    </Box>
  );
}

export function Loading() {
  return (
    <Box sx={{ height: 380, maxWidth: 760 }}>
      <MessageStreamPane conversation={null} isLoading isOnline={false} />
    </Box>
  );
}
