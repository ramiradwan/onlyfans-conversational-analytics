import { Button, Stack } from '@mui/material';
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { findingsBaseline } from '../analytics';
import { createRecurringMentionsClient, type PaidInsightsCapability } from '../analytics/recurringMentions';
import { moduleEngineAcquisitionApi, previewEngineAcquisitionApi } from '../api/engineAcquisition';
import { AnalyticsPresentation } from '../components/analytics';
import { ConversationQuestions } from '../components/analytics/ConversationQuestions';
import { RecurringMentionsAcquisition } from '../components/analytics/RecurringMentionsAcquisition';
import { RecurringMentionsReport } from '../components/analytics/RecurringMentionsReport';
import { TaskFindings } from '../components/analytics/TaskFindings';
import { TextAnalysisPreviews } from '../components/analytics/TextAnalysisPreviews';
import { ModelDownloadControls } from '../components/NlpDependencyControls';
import { TextAnalysisExecutionControls } from '../components/TextAnalysisExecutionControls';
import { Panel, SectionHeader } from '../components/ui';
import { analyticsStoreActions, useAnalyticsStore } from '../store/analyticsStore';
import { createFindingsPresentation } from '../store/findingsStore';
import { textAnalysisScopeKey, textAnalysisScopeReady } from '../store/textAnalysisExecutionStore';
import { bridgeTransportStore } from '../store/transportStore';
import { useUserRole } from '../store/userStore';

const findingsPresentation = createFindingsPresentation();

export default function AnalyticsView() {
  const account = useSyncExternalStore(bridgeTransportStore.subscribe, () => bridgeTransportStore.getState().creatorAccountId);
  const role = useUserRole();
  const currentScope = () => textAnalysisScopeKey(bridgeTransportStore.getState(), role, account);
  const scope = useSyncExternalStore(bridgeTransportStore.subscribe, currentScope, currentScope);
  const selection = useMemo(() => ({ current: null as string | null }), [scope]);
  const client = useMemo(() => createRecurringMentionsClient(), []);
  const [revision, setRevision] = useState(0);
  const checkAccess = useCallback(() => setRevision((value) => value + 1), []);
  const [modelPrepared, setModelPrepared] = useState(false);
  const [pilot, setPilot] = useState<{ scope: string; value: PaidInsightsCapability | null } | null>(null);
  useEffect(() => {
    if (!textAnalysisScopeReady(bridgeTransportStore.getState(), role, account)) return;
    const controller = new AbortController();
    void client.capability(controller.signal).then((value) => {
      if (!controller.signal.aborted) setPilot({ scope, value });
    }).catch(() => {
      if (!controller.signal.aborted) setPilot((previous) =>
        previous?.scope === scope && previous.value
          ? { scope, value: { ...previous.value, can_acquire_preview: false, can_preview: false, can_purchase: false, can_process: false } }
          : { scope, value: null });
    });
    return () => controller.abort();
  }, [account, client, revision, role, scope]);
  const access = pilot?.scope === scope ? pilot.value : null;
  const state = useAnalyticsStore((store) => store.state);
  const dateRange = useAnalyticsStore((store) => store.dateRange);
  const baseline = useMemo(() => (state.status === 'baseline' || state.status === 'model') && !state.isRefreshing
    ? findingsBaseline(state.data, dateRange) : null, [state, dateRange]);

  return (
    <AnalyticsPresentation
      questions={<ConversationQuestions />}
      findings={<><TaskFindings baseline={baseline} presentation={findingsPresentation} onRefreshBaseline={analyticsStoreActions.refresh} />
        {!access && <TextAnalysisPreviews creatorAccountId={account} />}</>}
      recurringMentions={access && account && <Stack key={scope} spacing={2}>
        <Panel component="section" aria-label="Recurring mentions">
          <SectionHeader title="Recurring mentions" summary="Exact phrases that two or more senders used in messages to you. Analysis runs on this computer." />
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button onClick={checkAccess}>Check module access</Button>
          </Stack>
        </Panel>
        <ModelDownloadControls key={access.profile_sha256} account={account} preferredProfile={access.profile_sha256} onReadinessChange={setModelPrepared} />
        <RecurringMentionsAcquisition kind="preview" scope={scope} account={account} canAcquire={access.can_acquire_preview} acquired={access.can_preview}
          onAdopt={checkAccess} />
        <TextAnalysisPreviews creatorAccountId={account} recurringProfile={access.profile_sha256} canPrepare={access.can_preview}
          onPreviewShown={checkAccess} modelPrepared={modelPrepared} engineApi={access.can_process ? moduleEngineAcquisitionApi : previewEngineAcquisitionApi} />
        <RecurringMentionsAcquisition kind="paid" scope={scope} account={account} canAcquire={access.can_purchase} acquired={access.can_process}
          onAdopt={checkAccess} />
        {access.can_process && <TextAnalysisExecutionControls creatorAccountId={account} preferredProfile={access.profile_sha256} modelPrepared={modelPrepared} />}
        <RecurringMentionsReport account={account} baseline={baseline} client={client} selection={selection} onRefreshBaseline={analyticsStoreActions.refresh} />
      </Stack>}
      state={state}
      dateRange={dateRange}
      onDateRangeChange={(range) => void analyticsStoreActions.setDateRange(range)}
      onRetry={() => void analyticsStoreActions.refresh()}
    />
  );
}
