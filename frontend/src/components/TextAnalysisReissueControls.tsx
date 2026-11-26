import SettingsBackupRestoreOutlined from '@mui/icons-material/SettingsBackupRestoreOutlined';
import { Button, Divider, Stack } from '@mui/material';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { Panel, SectionHeader, useReportedStatus, useRevealHold, type SectionMenuItem, type SectionStatus } from './ui';
import { StatusLine } from './ui/ReservedRegion';
import { textAnalysisReissueApi, type ReissueStatusView, type TextAnalysisReissueApi } from '../services/textAnalysisReissueApi';
import { textAnalysisScopeKey, textAnalysisScopeReady } from '../store/textAnalysisExecutionStore';
import { bridgeTransportStore, type BridgeTransportStore } from '../store/transportStore';
import { useUserRole, useUserStore } from '../store/userStore';

export const RECOVER_LICENCE = 'Recover existing licence';

interface Props {
  creatorAccountId: string | null; api?: TextAnalysisReissueApi;
  transport?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
  onStatus?: (status: SectionStatus | null) => void;
  /** Receives the menu item that starts a recovery, for the section that hosts it. */
  onMenu?: (items: readonly SectionMenuItem[] | null) => void;
}
export function TextAnalysisReissueControls(props: Props) {
  const { creatorAccountId, onMenu } = props;
  const transport = props.transport ?? bridgeTransportStore;
  const context = useSyncExternalStore(transport.subscribe, transport.getState, transport.getState);
  const role = useUserRole();
  // Recovery shows once one is under way or needs attention. That lasts for the account, because a reconnect
  // starts a new session below and must not hide and show the section again.
  const [shownFor, setShownFor] = useState<string | null>(null);
  const reveal = useCallback(() => setShownFor(creatorAccountId), [creatorAccountId]);
  useEffect(() => () => onMenu?.(null), [onMenu]);
  return <ReissueSession key={textAnalysisScopeKey(context, role, creatorAccountId)} {...props} transport={transport}
    shown={shownFor !== null && shownFor === creatorAccountId} onUnderWay={reveal} />;
}
function ReissueSession({ creatorAccountId, api = textAnalysisReissueApi, transport = bridgeTransportStore, onStatus, onMenu, shown, onUnderWay }: Props & {
  shown: boolean; onUnderWay: () => void;
}) {
  const [view, setView] = useState<ReissueStatusView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  // A failed automatic read can't tell whether a recovery is under way. It blocks starting one, but it doesn't
  // show recovery to someone who may never have started one.
  const [readFailed, setReadFailed] = useState(false);
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
    setView(null); setError(false); setReadFailed(false); setBusy(true); setExpiredChecked(false);
    if (active()) void api.status(controller.signal).then(value => { if (current()) setView(value); })
      .catch(() => { if (current()) setReadFailed(true); })
      .finally(() => { if (current()) setBusy(false); });
    else setBusy(false);
    return () => controller.abort();
  }, [api, active]);
  async function action(kind: 'status' | 'begin' | 'resume' | 'browser') {
    if (!active() || running.current !== null) return;
    const request = ++sequence.current, controller = lifetime.current!;
    const currentRequest = () => active() && lifetime.current === controller && !controller.signal.aborted && sequence.current === request;
    running.current = request; setBusy(true); setError(false); setReadFailed(false);
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
  const failed = error || readFailed;
  // The first read holds the section, so a recovery already under way shows with it.
  useRevealHold(textAnalysisScopeReady(transport.getState(), useUserStore.getState().role, creatorAccountId) && view === null && !failed);
  const underWay = error || Boolean(current);
  const visible = shown || underWay;
  useLayoutEffect(() => { if (underWay) onUnderWay(); }, [underWay, onUnderWay]);

  // The menu keeps its item while a new session loads, so the menu button never comes and goes.
  const canBegin = ready && !busy && !failed && view?.current === null;
  const latestAction = useRef(action);
  useEffect(() => { latestAction.current = action; });
  useEffect(() => {
    onMenu?.([{ label: RECOVER_LICENCE, disabled: !canBegin, onSelect: () => { void latestAction.current('begin'); } }]);
  }, [canBegin, onMenu]);

  const message = !ready ? 'Connect with an authorized creator session to recover access.'
    : failed ? 'Recovery could not be verified. Check the saved status before continuing.'
      : current?.state === 'adopted' ? 'Access is saved on this computer.'
        : current?.state === 'expired' ? 'The original request has expired. Check its recorded outcome before starting another request.'
          : current ? 'Continue the original request in your account, then check its outcome here.'
            : view ? 'No recovery is under way.' : 'Checking saved recovery status…';
  const headerStatus: SectionStatus | null = !visible ? null
    : failed ? { label: 'Needs attention', tone: 'warning' }
      : busy ? { label: 'Checking…', tone: 'default' }
        : !ready ? { label: 'Unavailable', tone: 'default' }
          : current?.state === 'adopted' ? { label: 'Saved', tone: 'success' }
            : current?.state === 'expired' ? { label: 'Expired', tone: 'warning' }
              : current ? { label: 'In progress', tone: 'info' }
                : { label: 'Not recovered', tone: 'default' };
  useReportedStatus(onStatus, headerStatus);
  if (!visible) return null;
  return <>
    <Divider />
    <Panel component="section" aria-label="Recover text analysis access">
      <SectionHeader
        icon={<SettingsBackupRestoreOutlined />}
        title="Recover text analysis access"
        summary="Use your account to restore an existing licence on a replacement computer."
        status={headerStatus}
      />
      <Stack spacing={1.5} data-journey-state="textanalysis.licence_recovery">
        <StatusLine id="reissue-feedback" text={message} summary={failed ? 'Needs attention' : 'Status'}
          quietSummary tone={failed ? 'error' : 'secondary'} />
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" disabled={!ready || busy} onClick={() => void action('status')}>Check saved recovery status</Button>
          {current?.state === 'expired' && expiredChecked && <Button size="small" variant="outlined" disabled={busy || error} onClick={() => void action('begin')}>Start a new recovery request</Button>}
          {current && current.state !== 'adopted' && <Button size="small" variant="outlined" disabled={busy} onClick={() => void action('resume')}>Check original recovery request</Button>}
          {current?.navigation_available && <Button size="small" variant="outlined" disabled={busy} onClick={() => void action('browser')}>Continue recovery in browser</Button>}
        </Stack>
      </Stack>
    </Panel>
  </>;
}
