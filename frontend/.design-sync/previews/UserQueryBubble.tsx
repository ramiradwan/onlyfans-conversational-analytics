import { Box, Stack, Typography } from '@mui/material';
import { UserQueryBubble } from 'onlyfans-analytics-frontend';

export function AnalyticsQuestions() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 720, p: 2 }}>
      <Typography
        variant="subtitle2"
        sx={{
          color: 'text.secondary',
          mb: 2
        }}>
        Recent questions
      </Typography>
      <Stack spacing={1.5}>
        <UserQueryBubble text="Who are my ten most engaged fans this month?" />
        <UserQueryBubble text="Which conversation topics are trending upward?" />
      </Stack>
    </Box>
  );
}
