import { Alert, Button, Stack, Typography } from '@mui/material';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  textAnalysisAcquisitionApi, type AcquisitionFlow, type AcquisitionKind,
  type AcquisitionStatus, type TextAnalysisAcquisitionApi,
} from '../../services/textAnalysisAcquisitionApi';
import { textAnalysisScopeKey, textAnalysisScopeReady } from '../../store/textAnalysisExecutionStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../../store/transportStore';
import { useUserStore } from '../../store/userStore';
import { Panel, SectionHeader } from '../ui';

type Props = {
  kind: AcquisitionKind; account: string; scope: string; canAcquire: boolean; acquired: boolean;
  onAdopt: () => void; api?: TextAnalysisAcquisitionApi; transport?: Pick<BridgeTransportStore, 'getState'>;
};
type Action = 'status' | 'begin' | 'browser' | 'read' | 'submit';

/** Mounted only in the selected package and keyed to its current ordinary desktop session. */
export function RecurringMentionsAcquisition({ kind, account, scope, canAcquire, acquired, onAdopt,
  api = textAnalysisAcquisitionApi, transport = bridgeTransportStore }: Props) {
  const paid = kind === 'paid';
  const [status, setStatus] = useState<AcquisitionStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const life = useRef<AbortController | null>(null);
  const running = useRef(false);
  const sequence = useRef(0);
  const active = useCallback(() => {
    const role = useUserStore.getState().role, state = transport.getState();
    return Boolean(life.current && !life.current.signal.aborted && textAnalysisScopeReady(state, role, account)
      && textAnalysisScopeKey(state, role, account) === scope);
  }, [account, scope, transport]);
  useEffect(() => {
    const controller = new AbortController(); life.current = controller;
    let pending = false;
    const observe = async () => {
      if (pending || running.current || !active()) return;
      pending = true;
      const request = ++sequence.current;
      try {
        const saved = await api.status(kind, controller.signal);
        if (!controller.signal.aborted && active() && sequence.current === request) { setStatus(saved); setFailed(false); }
      } catch { if (!controller.signal.aborted && active() && sequence.current === request) setFailed(true); }
      finally { pending = false; }
    };
    void observe();
    // A browser return only rereads the local journal. Settlement and adoption require the original operation.
    window.addEventListener('focus', observe);
    return () => { controller.abort(); window.removeEventListener('focus', observe); };
  }, [active, api, kind]);
  const flow = status?.current;
  const terminal = flow?.state === 'expired' || flow?.state === 'cancelled';
  async function act(action: Action) {
    if (!active() || running.current || !life.current) return;
    const signal = life.current.signal, current = status?.current;
    const valid = () => active() && !signal.aborted;
    const adopt = (next: AcquisitionFlow) => {
      if (!valid()) return;
      setStatus((previous) => previous && { ...previous, current: next });
      if (next.state === 'adopted') onAdopt();
    };
    running.current = true; sequence.current += 1; setBusy(true); setFailed(false);
    try {
      if (action === 'status') {
        const next = await api.status(kind, signal);
        if (valid()) {
          setStatus(next);
          if (next.reason === 'already_acquired') onAdopt();
          else if (current && next.current?.flow_id === current.flow_id && !['expired', 'cancelled', 'adopted'].includes(next.current.state)) {
            adopt(await api.read(kind, current.flow_id, signal));
          }
        }
      } else if (action === 'begin') {
        if (!status?.eligible || !canAcquire || !api.supported() || acquired) return;
        const next = await api.prepare(kind, signal); adopt(next);
        if (valid() && next.navigation_available) await api.openBrowser(kind, next.flow_id, signal, valid);
      } else if (current) {
        if (action === 'read') adopt(await api.read(kind, current.flow_id, signal));
        if (action === 'browser' && current.navigation_available && api.supported()) {
          await api.openBrowser(kind, current.flow_id, signal, valid);
        }
        if (action === 'submit' && canAcquire && status.eligible && current.review && current.state === 'confirmation_required') {
          adopt(await api.submit(kind, current.flow_id, current.review.review_id, signal));
        }
      }
    } catch { if (valid()) setFailed(true); }
    finally { running.current = false; if (valid()) setBusy(false); }
  }
  if (!canAcquire && !flow && !acquired) return null;
  const available = acquired && (!status || status.reason === 'already_acquired' || flow?.state === 'adopted');
  const eligible = Boolean(canAcquire && status?.eligible && !acquired && api.supported());
  const checkLabel = paid ? 'Check payment' : 'Check preview access';
  const initialLabel = paid ? 'Start test checkout' : 'Request free preview';
  const openLabel = paid ? 'Open test checkout' : 'Sign in';
  const state = flow?.state;
  return <Panel component="section" aria-label={paid ? 'Full report purchase' : 'Preview access'}>
    <SectionHeader title={paid ? 'Full report on recurring mentions' : 'One free preview'} />
    <Stack spacing={1.5}>
      {available ? <Typography role="status">{paid ? 'You’ve activated the text analysis module on this computer.' : 'One preview is available on this computer.'}</Typography> : <>
        {(!flow || terminal || state === 'prepared') && (paid ? <>
          <Typography>This one-time €9.99 test purchase covers version 1 of the text analysis module, including updates to that version. Use test payment details in Stripe. No real money is charged.</Typography>
          <Typography variant="body2">After checkout, return to Dipsy to activate the module on this computer. You can read your saved reports without activating it. If you choose to download a model, Dipsy asks you to confirm that separately.</Typography>
        </> : <Typography>Sign in, then confirm in Dipsy to allow one preview on this computer. Your preview counts the first time Dipsy shows you a usable result. If there’s nothing to show, you keep your preview.</Typography>)}
        {state === 'awaiting_browser' && <Typography role="status">{paid ? 'Finish the test checkout in your browser. Then return here and choose Check payment.' : 'Complete sign-in in your browser, then check preview access here.'}</Typography>}
        {state === 'confirmation_required' && flow?.review && <Typography role="status">
          {paid ? 'Your test payment is confirmed. Activate the module to run new analyses on this computer.' : 'You can now authorize one preview on this computer.'}
        </Typography>}
        {(state === 'confirming' || state === 'authorized') && <Typography role="status">The request is being verified. Check its outcome to finish.</Typography>}
        {state === 'adopted' && <Typography role="status">Access was saved on this computer. Check module access for its current availability.</Typography>}
        {terminal && <Typography role="status">{paid ? 'The checkout request' : 'The preview request'} {state === 'expired' ? 'expired.' : 'was cancelled.'}</Typography>}
        {(failed || state === 'unavailable') && <Alert severity="warning">Dipsy couldn’t verify access. On the original request, choose {checkLabel}.</Alert>}
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          {!failed && eligible && (!flow || terminal || state === 'prepared' || state === 'awaiting_browser' && !flow.navigation_available) &&
            <Button variant="contained" disabled={busy} onClick={() => void act('begin')}>{initialLabel}</Button>}
          {!failed && flow?.navigation_available && api.supported() &&
            <Button variant="outlined" disabled={busy} onClick={() => void act('browser')}>{openLabel}</Button>}
          {!failed && eligible && state === 'confirmation_required' && flow?.review &&
            <Button variant="contained" disabled={busy} onClick={() => void act('submit')}>{paid ? 'Activate on this computer' : 'Allow one preview'}</Button>}
          {!failed && flow && !terminal && state !== 'adopted' &&
            <Button variant="outlined" disabled={busy} onClick={() => void act('read')}>{checkLabel}</Button>}
          {(failed || state === 'adopted') && <Button variant="outlined" disabled={busy} onClick={() => void act('status')}>{checkLabel}</Button>}
        </Stack>
      </>}
    </Stack>
  </Panel>;
}
