import {
  MemoryRouter,
  RecentConversations,
  previewConversations,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function Latest() {
  return (
    <MemoryRouter>
      <RecentConversations conversations={previewConversations} />
    </MemoryRouter>
  );
}
