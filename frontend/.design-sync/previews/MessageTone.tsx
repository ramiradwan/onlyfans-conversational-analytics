import {
  Box,
  MessageTone,
  Stack,
  Typography,
} from 'onlyfans-analytics-frontend';

export function ToneStates() {
  const states = [
    ['Positive', 'positive'],
    ['Neutral (no label)', 'neutral'],
    ['Negative', 'negative'],
    ['Unknown (no label)', 'unknown'],
  ] as const;

  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 360, p: 2 }}>
      <Typography variant="subtitle2" sx={{ mb: 1.5 }}>
        Message tone
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
            <Box sx={{ color: 'text.muted', minHeight: 20 }}>
              <MessageTone sentiment={sentiment} />
            </Box>
          </Stack>
        ))}
      </Stack>
    </Box>
  );
}
