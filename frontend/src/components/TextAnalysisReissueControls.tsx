import { Button, Stack, Typography } from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import { textAnalysisReissueApi, type ReissueStatusView, type TextAnalysisReissueApi } from '../services/textAnalysisReissueApi';
import { textAnalysisScopeKey, textAnalysisScopeReady } from '../store/textAnalysisExecutionStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../store/transportStore';
import { useUserRole, useUserStore } from '../store/userStore';

interface Props {
  creatorAccountId: string | null; api?: TextAnalysisReissueApi;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}
export function TextAnalysisReissueControls(props: Props) {
  const transport = props.transport ?? bridgeTransportStore;
  const context = useSyncExternalStore(transport.subscribe, transport.getState, transport.getState);
  const role = useUserRole();
  return <ReissueSession key={textAnalysisScopeKey(context, role, props.creatorAccountId)} {...props} transport={transport} />;
}
function ReissueSession({ creatorAccountId, api = textAnalysisReissueApi, transport = bridgeTransportStore }: Props) {
  const [view, setView] = useState<ReissueStatusView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [expiredChecked, setExpiredChecked] = useState(false);
  const running = useRef<number | null>(null);
  const sequence = useRef(0);
  const lifetime = useRef<AbortController | null>(null);
  const binding = useMemo(() => textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId), [creatorAccountId, transport]);
  const active = useCallback(() => lifetime.current !== null && !lifetime.current.signal.aborted
    && binding === textAnalysisScopeKey(transport.getState(), useUserStore.getState().role, creatorAccountId)
    && textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId), [binding, creatorAccountId, transport]);
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    const request = ++sequence.current;
    const current = () => active() && lifetime.current === controller && !controller.signal.aborted && sequence.current === request;
    running.current = null;
    setView(null); setError(false); setBusy(true); setExpiredChecked(false);
    if (active()) void api.status(controller.signal).then(value => { if (current()) setView(value); })
      .catch(() => { if (current()) setError(true); })
      .finally(() => { if (current()) setBusy(false); });
    else setBusy(false);
    return () => controller.abort();
  }, [api, active]);
  async function action(kind: 'status' | 'begin' | 'resume' | 'browser') {
    if (!active() || running.current !== null) return;
    const request = ++sequence.current, controller = lifetime.current!;
    const currentRequest = () => active() && lifetime.current === controller && !controller.signal.aborted && sequence.current === request;
    running.current = request; setBusy(true); setError(false);
    try {
      const signal = controller.signal, current = view?.current;
      if (kind === 'browser' && current) await api.openBrowser(current.flow_id, signal);
      else {
        const next = kind === 'begin' ? await api.begin(signal)
          : kind === 'resume' && current ? await api.resume(current.flow_id, signal) : await api.status(signal);
        if (currentRequest()) {
          setView(next);
          setExpiredChecked(kind === 'resume' && next.current?.state === 'expired');
        }
      }
    } catch { if (currentRequest()) setError(true); }
    finally {
      if (running.current === request) running.current = null;
      if (currentRequest()) setBusy(false);
    }
  }
  const ready = active(), current = ready ? view?.current : null;
  const message = !ready ? 'Connect with an authorized creator session to recover access.'
    : error ? 'Recovery could not be verified. Check the saved status before continuing.'
      : current?.state === 'adopted' ? 'Access is saved on this computer. Processing availability is checked separately.'
        : current?.state === 'expired' ? 'The original request has expired. Check its recorded outcome before starting another request.'
          : current ? 'Continue the original request in your account, then check its outcome here.'
            : view ? 'Recover an existing text analysis licence for this computer.' : 'Checking saved recovery status…';
  return <Panel component="section" aria-label="Recover text analysis access">
    <SectionHeader title="Recover text analysis access" />
    <Stack spacing={2} data-journey-state="textanalysis.licence_recovery">
      <Typography>Use your account to restore an existing licence on a replacement computer.</Typography>
      <StatusLine id="reissue-feedback" text={message} summary={error ? 'Needs attention' : busy ? 'Checking…' : 'Status'}
        tone={error ? 'error' : 'secondary'} />
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Button disabled={!ready || busy} onClick={() => void action('status')}>Check saved recovery status</Button>
        {view?.current === null && <Button disabled={!ready || busy || error} onClick={() => void action('begin')}>Recover existing licence</Button>}
        {current?.state === 'expired' && expiredChecked && <Button disabled={busy || error} onClick={() => void action('begin')}>Start a new recovery request</Button>}
        {current && current.state !== 'adopted' && <Button disabled={busy} onClick={() => void action('resume')}>Check original recovery request</Button>}
        {current?.navigation_available && <Button disabled={busy} onClick={() => void action('browser')}>Continue recovery in browser</Button>}
      </Stack>
      <Typography>Recovery does not install files or start message analysis.</Typography>
    </Stack>
  </Panel>;
}
