import CodeIcon from '@mui/icons-material/Code';
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Paper,
  Skeleton,
  Stack,
  Typography,
} from '@mui/material';
import { styled } from '@mui/material/styles';
import { useId, useState } from 'react';

// Local graph-query result type.
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

const CodeBlock = styled('pre')(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  border: `1px solid ${theme.vars.palette.divider}`,
  borderRadius: theme.shape.borderRadius,
  fontSize: theme.typography.pxToRem(14),
  margin: 0,
  overflowX: 'auto',
  padding: theme.spacing(1),
}));

export function QueryResponseBubble({ response }: { response: QueryResponse }) {
  const { answer, gremlinQuery, result } = response;
  const [queryOpen, setQueryOpen] = useState(false);
  const titleId = useId();
  return (
    <Box sx={{ display: 'flex', justifyContent: 'flex-start' }}>
      <Bubble>
        <Stack spacing={2}>
          <Typography variant="body1">{answer}</Typography>
          {result && <CodeBlock>{JSON.stringify(result, null, 2)}</CodeBlock>}
          {gremlinQuery && (
            <Box>
              <Button
                aria-haspopup="dialog"
                onClick={() => setQueryOpen(true)}
                size="small"
                startIcon={<CodeIcon />}
                sx={{ color: 'text.secondary' }}
              >
                View query
              </Button>
            </Box>
          )}
        </Stack>
      </Bubble>
      {gremlinQuery && (
        <Dialog
          aria-labelledby={titleId}
          fullWidth
          maxWidth="sm"
          onClose={() => setQueryOpen(false)}
          open={queryOpen}
        >
          <DialogTitle id={titleId}>Generated query</DialogTitle>
          <DialogContent>
            <CodeBlock>
              <code>{gremlinQuery}</code>
            </CodeBlock>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setQueryOpen(false)}>Close</Button>
          </DialogActions>
        </Dialog>
      )}
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
