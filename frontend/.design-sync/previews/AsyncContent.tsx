import { Alert, Box, Chip, Skeleton, Stack, Typography } from '@mui/material';
import { AsyncContent } from 'onlyfans-analytics-frontend';

const topics = ['Custom content', 'Behind the scenes', 'Fitness routines'];

export function Loaded() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 520 }}>
      <Typography variant="subtitle1" sx={{ mb: 1.5 }}>
        Top conversation topics
      </Typography>
      <AsyncContent
        isLoading={false}
        data={topics}
        placeholder={<Skeleton variant="rounded" height={72} />}
        emptyMessage="No topics available."
        render={(items) => (
          <Stack direction="row" spacing={1} useFlexGap sx={{
            flexWrap: 'wrap'
          }}>
            {items.map((topic) => (
              <Chip key={topic} label={topic} color="primary" variant="outlined" />
            ))}
          </Stack>
        )}
      />
    </Box>
  );
}

export function Loading() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 520 }}>
      <AsyncContent
        isLoading
        data={null}
        placeholder={
          <Stack spacing={1}>
            <Skeleton width="42%" />
            <Skeleton variant="rounded" height={64} />
          </Stack>
        }
        emptyMessage="No topics available."
        render={() => null}
      />
    </Box>
  );
}

export function Empty() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 520 }}>
      <AsyncContent
        isLoading={false}
        data={[]}
        placeholder={<Skeleton variant="rounded" height={72} />}
        emptyMessage={<Alert severity="info">Insights will appear after the first conversation sync.</Alert>}
        render={() => null}
      />
    </Box>
  );
}
