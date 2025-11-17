import React from 'react';  
import { Card, CardContent, Skeleton } from '@mui/material';  
import { skeletonTokens } from '@/theme';    
interface KpiCardSkeletonProps {  
  grow?: boolean;  
}  
  
/**  
 * Skeleton state for KPI card.  
 * Matches exact card style via `theme.effects.cardBorder`  
 * and uses Tier 2 placeholder token for all skeleton colors.  
 */  
export function KpiCardSkeleton({ grow }: KpiCardSkeletonProps) {  
  return (  
    <Card  
      sx={(theme) => ({  
        flex: grow ? 1 : 'unset',  
        bgcolor: theme.vars.palette.background.paper,  
        ...theme.effects.cardBorder(theme),  
      })}  
    >  
      <CardContent>  
        <Skeleton  
          variant={skeletonTokens.kpiTitle.variant}  
          sx={(theme) => ({  
            ...skeletonTokens.kpiTitle.sx,  
            bgcolor: theme.vars.palette.placeholder, // enforce token usage  
          })}  
          animation={skeletonTokens.kpiTitle.animation}  
        />  
        <Skeleton  
          variant={skeletonTokens.kpiValue.variant}  
          sx={(theme) => ({  
            ...skeletonTokens.kpiValue.sx,  
            bgcolor: theme.vars.palette.placeholder, // enforce token usage  
          })}  
          animation={skeletonTokens.kpiValue.animation}  
        />  
      </CardContent>  
    </Card>  
  );  
}  