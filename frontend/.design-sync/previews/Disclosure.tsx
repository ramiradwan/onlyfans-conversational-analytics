import { Box, Button, Stack, Typography } from '@mui/material';
import { Disclosure, Panel } from 'onlyfans-analytics-frontend';

function DeleteMessages() {
  return (
    <Stack spacing={2}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Deleted messages are removed from this computer and your numbers are updated without them.
      </Typography>
      <Box>
        <Button color="error" variant="outlined">
          Delete all messages
        </Button>
      </Box>
    </Stack>
  );
}

export function Closed() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 560, p: 2 }}>
      <Panel>
        <Disclosure label="Delete messages">
          <DeleteMessages />
        </Disclosure>
      </Panel>
    </Box>
  );
}

export function Open() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 560, p: 2 }}>
      <Panel>
        <Disclosure defaultOpen label="Delete messages">
          <DeleteMessages />
        </Disclosure>
      </Panel>
    </Box>
  );
}
