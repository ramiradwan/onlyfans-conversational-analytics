import { Box, GraphExplorerView } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function EmptyWorkspace() {
  return (
    <Box sx={{ height: 420, minWidth: 680 }}>
      <GraphExplorerView />
    </Box>
  );
}
