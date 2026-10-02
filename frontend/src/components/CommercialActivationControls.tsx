import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { Panel, SectionHeader, useRevealHold, type SectionStatus } from './ui';
import { ReservedNotice, StatusLine } from './ui/ReservedRegion';
import { getConfig } from '../config/fastapiConfig';
import {
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  capabilityLicenseApi,
  CapabilityLicenseApiError,
  type CapabilityLicenseApi,
  type CapabilityLicenseReadiness,
} from '../services/capabilityLicenseApi';

function safeMessage(error: unknown): string {
  return error instanceof CapabilityLicenseApiError ? error.message : "Activation couldn't be checked. Try again.";
}

function StepLabel({ index, children }: { index: number; children: ReactNode }) {
  return (
    <Stack spacing={0.25}>
      <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 600 }}>
        Step {index} of 2
      </Typography>
      <Typography variant="body2">{children}</Typography>
    </Stack>
  );
}

/** Full analytics section of Settings: activation status and activation code entry. */
export function CommercialActivationControls({
  api = capabilityLicenseApi,
  secureSetupUrl,
}: {
  api?: CapabilityLicenseApi;
  /** Hosted secure setup page; read from the served config when omitted. */
  secureSetupUrl?: string;
}) {
  const [setupUrl] = useState(() => secureSetupUrl ?? getConfig().SECURE_SETUP_URL);
  const [readiness, setReadiness] = useState<CapabilityLicenseReadiness | null>(null);
  const [code, setCode] = useState('');
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [setupOpened, setSetupOpened] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const titleId = useId();
  useRevealHold(checking);

  const checkReadiness = () => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setChecking(true);
    setError(null);
    void api.readiness(controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      setReadiness(next);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(safeMessage(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setChecking(false);
    });
  };

  useEffect(() => {
    checkReadiness();
    return () => operation.current?.abort();
  }, [api]);

  const submit = async () => {
    if (submitting.current || checking) return;
    const value = code.trim();
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setError('Enter the full activation code and try again.');
      return;
    }

    submitting.current = true;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setChecking(true);
    setError(null);

    let redemptionFailure: unknown = null;
    try {
      await api.redeem(value, controller.signal);
      if (controller.signal.aborted) return;
      setCode('');
    } catch (cause) {
      if (controller.signal.aborted) return;
      redemptionFailure = cause;
    }

    try {
      const next = await api.readiness(controller.signal);
      if (controller.signal.aborted) return;
      setReadiness(next);
      if (next.commercial_authority === 'active') {
        setCode('');
        setError(null);
        setDialogOpen(false);
      } else if (redemptionFailure !== null) {
        setError(safeMessage(redemptionFailure));
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(redemptionFailure === null ? safeMessage(cause) : safeMessage(redemptionFailure));
      }
    } finally {
      submitting.current = false;
      if (!controller.signal.aborted) setChecking(false);
    }
  };

  const fullReady = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'admitted';
  const activeButBlocked = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'blocked';
  const activationRequired = readiness?.commercial_authority === 'required';
  const activationUnavailable = readiness?.commercial_authority === 'unavailable';
  const status: SectionStatus | null = checking && readiness === null
    ? null
    : fullReady
      ? { label: 'On', tone: 'success' }
      : activeButBlocked
        ? { label: 'Needs attention', tone: 'warning' }
        : activationRequired
          ? { label: 'Off', tone: 'default' }
          : null;

  return (
    <Panel sx={{ gap: 1 }}>
      <SectionHeader
        sx={{ height: { xs: '5.5rem', sm: '3rem' }, position: 'relative', '& > :first-child': { width: '100%' }, '& h2': { pr: '9rem' }, '& > [aria-live]': { position: 'absolute', top: 0, right: 0, width: '8rem', height: '1.5rem', '& .MuiChip-root': { width: '100%' }, '& .MuiChip-label': { width: '100%', textAlign: 'left' } } }}
        status={status}
        summary="Adds tone, reply, and topic insights to your conversations."
        title="Full analytics"
      />

      <ReservedNotice id="activation-notice" notice={error && !dialogOpen ? { title: '', body: error, severity: 'error' }
        : checking ? { title: '', body: 'Checking activation…', severity: 'info' }
          : activeButBlocked ? { title: "New messages aren't being analyzed", body: "Your activation is fine, but analysis can't run right now. Your existing numbers are still available.", severity: 'warning' }
            : activationUnavailable || readiness === null ? { title: '', body: "Your activation couldn't be checked. Nothing has changed.", severity: 'warning' } : null} />
      <Box sx={{ height: '2.5rem' }}>
      {!checking && activationRequired && (
        <Box data-journey-state="desktop.full_analytics_activation">
          <Button aria-haspopup="dialog" onClick={() => setDialogOpen(true)} variant="outlined">
            Turn on full analytics
          </Button>
        </Box>
      )}
      {!checking && (activationUnavailable || activeButBlocked || readiness === null) && (
        <Button onClick={checkReadiness} variant="outlined">Check again</Button>
      )}
      </Box>

      <Dialog
        aria-labelledby={titleId}
        fullWidth
        maxWidth="xs"
        onClose={() => setDialogOpen(false)}
        open={dialogOpen}
      >
        <DialogTitle id={titleId}>Turn on full analytics</DialogTitle>
        <DialogContent>
          <Stack spacing={2.5}>
            <DialogContentText variant="body2">
              Use an activation code to turn on Full analytics on this computer.
            </DialogContentText>
            <Stack spacing={1}>
              <StepLabel index={1}>Open secure setup and choose Activate Full.</StepLabel>
              {setupUrl && (
                <Box>
                  <Button
                    component="a"
                    endIcon={<OpenInNewIcon />}
                    href={setupUrl}
                    onClick={() => setSetupOpened(true)}
                    rel="noopener noreferrer"
                    size="small"
                    target="_blank"
                    variant={setupOpened ? 'text' : 'outlined'}
                  >
                    Open secure setup
                  </Button>
                </Box>
              )}
              <ReservedNotice id="activation-transfer" notice={setupOpened ? { title: '', body: 'Secure setup opened. When it gives you an activation code, return here and paste it below.', severity: 'info' } : null} />
            </Stack>
            <Stack spacing={1.5}>
              <StepLabel index={2}>Paste the code here. Codes expire after a few minutes.</StepLabel>
              <TextField
                autoComplete="off"
                fullWidth
                label="Activation code"
                helperText="Paste the code from the activation page."
                disabled={checking}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void submit(); } }}
                onChange={(event) => {
                  setCode(event.target.value.trim());
                  if (error) setError(null);
                }}
                size="small"
                value={code}
              />
            </Stack>
            <StatusLine id="activation-feedback" text={checking ? 'Checking code…' : error} tone={error ? 'error' : 'secondary'} />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>Cancel</Button>
          <Button disabled={checking} onClick={() => void submit()} variant="contained">
            Activate
          </Button>
        </DialogActions>
      </Dialog>

    </Panel>
  );
}
