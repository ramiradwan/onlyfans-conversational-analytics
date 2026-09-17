import HubOutlinedIcon from '@mui/icons-material/HubOutlined';
import { Box, Stack, Typography } from '@mui/material';

import { layoutTokens } from '@/theme/generated/tokens';

export const BRAND_MARK_SIZE = 32;

export function BrandMark() {
  return (
    <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', minWidth: 0 }}>
      <Box
        aria-hidden="true"
        sx={(theme) => ({
          alignItems: 'center',
          background: `linear-gradient(140deg, ${theme.vars.palette.primary.light}, ${theme.vars.palette.primary.main})`,
          borderRadius: `${layoutTokens.radius.compact}px`,
          color: theme.vars.palette.primary.contrastText,
          display: 'flex',
          flex: '0 0 auto',
          height: BRAND_MARK_SIZE,
          justifyContent: 'center',
          width: BRAND_MARK_SIZE,
        })}
      >
        <HubOutlinedIcon sx={{ fontSize: 18 }} />
      </Box>
      <Typography
        variant="subtitle1"
        noWrap
        sx={{ display: { xs: 'none', sm: 'block' }, fontWeight: 600, lineHeight: 1.2, minWidth: 0 }}
      >
        Conversation Analytics
      </Typography>
    </Stack>
  );
}
