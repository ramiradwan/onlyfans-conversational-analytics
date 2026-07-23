import { Card, CardContent, Typography } from '@mui/material';  
import React from 'react';  
import { KpiCardSkeleton } from './KpiCardSkeleton';  
  
interface KpiCardProps {  
  title: string;  
  value: string | number;  
  detail?: string;
  isLoading?: boolean;  
  /**  
   * Controls flex behavior.  
   * - true: card grows to fill horizontal row space.  
   * - false/undefined: card fits content height.  
   */  
  grow?: boolean;  
}  
  
/**  
 * Reusable KPI card for dashboards (Spec 5.3).  
 * Uses 'background.paper' and 'text.secondary' tokens.  
 * Now includes `theme.effects.cardBorder` for visual consistency  
 * with other Paper/Card surfaces in dashboards.  
 */  
export function KpiCard({ title, value, detail, isLoading, grow }: KpiCardProps) {
  if (isLoading) {  
    return <KpiCardSkeleton grow={grow} />;  
  }  
  
  return (
    <Card  
      sx={(theme) => ({  
        flex: grow ? 1 : 'unset',  
        bgcolor: theme.vars.palette.background.paper,  
        ...theme.effects.cardBorder(theme),  
      })}  
    >
      <CardContent>  
        <Typography variant="body2" gutterBottom sx={{
          color: 'text.secondary'
        }}>  
          {title}  
        </Typography>  
        <Typography variant="h5" component="div">  
          {value}  
        </Typography>  
        {detail && (
          <Typography
            variant="caption"
            sx={{
              color: 'text.secondary',
              display: 'block',
              mt: 0.75
            }}>
            {detail}
          </Typography>
        )}
      </CardContent>
    </Card>
  );  
}
