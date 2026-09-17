import { Box, TopicsTable, Typography } from 'onlyfans-analytics-frontend';

import type { AnalyticsTopicMetric } from '../../src/analytics';

const sampleTopics: AnalyticsTopicMetric[] = [
  { id: 'planning', label: 'Planning', volume: 18, sharePercent: 37.5, trendPercent: 12.5 },
  { id: 'check-ins', label: 'Check-ins', volume: 12, sharePercent: 25.0, trendPercent: -4.0 },
  { id: 'boundaries', label: 'Boundaries', volume: 8, sharePercent: 16.7, trendPercent: 0.0 },
  { id: 'support', label: 'Support', volume: 5, sharePercent: 10.4, trendPercent: null },
  { id: 'scheduling', label: 'Scheduling', volume: 3, sharePercent: 6.3, trendPercent: 2.0 },
  { id: 'follow-up', label: 'Follow-up', volume: 2, sharePercent: 4.1, trendPercent: -1.0 },
];

export function TopicMetrics() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 840, p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Topic metrics
      </Typography>
      <TopicsTable topics={sampleTopics} />
    </Box>
  );
}

export function EmptyState() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 840, p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Topic metrics
      </Typography>
      <TopicsTable topics={[]} />
    </Box>
  );
}
