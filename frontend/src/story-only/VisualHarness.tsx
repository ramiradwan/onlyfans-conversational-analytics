/** STORY ONLY: local visual QA entry point. Never imported by product runtime. */
import {
  Box,
  CssBaseline,
  MenuItem,
  Paper,
  Stack,
  TextField,
  Typography,
  styled,
} from '@mui/material';
import { ThemeProvider } from '@mui/material/styles';
import { createRoot } from 'react-dom/client';

import {
  STORY_ONLY_NOTICE,
  storyAnalyticsState,
  storyAnalyticsStateOptions,
  type StoryAnalyticsStateKey,
} from './analyticsFixtures';
import { StoryView, type StoryViewName } from './StoryViews';
import {
  StoryWorkspace,
  storyJourneyOptions,
  storyWorkspaceOptions,
  type StoryJourneyName,
  type StoryWorkspaceName,
} from './StoryWorkspace';
import { theme } from '../theme';
import '../index.css';

type StoryMode = 'light' | 'dark';

const Harness = styled('div')(({ theme: activeTheme }) => ({
  backgroundColor: activeTheme.vars.palette.background.default,
  color: activeTheme.vars.palette.text.primary,
  display: 'flex',
  flexDirection: 'column',
  height: '100vh',
  minHeight: 0,
  overflow: 'hidden',
}));

const StoryBanner = styled(Paper)(({ theme: activeTheme }) => ({
  alignItems: 'center',
  borderRadius: 0,
  display: 'flex',
  flex: '0 0 auto',
  flexWrap: 'wrap',
  gap: activeTheme.spacing(1.5),
  justifyContent: 'space-between',
  padding: activeTheme.spacing(1, 2),
}));

const Controls = styled(Stack)(({ theme: activeTheme }) => ({
  alignItems: 'center',
  flexDirection: 'row',
  flexWrap: 'wrap',
  gap: activeTheme.spacing(1),
}));

const Control = styled(TextField)(({ theme: activeTheme }) => ({
  minWidth: activeTheme.spacing(16),
}));

const Content = styled(Box)({
  display: 'flex',
  flex: '1 1 auto',
  minHeight: 0,
  minWidth: 0,
});

const viewOptions: readonly { key: StoryViewName; label: string }[] = [
  { key: 'analytics', label: 'Analytics' },
  { key: 'inbox', label: 'Inbox' },
  { key: 'graph', label: 'Graph' },
];

function parseView(value: string | null): StoryViewName {
  return viewOptions.some((option) => option.key === value)
    ? (value as StoryViewName)
    : 'analytics';
}

function parseState(value: string | null): StoryAnalyticsStateKey {
  if (value === 'available') return 'model';
  return storyAnalyticsStateOptions.some((option) => option.key === value)
    ? (value as StoryAnalyticsStateKey)
    : 'baseline';
}

function parseWorkspace(value: string | null): StoryWorkspaceName | null {
  return storyWorkspaceOptions.some((option) => option.key === value)
    ? (value as StoryWorkspaceName)
    : null;
}

function parseJourney(value: string | null): StoryJourneyName {
  return storyJourneyOptions.some((option) => option.key === value)
    ? (value as StoryJourneyName)
    : 'populated';
}

function replaceQuery(name: string, value: string) {
  const next = new URLSearchParams(window.location.search);
  next.set(name, value);
  window.location.search = next.toString();
}

interface StoryControlsProps {
  mode: StoryMode;
  stateKey: StoryAnalyticsStateKey;
  view: StoryViewName;
}

function StoryControls({ mode, stateKey, view }: StoryControlsProps) {
  return (
    <StoryBanner square>
      <Typography variant="caption">{STORY_ONLY_NOTICE}</Typography>
      <Controls aria-label="Visual harness controls">
        <Control
          select
          size="small"
          label="View"
          value={view}
          onChange={(event) => replaceQuery('view', event.target.value)}
        >
          {viewOptions.map((option) => (
            <MenuItem key={option.key} value={option.key}>{option.label}</MenuItem>
          ))}
        </Control>
        <Control
          select
          size="small"
          label="State"
          value={stateKey}
          onChange={(event) => replaceQuery('state', event.target.value)}
        >
          {storyAnalyticsStateOptions.map((option) => (
            <MenuItem key={option.key} value={option.key}>{option.label}</MenuItem>
          ))}
        </Control>
        <Control
          select
          size="small"
          label="Theme"
          value={mode}
          onChange={(event) => replaceQuery('mode', event.target.value)}
        >
          <MenuItem value="light">Light</MenuItem>
          <MenuItem value="dark">Dark</MenuItem>
        </Control>
      </Controls>
    </StoryBanner>
  );
}

const params = new URLSearchParams(window.location.search);
const mode: StoryMode = params.get('mode') === 'light' ? 'light' : 'dark';
const view = parseView(params.get('view'));
const stateKey = parseState(params.get('state'));
const workspace = parseWorkspace(params.get('workspace'));
const journey = parseJourney(params.get('state'));
document.documentElement.setAttribute('data-mui-color-scheme', mode);

export function VisualHarness() {
  if (workspace) {
    return (
      <ThemeProvider theme={theme} defaultMode={mode} disableTransitionOnChange>
        <CssBaseline />
        <StoryWorkspace
          analyticsState={storyAnalyticsState(stateKey)}
          journey={journey}
          workspace={workspace}
        />
      </ThemeProvider>
    );
  }
  const state = storyAnalyticsState(stateKey);
  return (
    <ThemeProvider theme={theme} defaultMode={mode} disableTransitionOnChange>
      <CssBaseline />
      <Harness>
        <StoryControls mode={mode} stateKey={stateKey} view={view} />
        <Content>
          <StoryView state={state} view={view} />
        </Content>
      </Harness>
    </ThemeProvider>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('Visual harness root is missing');
createRoot(root).render(<VisualHarness />);
