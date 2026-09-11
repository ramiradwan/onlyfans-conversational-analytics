import {
  INGESTION_DATABASE_NAME_PREFIX,
  LEGACY_INGESTION_DATABASE_NAME_PREFIX,
  accountDatabaseName,
} from './indexeddb-ingestion-storage.mjs';
import {
  ACTIVE_ACCOUNT_PARTITION_KEY,
  BRAIN_BINDING_MESSAGE_TYPE,
  FULL_STORAGE_BOOTSTRAP_KEY,
  RECONCILE_ALARM_NAME,
  createBrainBindingBridgeCore,
  createChromeAdapterCore,
} from './chrome-adapter-core.mjs';
import {
  LOCAL_SERVICE_ORIGIN,
  LOCAL_SERVICE_STORAGE_ROTATE,
  LOCAL_SERVICE_STORAGE_UNSEAL,
} from './local-service-endpoints.mjs';

export {
  ACTIVE_ACCOUNT_PARTITION_KEY,
  BRAIN_BINDING_MESSAGE_TYPE,
  FULL_STORAGE_BOOTSTRAP_KEY,
  RECONCILE_ALARM_NAME,
};

export function createChromeAdapter(
  chromeApi = globalThis.chrome,
  idFactory = () => crypto.randomUUID(),
  options = {},
) {
  return createChromeAdapterCore({
    chromeApi,
    idFactory,
    storageUnsealEndpoint: options.storageUnsealEndpoint ?? LOCAL_SERVICE_STORAGE_UNSEAL,
    storageRotateEndpoint: options.storageRotateEndpoint ?? LOCAL_SERVICE_STORAGE_ROTATE,
    ...options,
    accountDatabaseName,
    encryptedPrefix: INGESTION_DATABASE_NAME_PREFIX,
    legacyPrefix: LEGACY_INGESTION_DATABASE_NAME_PREFIX,
  });
}

export function createBrainBindingBridge(options = {}) {
  return createBrainBindingBridgeCore({
    allowedOrigins: [LOCAL_SERVICE_ORIGIN],
    ...options,
  });
}
