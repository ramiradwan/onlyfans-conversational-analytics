import { buildWorkerRecoveryDiagnostic, readSessionFailures } from './stable-connection-diagnostic.mjs';

export async function readCatchupMessageIds(chat) {
  const signal = AbortSignal.timeout(10_000);
  let status = 'unavailable', code = 'unavailable';
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await fetch('/api/v1/conversations/' + chat + '/messages?limit=100', { signal });
      status = response.status;
      const body = await response.json().catch(() => null);
      const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.code ?? body?.code;
      code = typeof detail === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(detail) ? detail : 'unavailable';
      if (response.ok && Array.isArray(body?.items)) return body.items.map(item => item.message_id);
      if (status !== 409 || code !== 'cursor_stale') break;
    }
  } catch {}
  throw new Error(`Catch-up messages unavailable: chat=${chat} status=${status} code=${code}`);
}

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
  counters.setupTrace ??= [];
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
      const catchup = components.history.catchup;
      const trace = (event, operation = null) => {
        try {
          const config = catchup.configuration(), session = catchup.session();
          counters.setupTrace.push({ event,
            operation: ['capture.state.report', 'history.check.begin'].includes(operation) ? operation : null,
            sessionPresent: session != null,
            configurationEnabled: config?.history_acquisition?.enabled === true,
            configurationApplied: session?.applied_config_revision != null
              && session.applied_config_revision === config?.config_revision });
          if (counters.setupTrace.length > 32) counters.setupTrace.shift();
        } catch {}
      };
      for (const [owner, method, event] of [
        [catchup, 'requestCaptureStateReport', 'notification'], [catchup, 'rpc', 'rpc'],
        [components.transport, 'onSession', 'admission'],
      ]) {
        const original = owner?.[method];
        if (typeof original !== 'function') continue;
        owner[method] = function (...args) {
          trace(event, event === 'rpc' ? args[0] : null);
          return original.apply(this, args);
        };
      }
      trace('initialized');
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
    recovery: buildWorkerRecoveryDiagnostic(worker?.recovery),
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
    setupTrace: Array.isArray(shim.setupTrace) ? shim.setupTrace.slice(-32).map(entry => ({
      event: enumValue(entry?.event, ['initialized', 'notification', 'admission', 'rpc']),
      operation: enumValue(entry?.operation, ['capture.state.report', 'history.check.begin']),
      sessionPresent: boolean(entry?.sessionPresent), configurationEnabled: boolean(entry?.configurationEnabled),
      configurationApplied: boolean(entry?.configurationApplied),
    })) : [],
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

export async function readCatchupWorker(context, findWorker = async context => {
  const { extensionWorker } = await import('./extension-browser.mjs');
  return extensionWorker(context, { timeoutMs: 1_000 });
}) {
  const worker = await findWorker(context);
  return worker.evaluate(async () => {
    const read = async callback => {
      let timer;
      try {
        return await Promise.race([Promise.resolve().then(callback),
          new Promise(resolve => { timer = setTimeout(() => resolve(null), 1_000); })]);
      } catch { return null; }
      finally { clearTimeout(timer); }
    };
    const [status, recovery] = await Promise.all([
      read(() => globalThis.__OFCA_CATCHUP_STATUS__?.()),
      read(() => globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__?.()),
    ]);
    return { shim: globalThis.__OFCA_CATCHUP_SHIM__ ?? null, status, recovery };
  });
}

export async function withCatchupDiagnostics(assertion, { summary, platform, context, brain,
  readWorker = () => readCatchupWorker(context), log = line => console.error(line), timeoutMs = 2_500,
  checkpoint = null }) {
  const report = async prefix => {
    let fields;
    try {
      const [lastSummary, worker] = await Promise.all([boundedRead(summary, timeoutMs), boundedRead(readWorker, timeoutMs)]);
      fields = catchupDiagnosticFields(lastSummary, platform.requestCounts, worker);
      fields.brainSessionFailures = brain?.sessionFailures?.() ?? readSessionFailures(brain?.recentOutput?.() ?? '');
    } catch { fields = catchupDiagnosticFields(null, null, null); }
    try { log(`${prefix} ${JSON.stringify(fields)}`); } catch {}
  };
  let result;
  try { result = await assertion(); }
  catch (error) {
    await report('Catch-up poll timed out');
    throw error;
  }
  if (checkpoint === 'initial_history') await report('Catch-up checkpoint initial_history');
  return result;
}
