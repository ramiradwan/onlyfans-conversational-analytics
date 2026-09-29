import { extensionWorker } from './extension-browser.mjs';

// Serialized into the copied worker with no module dependencies.
export function installCatchupShim(agentRuntime, chromeApi, root, readStatus) {
  const operations = () => ({ identity: 0, conversations: 0, 'message-page': 0, other: 0 });
  const failure = () => ({ count: 0, errorName: null });
  const counters = root.__OFCA_CATCHUP_SHIM__ ??= {
    initializeWrapped: 0, initializeCalls: 0, initializeCompleted: 0,
    installed: { history: 0, catchup: 0 }, lastInstalled: { history: false, catchup: false },
    calls: { history: operations(), catchup: operations() },
    failures: { tab_unavailable: failure(), execute_script_failed: failure(), binding_failed: failure(), other: failure() },
  };
  const errorName = error => ['Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError',
    'AbortError', 'TimeoutError', 'InvalidStateError', 'SecurityError', 'NotAllowedError'].includes(error?.name)
    ? error.name : 'Error';
  const recordFailure = (kind, error) => {
    counters.failures[kind].count++;
    counters.failures[kind].errorName = errorName(error);
  };
  root.__OFCA_CATCHUP_STATUS__ = async () => {
    const status = await readStatus();
    return {
      consentMode: status.consent?.mode, phase: status.phase,
      historyError: status.delivery?.history_error_code,
      startupError: status.delivery?.startup_error_code,
      transportState: status.delivery?.transport_state,
      historyPermission: status.history_permission,
      pendingEntries: status.delivery?.pending_entries,
      capturedChats: status.delivery?.captured_chats,
      capturedMessages: status.delivery?.captured_messages,
      historyEnabled: agentRuntime.configuration?.activeDocument?.history_acquisition?.enabled,
    };
  };
  const initialize = agentRuntime.initialize;
  counters.initializeWrapped++;
  agentRuntime.initialize = async (...args) => {
    counters.initializeCalls++;
    counters.lastInstalled = { history: false, catchup: false };
    try {
      const components = await initialize.apply(agentRuntime, args);
      const signer = category => ({ async read(request) {
        const operation = Object.hasOwn(counters.calls[category], request.operation) ? request.operation : 'other';
        counters.calls[category][operation]++;
        let stage = 'other';
        try {
          request.signal?.throwIfAborted();
          stage = 'tab_unavailable';
          const tabs = await chromeApi.tabs.query({ url: ['https://onlyfans.com/*'] });
          const tab = tabs.find(tab => tab.frozen !== true && tab.discarded !== true);
          if (!tab) throw new Error('Synthetic tab unavailable');
          const kind = request.operation === 'identity' ? 'identity'
            : request.operation === 'conversations' ? category + '_list' : category + '_messages';
          stage = 'execute_script_failed';
          const results = await chromeApi.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
            func: async (request, kind) => {
              try {
                return { ok: true, value: await globalThis.syntheticCatchupRead(request, kind) };
              } catch (error) {
                return { ok: false, errorName: error?.name };
              }
            }, args: [{ operation: request.operation, parameters: request.parameters }, kind] });
          stage = 'other';
          request.signal?.throwIfAborted();
          stage = 'binding_failed';
          const result = results?.[0]?.result;
          if (result?.ok !== true) {
            throw Object.assign(new Error('Synthetic binding failed'), { name: errorName({ name: result?.errorName }) });
          }
          return result.value;
        } catch (error) {
          recordFailure(stage, error);
          throw error;
        }
      } });
      for (const [key, category] of [['initial', 'history'], ['catchup', 'catchup']]) {
        components.history[key].signer = signer(category);
        counters.installed[category]++;
        counters.lastInstalled[category] = true;
      }
      counters.initializeCompleted++;
      return components;
    } catch (error) {
      recordFailure('other', error);
      throw error;
    }
  };
}

const enumValue = (value, allowed) => allowed.includes(value) ? value : null;
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const boolean = value => typeof value === 'boolean' ? value : null;
const timestamp = value => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && Number.isFinite(Date.parse(value)) ? value : null;

export function catchupDiagnosticFields(summary, requestCounts, worker) {
  const coverage = summary?.coverage;
  const status = worker?.status;
  return {
    summary: {
      viewRevision: count(summary?.viewRevision), conversationCount: count(summary?.conversationCount),
      messageCount: count(summary?.messageCount), analyticsBasis: enumValue(summary?.analyticsBasis, ['complete', 'synced_subset']),
      summaryOnly: boolean(summary?.summaryOnly), agentStatus: enumValue(summary?.agentStatus, ['connected', 'stale', 'disconnected']),
      lastHeartbeatAt: timestamp(summary?.lastHeartbeatAt),
      configRevisionsMatch: typeof summary?.appliedConfigRevision === 'string' && typeof summary?.requiredConfigRevision === 'string'
        ? summary.appliedConfigRevision === summary.requiredConfigRevision : null,
    },
    coverage: {
      status: enumValue(coverage?.status, ['unknown', 'partial', 'complete']),
      phase: enumValue(coverage?.phase, ['not_started', 'discovering', 'backfilling', 'paused', 'repairing', 'blocked', 'complete']),
      reason: enumValue(coverage?.reason, ['consent_revoked', 'configuration_not_applied', 'history_sync_paused',
        'inventory_not_frozen', 'conversation_evidence_missing', 'new_generation', 'new_conversation_discovered']),
      discovered_conversations: count(coverage?.discovered_conversations), complete_conversations: count(coverage?.complete_conversations),
      as_of: timestamp(coverage?.as_of), complete_as_of: timestamp(coverage?.complete_as_of),
    },
    catchup: {
      status: enumValue(summary?.catchupFreshness?.status, ['paused', 'checking', 'never_checked', 'behind', 'current']),
      reason: enumValue(summary?.catchupFreshness?.reason, ['user_paused', 'consent_needed', 'extension_offline', 'no_onlyfans_tab',
        'onlyfans_sleeping', 'account_changed', 'applying_settings', 'capture_off', 'extension_outdated', 'catch_up', 'canary',
        'awaiting_check', 'daily_cap', 'check_incomplete', 'not_observing']),
    },
    requestCounts: Object.fromEntries(['history_list', 'history_messages', 'catchup_list', 'catchup_messages', 'canary_list', 'identity']
      .map(key => [key, count(requestCounts?.[key])])),
    shim: safeShimCounters(worker?.shim),
    extension: {
      consentMode: enumValue(status?.consentMode, ['off', 'preview', 'full', 'paused', 'revoked']),
      phase: enumValue(status?.phase, ['booting', 'transitioning', 'off', 'preview', 'full', 'paused', 'revoked', 'identity', 'permission_required', 'unavailable']),
      historyError: enumValue(status?.historyError, ['history_unavailable', 'unsupported_browser', 'identity_required']),
      startupError: enumValue(status?.startupError, ['startup_failed', 'local_service_unavailable', 'local_service_timeout']),
      transportState: enumValue(status?.transportState, ['authenticated', 'authenticating', 'disconnected']),
      historyPermission: boolean(status?.historyPermission), historyEnabled: boolean(status?.historyEnabled),
      pendingEntries: count(status?.pendingEntries), capturedChats: count(status?.capturedChats), capturedMessages: count(status?.capturedMessages),
    },
  };
}

function safeShimCounters(shim) {
  if (!shim) return null;
  const categories = ['history', 'catchup'];
  return {
    initializeWrapped: count(shim.initializeWrapped), initializeCalls: count(shim.initializeCalls),
    initializeCompleted: count(shim.initializeCompleted),
    installed: Object.fromEntries(categories.map(key => [key, count(shim.installed?.[key])])),
    lastInstalled: Object.fromEntries(categories.map(key => [key, boolean(shim.lastInstalled?.[key])])),
    calls: Object.fromEntries(categories.map(category => [category, Object.fromEntries(
      ['identity', 'conversations', 'message-page', 'other'].map(operation => [operation, count(shim.calls?.[category]?.[operation])]),
    )])),
    failures: Object.fromEntries(['tab_unavailable', 'execute_script_failed', 'binding_failed', 'other'].map(kind => [kind, {
      count: count(shim.failures?.[kind]?.count), errorName: enumValue(shim.failures?.[kind]?.errorName,
        ['Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'AbortError', 'TimeoutError',
          'InvalidStateError', 'SecurityError', 'NotAllowedError']),
    }])),
  };
}

async function boundedRead(read, timeoutMs) {
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(read),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), timeoutMs); })]);
  } catch { return null; }
  finally { clearTimeout(timer); }
}

export async function readCatchupWorker(context) {
  const worker = await extensionWorker(context, { timeoutMs: 1_000 });
  return worker.evaluate(async () => {
    let timer;
    let status = null;
    try {
      status = await Promise.race([globalThis.__OFCA_CATCHUP_STATUS__?.(),
        new Promise(resolve => { timer = setTimeout(() => resolve(null), 1_000); })]);
    } catch {}
    finally { clearTimeout(timer); }
    return { shim: globalThis.__OFCA_CATCHUP_SHIM__ ?? null, status };
  });
}

export async function withCatchupDiagnostics(assertion, { summary, platform, context,
  readWorker = () => readCatchupWorker(context), log = line => console.error(line), timeoutMs = 2_500 }) {
  try { return await assertion(); }
  catch (error) {
    let fields;
    try {
      const [lastSummary, worker] = await Promise.all([boundedRead(summary, timeoutMs), boundedRead(readWorker, timeoutMs)]);
      fields = catchupDiagnosticFields(lastSummary, platform.requestCounts, worker);
    } catch { fields = catchupDiagnosticFields(null, null, null); }
    try { log(`Catch-up poll timed out ${JSON.stringify(fields)}`); } catch {}
    throw error;
  }
}
