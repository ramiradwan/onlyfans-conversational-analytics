import {
  Box,
  OperatorInboxView,
  createPreviewInboxStore,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

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
