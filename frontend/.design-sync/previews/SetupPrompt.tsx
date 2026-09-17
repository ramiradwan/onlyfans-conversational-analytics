import { Box, MemoryRouter, SetupPrompt } from 'onlyfans-analytics-frontend';

export function FirstRun() {
  return (
    <MemoryRouter>
      <Box sx={{ bgcolor: 'background.default', p: 3 }}>
        <SetupPrompt title="Finish setup" />
      </Box>
    </MemoryRouter>
  );
}

export function ExtensionConnected() {
  return (
    <MemoryRouter>
      <Box sx={{ bgcolor: 'background.default', p: 3 }}>
        <SetupPrompt extensionConnected title="Finish setup" />
      </Box>
    </MemoryRouter>
  );
}

export function FullAnalyticsRemaining() {
  return (
    <MemoryRouter>
      <Box sx={{ bgcolor: 'background.default', p: 3 }}>
        <SetupPrompt extensionConnected historyEnabled title="Finish setup" />
      </Box>
    </MemoryRouter>
  );
}
