import AccountTreeOutlinedIcon from '@mui/icons-material/AccountTreeOutlined';
import HubOutlinedIcon from '@mui/icons-material/HubOutlined';
import SendRoundedIcon from '@mui/icons-material/SendRounded';
import {
  Alert,
  Box,
  Button,
  Paper,
  Stack,
  TextField,
  Typography,
  styled,
} from '@mui/material';
import { Fragment, type FormEvent, useState } from 'react';

import {
  formatCount,
  type AnalyticsGraphSummary,
  type AnalyticsWindowSource,
} from '../../analytics';
import { componentTokens } from '../../theme';
import { AnalyticsWindowLabel, MetricRow } from '../analytics';

export type GraphQueryGate =
  | { enabled: false; reason: string }
  | { enabled: true; onSubmit(question: string): void };

const SummaryGrid = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  [theme.breakpoints.up('lg')]: {
    gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
  },
}));

const Panel = styled(Paper)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  padding: theme.spacing(2.5),
  ...theme.effects.cardBorder(theme),
}));

const Counts = styled('dl')(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(0.75),
  gridTemplateColumns: 'minmax(0, 1fr) auto',
  margin: 0,
  '& dt, & dd': {
    borderBottom: `1px solid ${theme.vars.palette.divider}`,
    margin: 0,
    padding: theme.spacing(0.75, 0),
  },
  '& dt': { color: theme.vars.palette.text.secondary },
  '& dd': { fontVariantNumeric: 'tabular-nums', fontWeight: theme.typography.fontWeightMedium },
}));

const QueryStage = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  display: 'grid',
  gap: theme.spacing(2),
  minHeight: theme.spacing(32),
  padding: theme.spacing(2),
  ...theme.effects.cardBorder(theme),
}));

const AssistantBubble = styled(Paper)(({ theme }) => ({
  alignSelf: 'start',
  backgroundColor: theme.vars.palette.communication.incomingSurface,
  border: `1px solid ${theme.vars.palette.communication.incomingBorder}`,
  borderRadius: componentTokens.inbox.bubbleRadius,
  borderEndStartRadius: 0,
  maxWidth: componentTokens.inbox.bubbleMaxWidth,
  padding: theme.spacing(1.5, 2),
}));

const QueryForm = styled('form')(({ theme }) => ({
  alignItems: 'center',
  backgroundColor: theme.vars.palette.communication.outgoingSurface,
  border: `1px solid ${theme.vars.palette.communication.outgoingBorder}`,
  borderRadius: componentTokens.inbox.bubbleRadius,
  borderEndEndRadius: 0,
  display: 'flex',
  gap: theme.spacing(1),
  justifySelf: 'end',
  maxWidth: componentTokens.inbox.bubbleMaxWidth,
  padding: theme.spacing(1),
  width: '100%',
}));

function CountList({ values }: { values: Readonly<Record<string, number>> }) {
  const rows = Object.entries(values).sort(
    ([left], [right]) => left.localeCompare(right),
  );
  if (rows.length === 0) {
    return <Typography color="text.secondary">No projected classes.</Typography>;
  }
  return (
    <Counts>
      {rows.map(([label, count]) => (
        <Fragment key={label}>
          <Typography component="dt" variant="body2">{label.replaceAll('_', ' ')}</Typography>
          <Typography component="dd" variant="body2">{formatCount(count)}</Typography>
        </Fragment>
      ))}
    </Counts>
  );
}

export interface GraphSummaryPanelProps {
  summary: AnalyticsGraphSummary | null;
  queryGate: GraphQueryGate;
  windowSource: AnalyticsWindowSource;
}

export function GraphSummaryPanel({
  summary,
  queryGate,
  windowSource,
}: GraphSummaryPanelProps) {
  const [question, setQuestion] = useState('');
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!queryGate.enabled || !question.trim()) return;
    queryGate.onSubmit(question.trim());
    setQuestion('');
  };

  return (
    <Stack spacing={2}>
      {summary ? (
        <>
          <MetricRow
            items={[
              {
                label: 'Graph nodes',
                value: formatCount(summary.nodeCount),
                supportingText: `Canonical revision ${formatCount(summary.sourceRevision)}`,
                icon: <HubOutlinedIcon />,
                tone: 'primary',
                windowSource,
              },
              {
                label: 'Graph edges',
                value: formatCount(summary.edgeCount),
                supportingText: 'Engine-neutral projected relations',
                icon: <AccountTreeOutlinedIcon />,
                tone: 'connection',
                windowSource,
              },
            ]}
          />
          <SummaryGrid>
            <Panel role="region" aria-labelledby="node-kinds-title">
              <Typography id="node-kinds-title" component="h2" variant="h6">Node kinds</Typography>
              <AnalyticsWindowLabel source={windowSource} />
              <CountList values={summary.nodeCountsByKind} />
            </Panel>
            <Panel role="region" aria-labelledby="relation-kinds-title">
              <Typography id="relation-kinds-title" component="h2" variant="h6">Relation kinds</Typography>
              <AnalyticsWindowLabel source={windowSource} />
              <CountList values={summary.edgeCountsByRelation} />
            </Panel>
          </SummaryGrid>
        </>
      ) : (
        <Alert severity="info">
          A relationship graph summary is not available for the current canonical projection.
        </Alert>
      )}

      <Panel role="region" aria-labelledby="graph-query-title">
        <Box>
          <Typography id="graph-query-title" component="h2" variant="h6">Ask about relationships</Typography>
          <Typography variant="body2" color="text.secondary">
            Natural-language graph queries require a bounded, engine-neutral query API.
          </Typography>
        </Box>
        <QueryStage>
          <AssistantBubble>
            <Typography variant="body2">
              {queryGate.enabled
                ? 'Enter a bounded relationship question.'
                : queryGate.reason}
            </Typography>
          </AssistantBubble>
          <QueryForm onSubmit={submit}>
            <TextField
              fullWidth
              size="small"
              label="Relationship question"
              value={question}
              disabled={!queryGate.enabled}
              onChange={(event) => setQuestion(event.target.value)}
            />
            <Button
              type="submit"
              variant="contained"
              disabled={!queryGate.enabled || !question.trim()}
              startIcon={<SendRoundedIcon />}
            >
              Ask
            </Button>
          </QueryForm>
        </QueryStage>
      </Panel>
    </Stack>
  );
}
