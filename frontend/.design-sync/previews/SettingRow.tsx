import { Box, Button, Divider, Stack } from '@mui/material';
import { Panel, SectionHeader, SettingRow } from 'onlyfans-analytics-frontend';

export function StoredMessages() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 640, p: 2 }}>
      <Panel>
        <Stack spacing={2}>
          <SectionHeader summary="Saved only on this computer." title="Stored messages" />
          <Divider />
          <SettingRow
            action={<Button size="small" sx={{ color: 'text.secondary' }}>Turn off archive</Button>}
            description="Keeping messages for 30 days"
            title="Archive"
          />
          <Divider />
          <SettingRow
            action={<Button size="small" variant="outlined">Download messages</Button>}
            description="Save your stored messages to a file. Deleting messages here doesn't delete the file."
            title="Download a copy"
          />
        </Stack>
      </Panel>
    </Box>
  );
}
