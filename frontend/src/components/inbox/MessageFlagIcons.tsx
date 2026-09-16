import MoodIcon from '@mui/icons-material/Mood';
import MoodBadIcon from '@mui/icons-material/MoodBad';
import { styled, Tooltip } from '@mui/material';

import type { MessageView } from '../../protocol';

const Flag = styled('span', {
  shouldForwardProp: (property) => property !== 'sentiment',
})<{ sentiment: MessageView['sentiment'] }>(({ sentiment, theme }) => ({
  alignItems: 'center',
  color:
    sentiment === 'positive'
      ? theme.vars.palette.success.main
      : theme.vars.palette.error.main,
  display: 'inline-flex',
  flexShrink: 0,
  fontSize: theme.typography.body2.fontSize,
}));

interface MessageFlagIconProps {
  sentiment: MessageView['sentiment'];
  context?: 'latest' | 'message';
}

export function MessageFlagIcon({
  sentiment,
  context = 'message',
}: MessageFlagIconProps) {
  if (sentiment === 'unknown' || sentiment === 'neutral') return null;

  const label =
    (context === 'latest' ? 'Latest message sentiment: ' : 'Message sentiment: ') + sentiment;
  const icon =
    sentiment === 'positive' ? <MoodIcon fontSize="inherit" /> : <MoodBadIcon fontSize="inherit" />;

  return (
    <Tooltip title={label}>
      <Flag sentiment={sentiment} role="img" aria-label={label}>
        {icon}
      </Flag>
    </Tooltip>
  );
}
