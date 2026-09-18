import {
  Box,
  WebAuthnAccessView,
  previewNoop,
} from 'onlyfans-analytics-frontend';

import type { WebAuthnApi } from '../../src/services/webauthnApi';

import './card.module.css';

const noopWebAuthnApi: WebAuthnApi = {
  enroll: async () => {},
  login: async () => {},
};

export function PasskeyAccess() {
  return (
    <Box sx={{ minHeight: 480, width: '100%' }}>
      <WebAuthnAccessView api={noopWebAuthnApi} onAuthenticated={previewNoop} />
    </Box>
  );
}
