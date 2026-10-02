import { Box } from '@mui/material';
import { useEffect, useState } from 'react';

import { FreshnessStatus } from '../components/ui/FreshnessStatus';
import type { CatchupFreshness } from '../protocol';
import type { BridgeConnectionState } from '../store/transportStore';

interface Frame { freshness: CatchupFreshness | null; bridge: BridgeConnectionState; snapshotUsable: boolean }
export function FreshnessFixture() {
  const [frame, setFrame] = useState<Frame>({ freshness: null, bridge: 'connected', snapshotUsable: true });
  useEffect(() => {
    const update = (event: Event) => setFrame((event as CustomEvent<Frame>).detail);
    window.addEventListener('freshness-fixture', update);
    return () => window.removeEventListener('freshness-fixture', update);
  }, []);
  return <Box sx={{ p: 2 }}>
    <Box data-reserved-region="toolbar" sx={{ height: 72, display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
      <Box sx={{ display: { xs: 'none', sm: 'block' } }}><FreshnessStatus {...frame} /></Box>
    </Box>
    <Box data-reserved-region="status-row" sx={{ display: { xs: 'flex', sm: 'none' }, alignItems: 'center', height: 40 }}><FreshnessStatus {...frame} /></Box>
    <Box data-reserved-region="following-content" sx={{ height: 128, bgcolor: 'background.paper' }}><span data-region-content>Messages</span></Box>
  </Box>;
}
