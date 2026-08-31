export type CreatorVaultPolicyType =
  | 'disabled'
  | 'finite'
  | 'export_and_delete'
  | 'indefinite_until_delete';

export type CreatorVaultCommandAction =
  | 'enable_finite'
  | 'enable_indefinite'
  | 'disable'
  | 'delete_message'
  | 'delete_conversation'
  | 'delete_participant'
  | 'delete_all'
  | 'unlink';

export type UnlinkArchiveTreatment = 'preserve' | 'delete';

export interface CreatorVaultDeletionOperation {
  operation_id: string;
  status: 'pending' | 'incomplete' | 'complete';
  deletion_revision: number;
}

export interface CreatorVaultStatus {
  creator_account_id: string;
  policy: {
    enabled: boolean;
    policy_type: CreatorVaultPolicyType;
    finite_horizon_days: number | null;
    revision: number;
  };
  capabilities: {
    finite_retention: boolean;
    indefinite_retention: boolean;
    deletion_scopes: Array<'message' | 'conversation' | 'participant' | 'all'>;
    unlink_archive_treatments: UnlinkArchiveTreatment[];
    export: boolean;
  };
  deletion_operation?: CreatorVaultDeletionOperation | null;
}

export interface CreatorVaultCommand {
  action: CreatorVaultCommandAction;
  finite_horizon_days?: number;
  target_id?: string;
  unlink_archive_treatment?: UnlinkArchiveTreatment;
}

export interface CreatorVaultCommandResult {
  action: CreatorVaultCommandAction;
  status: CreatorVaultStatus;
  deletion_revision: number | null;
  deletion_operation?: CreatorVaultDeletionOperation | null;
  unlink_archive_treatment: UnlinkArchiveTreatment | null;
}

export interface CreatorVaultExportDocument {
  manifest: {
    export_type: 'creator_vault';
    content: {
      conversation_count: number;
      message_count: number;
      sha256: string;
    };
    copy_domains: {
      managed_recovery: {
        inspection_complete: boolean;
        copies_may_remain: boolean;
      };
      this_export_after_delivery: {
        managed_by_product: false;
        observable_by_product: false;
        managed_vault_deletion_applies: false;
      };
    };
  };
  conversations: unknown[];
  messages: unknown[];
}

export class CreatorVaultApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = 'CreatorVaultApiError';
  }
}

export interface CreatorVaultApi {
  get(signal?: AbortSignal): Promise<CreatorVaultStatus>;
  command(input: CreatorVaultCommand, signal?: AbortSignal): Promise<CreatorVaultCommandResult>;
  retryDeletion?(operationId: string, signal?: AbortSignal): Promise<CreatorVaultDeletionOperation>;
  exportDocument(signal?: AbortSignal): Promise<CreatorVaultExportDocument>;
}

interface CreatorVaultApiOptions {
  baseUrl?: string;
  csrfHeaderName?: string;
  fetch?: typeof fetch;
  getCsrfToken?: () => string | null | Promise<string | null>;
}

function defaultCsrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

function objectValue(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new CreatorVaultApiError('Brain returned an invalid Creator Vault document.');
  }
  return value as Record<string, unknown>;
}

function parseDeletionOperation(value: unknown): CreatorVaultDeletionOperation {
  const operation = objectValue(value);
  if (
    typeof operation.operation_id !== 'string'
    || !['pending', 'incomplete', 'complete'].includes(String(operation.status))
    || typeof operation.deletion_revision !== 'number'
  ) {
    throw new CreatorVaultApiError('Brain returned an invalid Creator Vault deletion operation.');
  }
  return value as CreatorVaultDeletionOperation;
}

function parseStatus(value: unknown): CreatorVaultStatus {
  const root = objectValue(value);
  const policy = objectValue(root.policy);
  const capabilities = objectValue(root.capabilities);
  if (
    typeof root.creator_account_id !== 'string'
    || typeof policy.enabled !== 'boolean'
    || typeof policy.policy_type !== 'string'
    || !(policy.finite_horizon_days === null || typeof policy.finite_horizon_days === 'number')
    || typeof policy.revision !== 'number'
    || typeof capabilities.finite_retention !== 'boolean'
    || typeof capabilities.indefinite_retention !== 'boolean'
    || !Array.isArray(capabilities.deletion_scopes)
    || !Array.isArray(capabilities.unlink_archive_treatments)
    || typeof capabilities.export !== 'boolean'
    || !(
      root.deletion_operation === undefined
      || root.deletion_operation === null
      || typeof root.deletion_operation === 'object'
    )
  ) {
    throw new CreatorVaultApiError('Brain returned an invalid Creator Vault status.');
  }
  if (root.deletion_operation !== undefined && root.deletion_operation !== null) {
    parseDeletionOperation(root.deletion_operation);
  }
  return value as CreatorVaultStatus;
}

function parseCommandResult(value: unknown): CreatorVaultCommandResult {
  const root = objectValue(value);
  if (typeof root.action !== 'string') {
    throw new CreatorVaultApiError('Brain returned an invalid Creator Vault command result.');
  }
  parseStatus(root.status);
  if (root.deletion_operation !== undefined && root.deletion_operation !== null) {
    parseDeletionOperation(root.deletion_operation);
  }
  return value as CreatorVaultCommandResult;
}

function parseExport(value: unknown): CreatorVaultExportDocument {
  const root = objectValue(value);
  const manifest = objectValue(root.manifest);
  const content = objectValue(manifest.content);
  const copyDomains = objectValue(manifest.copy_domains);
  const recovery = objectValue(copyDomains.managed_recovery);
  if (
    manifest.export_type !== 'creator_vault'
    || typeof content.message_count !== 'number'
    || typeof content.conversation_count !== 'number'
    || typeof content.sha256 !== 'string'
    || typeof recovery.inspection_complete !== 'boolean'
    || typeof recovery.copies_may_remain !== 'boolean'
    || !Array.isArray(root.conversations)
    || !Array.isArray(root.messages)
  ) {
    throw new CreatorVaultApiError('Brain returned an invalid Creator Vault export.');
  }
  return value as CreatorVaultExportDocument;
}

async function jsonResponse<T>(
  response: Response,
  parser: (value: unknown) => T,
  label: string,
): Promise<T> {
  if (!response.ok) {
    throw new CreatorVaultApiError(`${label} failed (${response.status}).`, response.status);
  }
  return parser(await response.json());
}

export function createCreatorVaultApi(
  options: CreatorVaultApiOptions = {},
): CreatorVaultApi {
  const request = options.fetch ?? globalThis.fetch.bind(globalThis);
  const csrfHeaderName = options.csrfHeaderName ?? 'X-CSRF-Token';
  const getCsrfToken = options.getCsrfToken ?? defaultCsrfToken;
  const endpoint = `${(options.baseUrl ?? '').replace(/\/$/, '')}/api/v1/settings/creator-vault`;

  const csrfPost = async (url: string, body: string | undefined, signal?: AbortSignal) => {
    const csrf = await getCsrfToken();
    if (!csrf) {
      throw new CreatorVaultApiError('A CSRF token is required to change Creator Vault settings.');
    }
    return request(url, {
      body,
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        [csrfHeaderName]: csrf,
      },
      method: 'POST',
      signal,
    });
  };

  return {
    async get(signal) {
      return jsonResponse(
        await request(endpoint, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
          method: 'GET',
          signal,
        }),
        parseStatus,
        'Creator Vault status request',
      );
    },

    async command(input, signal) {
      return jsonResponse(
        await csrfPost(`${endpoint}/commands`, JSON.stringify(input), signal),
        parseCommandResult,
        'Creator Vault command',
      );
    },

    async retryDeletion(operationId, signal) {
      return jsonResponse(
        await csrfPost(`${endpoint}/deletions/${encodeURIComponent(operationId)}/retry`, undefined, signal),
        parseDeletionOperation,
        'Creator Vault deletion retry',
      );
    },

    async exportDocument(signal) {
      return jsonResponse(
        await request(`${endpoint}/export`, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
          method: 'GET',
          signal,
        }),
        parseExport,
        'Creator Vault export',
      );
    },
  };
}

export const creatorVaultApi = createCreatorVaultApi();
