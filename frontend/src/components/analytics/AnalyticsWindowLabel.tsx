import { Typography } from '@mui/material';

import {
  analyticsWindowLabel,
  type AnalyticsWindowSource,
} from '../../analytics';

export interface AnalyticsWindowLabelProps {
  source: AnalyticsWindowSource;
}

export function AnalyticsWindowLabel({ source }: AnalyticsWindowLabelProps) {
  const label = analyticsWindowLabel(source);
  return (
    <Typography
      component="p"
      variant="caption"
      color="text.secondary"
    >
      Data window: {label}
    </Typography>
  );
}
