import {
  Box,
  MessageTone,
  Panel,
  Stack,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function ToneStates() {
  const states = [
    ['Positive', 'positive'],
    ['Neutral (no label)', 'neutral'],
    ['Negative', 'negative'],
    ['Unknown (no label)', 'unknown'],
  ] as const;

  return (
    <Panel sx={{ maxWidth: 360, p: 2, gap: 1.5 }}>
      <Typography variant="subtitle2">Message tone</Typography>
      <Stack spacing={1.25}>
        {states.map(([label, sentiment]) => (
          <Stack
            key={sentiment}
            direction="row"
            sx={{
              alignItems: 'center',
              justifyContent: 'space-between'
            }}>
            <Typography variant="body2" sx={{
              color: 'text.secondary'
            }}>
              {label}
            </Typography>
            <Box sx={{ color: 'text.muted', minHeight: 20 }}>
              <MessageTone sentiment={sentiment} />
            </Box>
          </Stack>
        ))}
      </Stack>
    </Panel>
  );
}
