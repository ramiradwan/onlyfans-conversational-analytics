import { Box } from '@mui/material';
import {
  OperatorInboxView,
  createPreviewInboxStore,
} from 'onlyfans-analytics-frontend';

const previewMessageApi = {
  getPage: async () => new Promise<never>(() => {}),
};

export function LiveInbox() {
  return (
    <Box sx={{ height: 620, minWidth: 840 }}>
      <OperatorInboxView
        messageApi={previewMessageApi}
        store={createPreviewInboxStore()}
      />
    </Box>
  );
}
