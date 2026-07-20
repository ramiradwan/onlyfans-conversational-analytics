import { Box, Paper, Stack, Typography, styled } from '@mui/material';
import { type ReactNode, useId } from 'react';

import { AnalyticsWindowLabel } from './AnalyticsWindowLabel';
import type { AnalyticsWindowSource } from '../../analytics';

const Root = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(2),
  height: '100%',
  minWidth: 0,
  padding: theme.spacing(2.5),
  ...theme.effects.cardBorder(theme),
}));

const Header = styled(Stack)(({ theme }) => ({
  alignItems: 'flex-start',
  flexDirection: 'row',
  gap: theme.spacing(2),
  justifyContent: 'space-between',
}));

export interface ChartPanelProps {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  labelledBy?: string;
  windowSource: AnalyticsWindowSource;
}

export function ChartPanel({
  title,
  description,
  action,
  children,
  labelledBy,
  windowSource,
}: ChartPanelProps) {
  const generatedTitleId = useId().replace(/:/g, '');
  const titleId = labelledBy ?? `analytics-panel-${generatedTitleId}-title`;
  return (
    <Root role="region" aria-labelledby={titleId}>
      <Header>
        <Box minWidth={0}>
          <Typography id={titleId} component="h2" variant="h6">
            {title}
          </Typography>
          {description && (
            <Typography component="p" variant="body2" color="text.secondary">
              {description}
            </Typography>
          )}
          <AnalyticsWindowLabel source={windowSource} />
        </Box>
        {action}
      </Header>
      {children}
    </Root>
  );
}
