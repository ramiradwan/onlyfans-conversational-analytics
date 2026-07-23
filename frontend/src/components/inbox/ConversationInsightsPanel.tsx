import AccountTreeOutlinedIcon from '@mui/icons-material/AccountTreeOutlined';
import {
  Alert,
  Box,
  Chip,
  Paper,
  Skeleton,
  Stack,
  Typography,
  styled,
} from '@mui/material';
import { Fragment, useId } from 'react';

import {
  formatCount,
  formatDurationFromSeconds,
  formatRatioPercent,
  formatSentimentScore,
  sentimentLabel,
  type AnalyticsConversationInsight,
  type AnalyticsReadState,
  type AnalyticsWindowSource,
} from '../../analytics';
import { componentTokens } from '../../theme';
import { AnalyticsWindowLabel } from '../analytics/AnalyticsWindowLabel';

const Root = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(2),
  height: '100%',
  minHeight: 0,
  minWidth: componentTokens.inbox.insightsPaneMinWidth,
  overflowY: 'auto',
  padding: theme.spacing(2),
}));

const Section = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.surface.subtle,
  borderRadius: Number(theme.shape.borderRadius) * 2,
  display: 'grid',
  gap: theme.spacing(1),
  padding: theme.spacing(1.5),
}));

const DefinitionList = styled('dl')(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(0.75),
  gridTemplateColumns: 'minmax(0, 1fr) auto',
  margin: 0,
  '& dt, & dd': { margin: 0 },
  '& dt': { color: theme.vars.palette.text.secondary },
  '& dd': {
    fontVariantNumeric: 'tabular-nums',
    fontWeight: theme.typography.fontWeightMedium,
    textAlign: 'end',
  },
}));

function sortedCounts(counts: Readonly<Record<string, number>>) {
  return Object.entries(counts).sort(
    ([leftLabel, leftCount], [rightLabel, rightCount]) =>
      rightCount - leftCount || leftLabel.localeCompare(rightLabel),
  );
}

export interface ConversationInsightsPanelProps {
  fanName: string | null;
  insight: AnalyticsConversationInsight | null;
  analyticsState: AnalyticsReadState;
  windowSource?: AnalyticsWindowSource;
}

export function ConversationInsightsPanel({
  fanName,
  insight,
  analyticsState,
  windowSource,
}: ConversationInsightsPanelProps) {
  const instanceId = useId().replace(/:/g, '');
  const titleId = `conversation-insights-title-${instanceId}`;
  const descriptionId = `conversation-insights-description-${instanceId}`;
  const topics = insight ? sortedCounts(insight.topicCounts) : [];
  const engagement = insight ? sortedCounts(insight.engagementCounts) : [];
  const isBaselineFrame =
    analyticsState.status === 'baseline' ||
    ((analyticsState.status === 'error' || analyticsState.status === 'building') &&
      analyticsState.previousStatus === 'baseline');

  return (
    <Root
      role="complementary"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
    >
      <Box>
        <Typography id={titleId} component="h2" variant="h6">
          Conversation insights
        </Typography>
        <Typography id={descriptionId} variant="body2" sx={{
          color: 'text.secondary'
        }}>
          {fanName ?? 'No fan selected'}
        </Typography>
        {windowSource && <AnalyticsWindowLabel source={windowSource} />}
      </Box>

      {analyticsState.status === 'loading' && (
        <Stack spacing={1.5} role="status" aria-label="Loading conversation insights">
          <Skeleton variant="rounded" height={96} />
          <Skeleton variant="rounded" height={112} />
          <Skeleton variant="rounded" height={96} />
        </Stack>
      )}

      {analyticsState.status === 'building' && (
        <Alert severity="info">A fresh conversation insights frame is building.</Alert>
      )}

      {analyticsState.status === 'unavailable' && (
        <Alert severity="info">Canonical conversation insights are unavailable.</Alert>
      )}

      {analyticsState.status === 'error' && (
        <Alert severity="error">{analyticsState.message}</Alert>
      )}

      {isBaselineFrame && (
        <Alert severity="warning" icon={false}>
          Directional baseline · not calibrated
        </Alert>
      )}

      {analyticsState.status !== 'loading' && fanName === null && (
        <Typography sx={{
          color: 'text.secondary'
        }}>
          Choose a conversation to inspect its projected signals.
        </Typography>
      )}

      {analyticsState.status !== 'loading' && fanName !== null && insight === null && (
        <Alert severity="info">
          No conversation-level analytics are resolved for this fan yet.
        </Alert>
      )}

      {insight && (
        <Stack spacing={1.5}>
          <Section>
            <Typography component="h3" variant="subtitle2">
              Conversation
            </Typography>
            <DefinitionList>
              <Typography component="dt" variant="body2">Messages</Typography>
              <Typography component="dd" variant="body2">{formatCount(insight.messageCount)}</Typography>
              <Typography component="dt" variant="body2">Unread</Typography>
              <Typography component="dd" variant="body2">{formatCount(insight.unreadCount)}</Typography>
              <Typography component="dt" variant="body2">Reply coverage</Typography>
              <Typography component="dd" variant="body2">{formatRatioPercent(insight.responseCoverage)}</Typography>
              <Typography component="dt" variant="body2">Average response</Typography>
              <Typography component="dd" variant="body2">{formatDurationFromSeconds(insight.averageResponseSeconds)}</Typography>
            </DefinitionList>
          </Section>

          <Section>
            <Typography component="h3" variant="subtitle2">
              Sentiment
            </Typography>
            <Typography variant="body2">
              {sentimentLabel(insight.averageSentimentScore)} · {formatSentimentScore(insight.averageSentimentScore)}
            </Typography>
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              Directional range: −1 to +1
            </Typography>
          </Section>

          <Section>
            <Typography component="h3" variant="subtitle2">
              Topics
            </Typography>
            {topics.length ? (
              <Stack
                direction="row"
                useFlexGap
                sx={{
                  flexWrap: 'wrap',
                  gap: 1
                }}>
                {topics.map(([label, count]) => (
                  <Chip key={label} label={`${label} · ${formatCount(count)}`} size="small" variant="outlined" />
                ))}
              </Stack>
            ) : (
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>No projected topics.</Typography>
            )}
          </Section>

          <Section>
            <Typography component="h3" variant="subtitle2">
              Engagement states
            </Typography>
            {engagement.length ? (
              <DefinitionList>
                {engagement.map(([label, count]) => (
                  <Fragment key={label}>
                    <Typography component="dt" variant="body2">{label}</Typography>
                    <Typography component="dd" variant="body2">{formatCount(count)}</Typography>
                  </Fragment>
                ))}
              </DefinitionList>
            ) : (
              <Typography variant="body2" sx={{
                color: 'text.secondary'
              }}>No projected engagement states.</Typography>
            )}
          </Section>

          <Section>
            <Stack direction="row" spacing={1} sx={{
              alignItems: 'center'
            }}>
              <AccountTreeOutlinedIcon color="disabled" aria-hidden="true" />
              <Typography component="h3" variant="subtitle2">
                Relationship graph
              </Typography>
            </Stack>
            <Typography variant="body2" sx={{
              color: 'text.secondary'
            }}>
              Fan-level paths and neighborhoods remain unavailable until bounded graph query APIs are integrated.
            </Typography>
          </Section>
        </Stack>
      )}
    </Root>
  );
}
