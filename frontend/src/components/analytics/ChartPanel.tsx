import { Box, Stack, Typography, styled } from '@mui/material';
import { type ReactNode, useId } from 'react';

import { AnalyticsWindowLabel } from './AnalyticsWindowLabel';
import type { AnalyticsWindowSource } from '../../analytics';
import { Panel, type PanelEmphasis } from '../ui/Panel';

const Root = styled(Panel)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(2),
  height: '100%',
  minWidth: 0,
  padding: theme.spacing(2.5),
}));

const Header = styled(Stack)(({ theme }) => ({
  alignItems: 'flex-start',
  flexDirection: 'row',
  gap: theme.spacing(2),
  justifyContent: 'space-between',
}));

export interface ChartPanelProps {
  title: string;
  emphasis?: PanelEmphasis;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  labelledBy?: string;
  windowSource?: AnalyticsWindowSource;
}

export function ChartPanel({
  title,
  emphasis = 'secondary',
  description,
  action,
  children,
  labelledBy,
  windowSource,
}: ChartPanelProps) {
  const generatedTitleId = useId().replace(/:/g, '');
  const titleId = labelledBy ?? `analytics-panel-${generatedTitleId}-title`;
  return (
    <Root arrivalStep={emphasis === 'dominant' ? 0 : 1} emphasis={emphasis} role="region" aria-labelledby={titleId}>
      <Header>
        <Box sx={{
          minWidth: 0
        }}>
          <Typography id={titleId} component="h2" variant="h6">
            {title}
          </Typography>
          {description && (
            <Typography component="p" variant="body2" sx={{
              color: 'text.secondary'
            }}>
              {description}
            </Typography>
          )}
          {windowSource && <AnalyticsWindowLabel source={windowSource} />}
        </Box>
        {action}
      </Header>
      {children}
    </Root>
  );
}
