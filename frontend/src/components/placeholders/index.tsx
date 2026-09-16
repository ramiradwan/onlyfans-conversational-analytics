// src/components/placeholders/index.tsx  
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
import React from 'react';  
  
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
  
