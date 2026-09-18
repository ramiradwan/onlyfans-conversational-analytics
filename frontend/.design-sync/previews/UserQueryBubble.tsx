import {
  Box,
  Stack,
  Typography,
  UserQueryBubble,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function AnalyticsQuestions() {
  return (
    <Box sx={{ maxWidth: 720 }}>
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
