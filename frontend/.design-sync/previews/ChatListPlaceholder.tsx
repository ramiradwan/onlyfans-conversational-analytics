import {
  ChatListPlaceholder,
  Panel,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function ConversationsLoading() {
  return (
    <Panel sx={{ maxWidth: 420, p: 2, gap: 1 }}>
      <Typography variant="subtitle1">Conversations</Typography>
      <ChatListPlaceholder rows={5} />
    </Panel>
  );
}
