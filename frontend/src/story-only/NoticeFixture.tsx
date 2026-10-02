import { Box } from '@mui/material';
import { useEffect, useState } from 'react';
import { ReservedNotice, StatusLine } from '../components/ui/ReservedRegion';

export function NoticeFixture() {
  const [text, setText] = useState('Saved.');
  useEffect(() => {
    const update = (event: Event) => setText((event as CustomEvent<string>).detail);
    window.addEventListener('notice-fixture', update);
    return () => window.removeEventListener('notice-fixture', update);
  }, []);
  return <Box sx={{ p: 2 }}>
    <ReservedNotice id="notice" notice={{ title: text, body: text, severity: 'info' }} />
    <StatusLine id="line" text={text} />
    <button type="button">Following action</button>
  </Box>;
}
