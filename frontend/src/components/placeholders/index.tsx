// src/components/placeholders/index.tsx  
import React from 'react';  
import {  
  Box,  
  Skeleton,  
  useTheme,  
  Paper,  
  List,  
  ListItemButton,  
  ListItemAvatar,  
  ListItemText,  
  Stack,  
} from '@mui/material';  
  
/**  
 * ChartPlaceholder  
 * Generic chart skeleton frame — used in AnalyticsView & CreatorDashboardView  
 */  
export const ChartPlaceholder = ({ height = 300 }: { height?: number }) => {  
  const theme = useTheme();  
  return (  
    <Box  
      sx={{  
        height,  
        width: '100%',  
        bgcolor: theme.vars.palette.placeholder,  
        borderRadius: 1,  
        display: 'flex',  
        flexDirection: 'column',  
        justifyContent: 'space-between',  
        p: 2,  
        ...theme.effects.chartFrame(theme),  
      }}  
    >  
      <Box sx={{ height: '2px', bgcolor: theme.vars.palette.divider, mb: 1 }} />  
      <Box  
        sx={{  
          flexGrow: 1,  
          position: 'relative',  
          border: `1px dashed ${theme.vars.palette.divider}`,  
        }}  
      >  
        {[...Array(3)].map((_, i) => (  
          <Box  
            key={i}  
            sx={{  
              position: 'absolute',  
              left: `${20 + i * 20}%`,  
              bottom: 0,  
              height: `${30 + i * 15}%`,  
              width: '4px',  
              bgcolor: theme.vars.palette.divider,  
              borderRadius: 1,  
            }}  
          />  
        ))}  
      </Box>  
      <Box sx={{ height: '2px', bgcolor: theme.vars.palette.divider, mt: 1 }} />  
    </Box>  
  );  
};  
  
/**  
 * KpiPlaceholder  
 * KPI card skeleton — used in CreatorDashboardView  
 */  
export const KpiPlaceholder = () => {  
  const theme = useTheme();  
  return (  
    <Paper  
      sx={{  
        p: 2,  
        bgcolor: theme.vars.palette.background.paper,  
        display: 'flex',  
        flexDirection: 'column',  
        gap: 1,  
        height: '100%',  
        ...theme.effects.cardBorder(theme),  
      }}  
      elevation={0}  
    >  
      <Box  
        sx={{  
          height: 14,  
          width: '60%',  
          bgcolor: theme.vars.palette.placeholder,  
          borderRadius: 1,  
        }}  
      />  
      <Box  
        sx={{  
          height: 26,  
          width: '80%',  
          bgcolor: theme.vars.palette.placeholder,  
          borderRadius: 1,  
        }}  
      />  
    </Paper>  
  );  
};  
  
/**  
 * HorizontalBarsPlaceholder  
 * Used for horizontal bar chart skeletons in AnalyticsView  
 */  
export const HorizontalBarsPlaceholder = ({ bars = 5 }: { bars?: number }) => {  
  const theme = useTheme();  
  return (  
    <Box sx={{ width: '100%', display: 'flex', flexDirection: 'column', gap: 1 }}>  
      {Array.from({ length: bars }).map((_, idx) => (  
        <Box  
          key={idx}  
          sx={{  
            height: 16,  
            width: `${60 + Math.random() * 40}%`,  
            bgcolor: theme.vars.palette.placeholder,  
            borderRadius: 1,  
          }}  
        />  
      ))}  
    </Box>  
  );  
};  
  
/**  
 * TablePlaceholder  
 * Used for table skeletons in AnalyticsView  
 */  
export const TablePlaceholder = ({ rows = 4 }: { rows?: number }) => {  
  const theme = useTheme();  
  return (  
    <Box sx={{ width: '100%' }}>  
      <Box sx={{ display: 'flex', borderBottom: `1px solid ${theme.vars.palette.divider}`, p: 1 }}>  
        {['Topic', 'Volume', '% of Total', 'Trend (7d)'].map((_, idx) => (  
          <Box  
            key={idx}  
            sx={{  
              flex: idx === 0 ? 2 : 1,  
              height: 20,  
              bgcolor: theme.vars.palette.placeholder,  
              borderRadius: 1,  
              mr: 1,  
            }}  
          />  
        ))}  
      </Box>  
      {Array.from({ length: rows }).map((_, rIdx) => (  
        <Box  
          key={rIdx}  
          sx={{  
            display: 'flex',  
            borderBottom: `1px solid ${theme.vars.palette.divider}`,  
            p: 1,  
          }}  
        >  
          {Array.from({ length: 4 }).map((_, cIdx) => (  
            <Box  
              key={cIdx}  
              sx={{  
                flex: cIdx === 0 ? 2 : 1,  
                height: 16,  
                bgcolor: theme.vars.palette.placeholder,  
                borderRadius: 1,  
                mr: 1,  
              }}  
            />  
          ))}  
        </Box>  
      ))}  
    </Box>  
  );  
};  
  
/**  
 * ChatListPlaceholder  
 * Conversation list skeleton — used in OperatorInboxView  
 */  
export const ChatListPlaceholder = ({ rows = 6 }: { rows?: number }) => {  
  const theme = useTheme();  
  return (  
    <List>  
      {Array.from({ length: rows }).map((_, idx) => (  
        <ListItemButton key={idx} sx={{ py: 1.5 }}>  
          <ListItemAvatar>  
            <Skeleton  
              variant="circular"  
              width={40}  
              height={40}  
              animation={false}  
              sx={{ bgcolor: theme.vars.palette.placeholder }}  
            />  
          </ListItemAvatar>  
          <ListItemText  
            primary={  
              <Skeleton  
                width="60%"  
                height={16}  
                animation={false}  
                sx={{ bgcolor: theme.vars.palette.placeholder }}  
              />  
            }  
            secondary={  
              <Skeleton  
                width="80%"  
                height={14}  
                animation={false}  
                sx={{ bgcolor: theme.vars.palette.placeholder }}  
              />  
            }  
          />  
        </ListItemButton>  
      ))}  
    </List>  
  );  
};  
  
/**  
 * MessageStreamPlaceholder  
 * Chat bubble skeletons — used in OperatorInboxView  
 */  
export const MessageStreamPlaceholder = ({ bubbles = 4 }: { bubbles?: number }) => {  
  const theme = useTheme();  
  return (  
    <Stack spacing={2} sx={{ p: 2 }}>  
      {Array.from({ length: bubbles }).map((_, idx) => (  
        <Skeleton  
          key={idx}  
          variant="rectangular"  
          animation={false} // calm, no shimmer  
          width={idx % 2 === 0 ? '65%' : '45%'} // alternate width  
          height={idx % 3 === 0 ? 80 : 56} // occasional taller bubble  
          sx={{  
            borderRadius: 2,  
            alignSelf: idx % 2 === 0 ? 'flex-start' : 'flex-end', // alternate alignment  
            bgcolor: theme.vars.palette.placeholder,  
          }}  
        />  
      ))}  
    </Stack>  
  );  
};  
  
/**  
 * Fan360Placeholder  
 * Insight panel skeleton — used in OperatorInboxView  
 */  
export const Fan360Placeholder = () => {  
  const theme = useTheme();  
  return (  
    <Stack spacing={2}>  
      <Skeleton width="40%" height={20} animation={false} sx={{ bgcolor: theme.vars.palette.placeholder }} />  
      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">  
        {Array.from({ length: 3 }).map((_, idx) => (  
          <Skeleton  
            key={idx}  
            variant="rounded"  
            width={80}  
            height={28}  
            animation={false}  
            sx={{ bgcolor: theme.vars.palette.placeholder }}  
          />  
        ))}  
      </Stack>  
      <Skeleton width="50%" height={20} animation={false} sx={{ bgcolor: theme.vars.palette.placeholder }} />  
      <Stack spacing={1}>  
        {Array.from({ length: 2 }).map((_, idx) => (  
          <Skeleton  
            key={idx}  
            variant="rounded"  
            width={120}  
            height={28}  
            animation={false}  
            sx={{ bgcolor: theme.vars.palette.placeholder }}  
          />  
        ))}  
      </Stack>  
    </Stack>  
  );  
};  