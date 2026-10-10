import CheckRoundedIcon from '@mui/icons-material/CheckRounded';
import { Box, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

import { Panel } from './ui';
import { BrandMark } from '../layouts/BrandMark';
import { componentTokens } from '../theme';

export interface SetupStep { label: string; complete?: boolean; current?: boolean }

/** Common shell; the caller supplies only confirmed steps and account labels. */
export function OnboardingWorkspace({ children, steps = [], creator }: {
  children: ReactNode; steps?: SetupStep[]; creator?: string;
}) {
  const roles = componentTokens.staticUi;
  return (
    <Stack sx={{ minHeight: '100dvh', bgcolor: 'background.default', color: 'text.primary', overflowY: 'auto' }}>
      <Stack component="header" direction="row" spacing={1.5} sx={{ p: { xs: 2, sm: 3.5 }, alignItems: 'center', flexWrap: 'wrap', rowGap: 1,
        borderBottom: '1px solid', borderColor: 'divider',
        '& > :first-child .MuiTypography-root': { display: 'block', whiteSpace: 'normal' } }}>
        <BrandMark />
        {creator && <Typography sx={{ ml: 'auto !important', color: 'text.secondary', overflowWrap: 'anywhere' }}>{creator}</Typography>}
      </Stack>
      <Box sx={{ width: '100%', maxWidth: steps.length ? roles.fullWorkspaceWidth : roles.workspaceWidth,
        mx: 'auto', p: { xs: 2, sm: 3.5 }, display: 'grid', alignItems: 'start', gap: 3,
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: steps.length ? `${roles.setupRailWidth} minmax(0, 1fr)` : 'minmax(0, 1fr)' } }}>
        {steps.length > 0 && <Box component="nav" aria-label="Setup progress">
          <Box component="ol" sx={{ m: 0, p: 0, listStyle: 'none', display: { xs: 'flex', md: 'block' }, flexWrap: 'wrap' }}>
            {steps.map((step, index) => <Stack key={step.label} component="li" direction="row" spacing={1.25}
              aria-current={step.current ? 'step' : undefined}
              sx={{ p: 1.25, flex: { xs: '1 1 45%', md: 'initial' }, alignItems: 'flex-start', borderRadius: '12px',
                bgcolor: step.current ? 'background.paper' : 'transparent', color: step.current ? 'text.primary' : 'text.secondary' }}>
              <Box component="span" aria-label={step.complete ? 'Complete' : undefined} sx={{ width: 24, minHeight: 24, flexShrink: 0,
                border: '1px solid', borderColor: step.current ? 'primary.main' : 'divider', borderRadius: '50%', display: 'grid', placeItems: 'center',
                bgcolor: step.current ? 'primary.main' : 'transparent', color: step.current ? 'primary.contrastText' : 'primary.main', fontSize: '0.75rem' }}>
                {step.complete ? <CheckRoundedIcon sx={{ fontSize: 16 }} /> : index + 1}
              </Box>
              <Typography variant="body2" sx={{ fontWeight: step.current ? 600 : 400 }}>{step.label}</Typography>
            </Stack>)}
          </Box>
        </Box>}
        <Panel component="main" emphasis="dominant" sx={{ p: { xs: 2.5, sm: 3.5 }, gap: 2 }}>{children}</Panel>
      </Box>
      <Box component="footer" sx={{ mt: 'auto', px: { xs: 2, sm: 3.5 }, py: 1.5, borderTop: '1px solid', borderColor: 'divider', bgcolor: 'background.paper', color: 'text.secondary' }}>
        <Typography variant="caption" sx={{ display: 'block' }}>Use only with a creator account you are authorized to analyze.</Typography>
        <Typography variant="caption" sx={{ display: 'block' }}>Independent software. It is not affiliated with or endorsed by OnlyFans.</Typography>
      </Box>
    </Stack>
  );
}
