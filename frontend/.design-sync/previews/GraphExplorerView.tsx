import { Box } from '@mui/material';
import { GraphExplorerView } from 'onlyfans-analytics-frontend';

export function EmptyWorkspace() {
  return (
    <Box sx={{ height: 420, minWidth: 680 }}>
      <GraphExplorerView />
    </Box>
  );
}
