import { Box } from '@mui/material';
import {
  MemoryRouter,
  RecentConversations,
  previewConversations,
} from 'onlyfans-analytics-frontend';

export function Latest() {
  return (
    <MemoryRouter>
      <Box sx={{ bgcolor: 'background.default', p: 2 }}>
        <RecentConversations conversations={previewConversations} />
      </Box>
    </MemoryRouter>
  );
}
