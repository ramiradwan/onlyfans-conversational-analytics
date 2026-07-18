/** Centralizes MV3 chrome.* access used by the Agent transport. */

const IDENTITY_KEYS = [
  'agent_installation_id',
  'agent_stream_id',
  'last_acknowledged_source_seq',
  'applied_config_revision',
];
const INGESTION_STATE_KEY = 'durable_ingestion_v1';
const APPLIED_CONFIG_KEY = 'applied_agent_config_v1';
const COMMAND_STATE_KEY = 'durable_command_results_v1';


function storageGet(storage, keys) {
  return new Promise((resolve, reject) => {
    storage.get(keys, (value) => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve(value ?? {});
    });
  });
}

function storageSet(storage, value) {
  return new Promise((resolve, reject) => {
    storage.set(value, () => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

function storageRemove(storage, keys) {
  return new Promise((resolve, reject) => {
    storage.remove(keys, () => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

export function createChromeAdapter(chromeApi = globalThis.chrome, idFactory = () => crypto.randomUUID()) {
  if (!chromeApi?.storage?.local) throw new Error('chrome.storage.local is unavailable');
  const storage = chromeApi.storage.local;

  return {
    async loadAgentIdentity() {
      const saved = await storageGet(storage, IDENTITY_KEYS);
      const identity = {
        agentInstallationId: saved.agent_installation_id ?? idFactory(),
        agentStreamId: saved.agent_stream_id ?? idFactory(),
        lastAcknowledgedSourceSeq: Number.isSafeInteger(saved.last_acknowledged_source_seq)
          ? saved.last_acknowledged_source_seq
          : 0,
        appliedConfigRevision:
          typeof saved.applied_config_revision === 'string'
            ? saved.applied_config_revision
            : null,
      };
      await storageSet(storage, {
        agent_installation_id: identity.agentInstallationId,
        agent_stream_id: identity.agentStreamId,
        last_acknowledged_source_seq: identity.lastAcknowledgedSourceSeq,
        applied_config_revision: identity.appliedConfigRevision,
      });
      return identity;
    },

    saveAcknowledgedSourceSeq(sourceSeq) {
      return storageSet(storage, { last_acknowledged_source_seq: sourceSeq });
    },

    saveAppliedConfigRevision(revision) {
      return storageSet(storage, { applied_config_revision: revision });
    },

    async loadAppliedConfig() {
      const saved = await storageGet(storage, [APPLIED_CONFIG_KEY]);
      return saved[APPLIED_CONFIG_KEY] ?? null;
    },

    saveAppliedConfig(document) {
      return storageSet(storage, {
        [APPLIED_CONFIG_KEY]: document,
        applied_config_revision: document.config_revision,
      });
    },

    clearAppliedConfig() {
      return storageSet(storage, {
        [APPLIED_CONFIG_KEY]: null,
        applied_config_revision: null,
      });
    },

    async loadLegacyIngestionState() {
      const saved = await storageGet(storage, [INGESTION_STATE_KEY]);
      return saved[INGESTION_STATE_KEY] ?? null;
    },

    deleteLegacyIngestionState() {
      return storageRemove(storage, [INGESTION_STATE_KEY]);
    },

    async loadCommandState() {
      const saved = await storageGet(storage, [COMMAND_STATE_KEY]);
      return saved[COMMAND_STATE_KEY] ?? null;
    },

    saveCommandState(state) {
      return storageSet(storage, { [COMMAND_STATE_KEY]: state });
    },

    onWake(listener) {
      const events = [
        chromeApi.runtime?.onStartup,
        chromeApi.runtime?.onInstalled,
        chromeApi.runtime?.onMessage,
        chromeApi.tabs?.onUpdated,
      ].filter((event) => event?.addListener);
      const wrappers = events.map((event) => {
        const wrapper = () => listener();
        event.addListener(wrapper);
        return [event, wrapper];
      });
      return () => wrappers.forEach(([event, wrapper]) => event.removeListener?.(wrapper));
    },
  };
}
