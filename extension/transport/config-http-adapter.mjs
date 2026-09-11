import { parseAgentConfigGetRequest } from '../protocol/index.mjs';
import { createSecureLocalFetch } from './secure-local-fetch.mjs';
import { LOCAL_SERVICE_CONFIG } from './local-service-endpoints.mjs';

export class ConfigFetchError extends Error {
  constructor(code, detail, retryable = false, status = null) {
    super(detail);
    this.name = 'ConfigFetchError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

export function createConfigHttpAdapter(options = {}) {
  const endpoint = options.endpoint ?? LOCAL_SERVICE_CONFIG;
  const fetchImpl = createSecureLocalFetch({ fetchImpl: options.fetchImpl ?? globalThis.fetch });
  if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');
  return {
    async fetchConfig(context) {
      const request = parseAgentConfigGetRequest({
        operation: 'agent.config.get',
        protocol_version: '2',
        auth_ticket: context.authTicket,
        agent_installation_id: context.agentInstallationId,
        creator_account_id: context.creatorAccountId,
        current_etag: context.currentEtag,
        current_config_revision: context.currentConfigRevision,
        supported_config_schema_versions: context.supportedSchemaVersions,
      });
      const url = new URL(endpoint);
      for (const [key, value] of Object.entries(request)) {
        if (key === 'auth_ticket' || value === null) continue;
        if (Array.isArray(value)) for (const item of value) url.searchParams.append(key, item);
        else url.searchParams.set(key, String(value));
      }
      try {
        const response = await fetchImpl(url.toString(), {
          method: 'GET',
          headers: {
            Authorization: `Bearer ${request.auth_ticket}`,
            ...(request.current_etag ? { 'If-None-Match': request.current_etag } : {}),
          },
          signal: context.signal,
        });
        const etag = response.headers?.get?.('etag') ?? null;
        if (response.status === 304) return { status: 304, etag, document: null };
        let document = null;
        if (response.status === 200) {
          try { document = await response.json(); }
          catch { throw new ConfigFetchError('invalid_response', 'Brain returned a non-JSON configuration document', true); }
        }
        return { status: response.status, etag, document };
      } catch (error) {
        context.signal?.throwIfAborted();
        if (error instanceof ConfigFetchError) throw error;
        if (error?.name === 'AbortError' || error?.code === 'local_service_timeout') {
          throw new ConfigFetchError('timeout', 'Agent configuration fetch timed out', true);
        }
        throw new ConfigFetchError('network_error', 'Agent configuration fetch failed', true);
      }
    },
  };
}
