import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Collapse,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { Panel, SectionHeader, type SectionStatus } from './ui';
import { getConfig } from '../config/fastapiConfig';
import {
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  capabilityLicenseApi,
  type CapabilityLicenseApi,
  type CapabilityLicenseReadiness,
} from '../services/capabilityLicenseApi';

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Activation couldn't be checked. Try again.";
}

function StepLabel({ index, children }: { index: number; children: ReactNode }) {
  return (
    <Stack direction="row" spacing={1.5} sx={{ alignItems: 'baseline' }}>
      <Typography
        aria-hidden="true"
        variant="caption"
        sx={{ color: 'text.secondary', fontWeight: 600, minWidth: 12 }}
      >
        {index}
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
  const [expanded, setExpanded] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const stepsId = useId();

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
    const value = code.trim();
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setError('Enter the full activation code and try again.');
      return;
    }

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
      } else if (redemptionFailure !== null) {
        setError(safeMessage(redemptionFailure));
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(redemptionFailure === null ? safeMessage(cause) : safeMessage(redemptionFailure));
      }
    } finally {
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
    <Panel>
      <SectionHeader
        status={status}
        summary="Adds tone, reply, and topic insights to your conversations."
        title="Full analytics"
      />

      {checking && (
        <Typography role="status" variant="body2" sx={{ color: 'text.secondary' }}>
          Checking activation…
        </Typography>
      )}

      {!checking && activeButBlocked && (
        <Alert severity="warning" role="status">
          <AlertTitle>New messages aren&apos;t being analyzed</AlertTitle>
          Your activation is fine, but analysis can&apos;t run right now. Your existing numbers are
          still available.
        </Alert>
      )}

      {!checking && (activationUnavailable || readiness === null) && (
        <Alert severity="warning" role="status">
          Your activation couldn&apos;t be checked. Nothing has changed.
        </Alert>
      )}

      {error && <Alert severity="error" role="alert">{error}</Alert>}

      {!checking && activationRequired && (
        <>
          {!expanded && (
            <Box>
              <Button
                aria-controls={stepsId}
                aria-expanded={false}
                onClick={() => setExpanded(true)}
                variant="outlined"
              >
                Turn on full analytics
              </Button>
            </Box>
          )}
          <Collapse id={stepsId} in={expanded} unmountOnExit>
            <Stack spacing={2}>
              <Stack spacing={1}>
                <StepLabel index={1}>Open secure setup and choose Activate Full.</StepLabel>
                {setupUrl && (
                  <Box sx={{ pl: 3.5 }}>
                    <Button
                      component="a"
                      endIcon={<OpenInNewIcon />}
                      href={setupUrl}
                      rel="noopener noreferrer"
                      size="small"
                      target="_blank"
                      variant="outlined"
                    >
                      Open secure setup
                    </Button>
                  </Box>
                )}
              </Stack>
              <Stack spacing={1}>
                <StepLabel index={2}>Paste the code here. Codes expire after a few minutes.</StepLabel>
                <Stack
                  direction={{ xs: 'column', sm: 'row' }}
                  spacing={1.5}
                  sx={{ alignItems: { sm: 'flex-start' }, pl: 3.5 }}
                >
                  <TextField
                    autoComplete="off"
                    label="Activation code"
                    onChange={(event) => {
                      setCode(event.target.value);
                      if (error) setError(null);
                    }}
                    size="small"
                    sx={{ flex: 1, maxWidth: { sm: 420 } }}
                    value={code}
                  />
                  <Button onClick={() => void submit()} variant="contained">
                    Activate
                  </Button>
                </Stack>
              </Stack>
            </Stack>
          </Collapse>
        </>
      )}

      {!checking && (activationUnavailable || activeButBlocked || readiness === null) && (
        <Box>
          <Button onClick={checkReadiness} variant="outlined">Check again</Button>
        </Box>
      )}
    </Panel>
  );
}
