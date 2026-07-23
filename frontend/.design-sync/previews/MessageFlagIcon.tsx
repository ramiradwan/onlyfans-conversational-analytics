import { Box, Stack, Typography } from '@mui/material';
import { MessageFlagIcon } from 'onlyfans-analytics-frontend';

export function SentimentStates() {
  const states = [
    ['Positive', 'positive'],
    ['Neutral', 'neutral'],
    ['Negative', 'negative'],
    ['Unknown', 'unknown'],
  ] as const;

  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 360, p: 2 }}>
      <Typography variant="subtitle2" sx={{ mb: 1.5 }}>
        Message sentiment
      </Typography>
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
            <Box sx={{ minWidth: 24, textAlign: 'center' }}>
              <MessageFlagIcon sentiment={sentiment} />
            </Box>
          </Stack>
        ))}
      </Stack>
    </Box>
  );
}

export function LatestMessageContext() {
  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{
        alignItems: 'center',
        p: 2
      }}>
      <Typography variant="body2">Latest message</Typography>
      <MessageFlagIcon sentiment="positive" context="latest" />
    </Stack>
  );
}
