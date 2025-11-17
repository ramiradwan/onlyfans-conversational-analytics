import CodeIcon from '@mui/icons-material/Code';  
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';  
import {  
  Paper,  
  Typography,  
  Box,  
  Stack,  
  Accordion,  
  AccordionSummary,  
  AccordionDetails,  
  Skeleton,  
} from '@mui/material';  
import { styled } from '@mui/material/styles';  
import React from 'react';  
  
// Temporary local type until backend types are generated  
interface QueryResponse {  
  id: string;  
  question: string;  
  answer?: string;  
  gremlinQuery?: string;  
  result?: Record<string, unknown> | unknown[];  
}  
  
const Bubble = styled(Paper)(({ theme }) => ({  
  padding: theme.spacing(1.5, 2),  
  borderRadius: theme.shape.borderRadius,  
  maxWidth: '80%',  
  wordBreak: 'break-word',  
  boxShadow: 'none',  
  backgroundColor: theme.vars.palette.background.paper,  
  color: theme.vars.palette.text.primary,  
  borderBottomLeftRadius: 0,  
}));  
  
const renderResult = (result: unknown) => {  
  if (!result) return null;  
  return (  
    <Box  
      component="pre"  
      sx={{  
        bgcolor: 'background.default',  
        border: (theme) => `1px solid ${theme.vars.palette.divider}`,  
        borderRadius: 1,  
        p: 1,  
        overflowX: 'auto',  
        fontSize: '0.875rem',  
      }}  
    >  
      {JSON.stringify(result, null, 2)}  
    </Box>  
  );  
};  
  
export function QueryResponseBubble({ response }: { response: QueryResponse }) {  
  const { answer, gremlinQuery, result } = response;  
  return (  
    <Box sx={{ display: 'flex', justifyContent: 'flex-start' }}>  
      <Bubble>  
        <Stack spacing={2}>  
          <Typography variant="body1">{answer}</Typography>  
          {result && renderResult(result)}  
          {gremlinQuery && (  
            <Accordion  
              elevation={0}  
              sx={{  
                bgcolor: 'background.default',  
                border: (theme) => `1px solid ${theme.vars.palette.divider}`,  
              }}  
            >  
              <AccordionSummary  
                expandIcon={<ExpandMoreIcon />}  
                sx={{  
                  minHeight: 'auto',  
                  '& .MuiAccordionSummary-content': { my: 1 },  
                }}  
              >  
                <CodeIcon  
                  fontSize="small"  
                  sx={{ mr: 1, color: 'text.secondary' }}  
                />  
                <Typography variant="body2" color="text.secondary">  
                  Generated Query  
                </Typography>  
              </AccordionSummary>  
              <AccordionDetails sx={{ p: 1, bgcolor: 'background.paper' }}>  
                <Box  
                  component="pre"  
                  sx={{  
                    overflowX: 'auto',  
                    m: 0,  
                    fontSize: '0.875rem',  
                  }}  
                >  
                  <code>{gremlinQuery}</code>  
                </Box>  
              </AccordionDetails>  
            </Accordion>  
          )}  
        </Stack>  
      </Bubble>  
    </Box>  
  );  
}  
  
export function QueryResponseBubbleSkeleton() {  
  return (  
    <Box sx={{ display: 'flex', justifyContent: 'flex-start' }}>  
      <Bubble>  
        <Stack spacing={1}>  
          <Skeleton variant="text" width={80} animation={false} />  
          <Skeleton variant="text" width={250} animation={false} />  
          <Skeleton variant="text" width={220} animation={false} />  
        </Stack>  
      </Bubble>  
    </Box>  
  );  
}  