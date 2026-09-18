import { styled, Typography } from '@mui/material';

import type { MessageView } from '../../protocol';

const ToneDot = styled('span', {
  shouldForwardProp: (property) => property !== 'tone',
})<{ tone: 'positive' | 'negative' }>(({ tone, theme }) => ({
  backgroundColor:
    tone === 'positive' ? theme.vars.palette.sentiment.positive : theme.vars.palette.sentiment.negative,
  borderRadius: '50%',
  display: 'inline-block',
  flexShrink: 0,
  height: 6,
  width: 6,
}));

const ToneLabel = styled(Typography)(({ theme }) => ({
  alignItems: 'center',
  color: 'inherit',
  display: 'inline-flex',
  gap: theme.spacing(0.75),
}));

interface MessageToneProps {
  sentiment: MessageView['sentiment'];
}

/** Text label for a classified message tone; neutral and unclassified messages show nothing. */
export function MessageTone({ sentiment }: MessageToneProps) {
  if (sentiment !== 'positive' && sentiment !== 'negative') return null;

  return (
    <ToneLabel variant="caption">
      <ToneDot tone={sentiment} aria-hidden="true" />
      {sentiment === 'positive' ? 'Positive tone' : 'Negative tone'}
    </ToneLabel>
  );
}
