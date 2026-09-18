import { Box, Stack, Typography } from '@mui/material';

import { layoutTokens } from '@/theme/generated/tokens';

export const BRAND_MARK_SIZE = 32;

function ConversationAnalyticsGlyph() {
  return (
    <svg
      aria-hidden="true"
      data-brand-mark="conversation-analytics"
      fill="none"
      height="20"
      viewBox="0 0 20 20"
      width="20"
    >
      <path
        d="M5 3.75h10A2.25 2.25 0 0 1 17.25 6v6A2.25 2.25 0 0 1 15 14.25h-4.4L7 16.75v-2.5H5A2.25 2.25 0 0 1 2.75 12V6A2.25 2.25 0 0 1 5 3.75Z"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.65"
      />
      <path
        d="M6.25 10.75 8.7 8.45l2 1.45 2.8-3"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.65"
      />
      <circle cx="6.25" cy="10.75" fill="currentColor" r=".8" />
      <circle cx="8.7" cy="8.45" fill="currentColor" r=".8" />
      <circle cx="10.7" cy="9.9" fill="currentColor" r=".8" />
      <circle cx="13.5" cy="6.9" fill="currentColor" r=".8" />
    </svg>
  );
}

export function BrandMark() {
  return (
    <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', minWidth: 0 }}>
      <Box
        aria-hidden="true"
        data-visual="brand-tile"
        sx={(theme) => ({
          alignItems: 'center',
          background: `linear-gradient(140deg, ${theme.vars.palette.brand.light}, ${theme.vars.palette.brand.main})`,
          borderRadius: `${layoutTokens.radius.compact}px`,
          color: theme.vars.palette.brand.contrastText,
          display: 'flex',
          flex: '0 0 auto',
          height: BRAND_MARK_SIZE,
          justifyContent: 'center',
          width: BRAND_MARK_SIZE,
        })}
      >
        <ConversationAnalyticsGlyph />
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
