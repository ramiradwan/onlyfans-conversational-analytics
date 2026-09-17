import { Panel, SectionHeader, Stack } from 'onlyfans-analytics-frontend';

export function WithStatus() {
  return (
    <Stack spacing={2} sx={{ bgcolor: 'background.default', maxWidth: 640, p: 2 }}>
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
