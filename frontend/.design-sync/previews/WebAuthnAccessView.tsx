import { Box } from '@mui/material';
import { WebAuthnAccessView, previewNoop } from 'onlyfans-analytics-frontend';

import type { WebAuthnApi } from '../../src/services/webauthnApi';

const noopWebAuthnApi: WebAuthnApi = {
  enroll: async () => {},
  login: async () => {},
};

export function PasskeyAccess() {
  return (
    <Box sx={{ bgcolor: 'background.default', minHeight: 480, p: 2, width: '100%' }}>
      <WebAuthnAccessView api={noopWebAuthnApi} onAuthenticated={previewNoop} />
    </Box>
  );
}
