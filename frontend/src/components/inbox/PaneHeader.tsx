import { Box, styled } from '@mui/material';

import { componentTokens } from '../../theme';

/** Vertical padding of the header row, in theme spacing units. */
export const paneHeaderPaddingY = 1.5;

/** Header row shared by the conversation list and message stream panes. */
export const PaneHeader = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  backgroundColor: theme.vars.palette.background.paper,
  borderBottom: '1px solid ' + theme.vars.palette.divider,
  boxSizing: 'content-box',
  display: 'flex',
  justifyContent: 'space-between',
  minHeight: componentTokens.inbox.paneTitleHeight,
  padding: theme.spacing(paneHeaderPaddingY, 2),
}));
