import { Box } from '@mui/material';
import {
  OperatorInboxView,
  createPreviewInboxStore,
} from 'onlyfans-analytics-frontend';

export function LiveInbox() {
  return (
    <Box sx={{ height: 620, minWidth: 840 }}>
      <OperatorInboxView store={createPreviewInboxStore()} />
    </Box>
  );
}
