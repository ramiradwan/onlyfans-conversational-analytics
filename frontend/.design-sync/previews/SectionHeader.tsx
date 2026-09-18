import { Panel, SectionHeader, Stack } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function WithStatus() {
  return (
    <Stack spacing={2} sx={{ maxWidth: 640 }}>
      <Panel>
        <SectionHeader
          status={{ label: 'Not connected', tone: 'default' }}
          summary="Connect the browser extension so your messages reach this app."
          title="Browser extension"
        />
      </Panel>
      <Panel>
        <SectionHeader
          status={{ label: 'On', tone: 'success' }}
          summary="Adds tone, reply, and topic insights to your conversations."
          title="Full analytics"
        />
      </Panel>
    </Stack>
  );
}
