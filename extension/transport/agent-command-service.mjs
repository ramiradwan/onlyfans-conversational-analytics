const clone = (value) => JSON.parse(JSON.stringify(value));

const VALID_OUTCOMES = new Set(['accepted', 'succeeded', 'failed']);

export class UnsupportedCommandExecutor {
  async execute(_action) {
    return {
      status: 'failed',
      output: null,
      error: {
        code: 'execution_error',
        detail: 'Command action is unsupported by the default executor',
        retryable: false,
      },
    };
  }
}

export class AgentCommandService {
  constructor(options = {}) {
    this.persistence = options.persistence ?? {};
    this.executor = options.executor ?? new UnsupportedCommandExecutor();
    this.appliedConfig = options.appliedConfig ?? (() => null);
    this.now = options.now ?? (() => Date.now());
    this.idFactory = options.idFactory ?? (() => crypto.randomUUID());
    this.records = new Map();
    this.pendingCommandIds = new Set();
    this.inFlight = new Map();
    this.initialization = null;
  }

  async initialize() {
    if (this.initialization !== null) return this.initialization;
    this.initialization = this.#load();
    return this.initialization;
  }

  async execute(payload, session, correlationId = null) {
    await this.initialize();
    const active = this.inFlight.get(payload.command_id);
    if (active) return clone(await active);
    const existing = this.records.get(payload.command_id);
    if (existing) return clone(existing.result);

    const execution = this.#executeOnce(payload, session, correlationId);
    this.inFlight.set(payload.command_id, execution);
    try {
      return clone(await execution);
    } finally {
      this.inFlight.delete(payload.command_id);
    }
  }

  async acknowledge(payload) {
    await this.initialize();
    const record = this.records.get(payload.command_id);
    if (
      !record
      || record.creator_account_id !== payload.creator_account_id
      || record.result.result_id !== payload.result_id
    ) return false;
    if (!this.pendingCommandIds.delete(payload.command_id)) return true;
    await this.#persist();
    return true;
  }

  async pendingResults(creatorAccountId = null) {
    await this.initialize();
    return [...this.pendingCommandIds]
      .map((commandId) => this.records.get(commandId))
      .filter(Boolean)
      .filter((record) => creatorAccountId === null || record.creator_account_id === creatorAccountId)
      .map((record) => clone(record));
  }

  async storedResult(commandId) {
    await this.initialize();
    const record = this.records.get(commandId);
    return record ? clone(record.result) : null;
  }

  async #load() {
    const saved = await this.persistence.loadCommandState?.();
    if (
      !saved
      || saved.version !== 1
      || saved.records === null
      || typeof saved.records !== 'object'
    ) return;
    for (const [commandId, record] of Object.entries(saved.records)) {
      if (record?.result?.command_id === commandId) {
        this.records.set(commandId, clone(record));
      }
    }
    const pending = Array.isArray(saved.pending_command_ids)
      ? saved.pending_command_ids
      : [];
    for (const commandId of pending) {
      if (this.records.has(commandId)) this.pendingCommandIds.add(commandId);
    }
  }

  async #executeOnce(payload, session, correlationId) {
    let outcome;
    if (payload.creator_account_id !== session.creator_account_id) {
      outcome = this.#refused('Command account does not match the active Agent binding');
    } else if (
      payload.connection_id !== session.connection_id
      || payload.fencing_token !== session.fencing_token
    ) {
      outcome = this.#refused('Command fencing token is not current');
    } else {
      const policy = this.appliedConfig()?.command_policy ?? null;
      if (!policy?.allowed_actions?.includes(payload.action.type)) {
        outcome = this.#refused(
          'Command action is not allowed by the applied configuration',
        );
      } else if (
        payload.action.type === 'message.send'
        && payload.action.text.length > policy.max_text_length
      ) {
        outcome = this.#refused(
          'Command action exceeds the applied configuration limits',
        );
      } else if (
        policy.require_idempotency
        && payload.idempotency_policy !== 'deduplicate'
      ) {
        outcome = this.#refused(
          'Command idempotency policy is not allowed by the applied configuration',
        );
      } else if (Date.parse(payload.deadline) <= this.now()) {
        outcome = {
          status: 'failed',
          output: null,
          error: {
            code: 'deadline_exceeded',
            detail: 'Command deadline has passed',
            retryable: false,
          },
        };
      } else {
        try {
          outcome = this.#normalizeOutcome(
            await this.executor.execute(clone(payload.action)),
          );
        } catch (_error) {
          outcome = {
            status: 'failed',
            output: null,
            error: {
              code: 'execution_error',
              detail: 'Command executor failed',
              retryable: false,
            },
          };
        }
      }
    }

    const result = {
      command_id: payload.command_id,
      result_id: this.idFactory(),
      status: outcome.status,
      completed_at: new Date(this.now()).toISOString(),
      output: outcome.output,
      error: outcome.error,
    };
    this.records.set(payload.command_id, {
      creator_account_id: payload.creator_account_id,
      result: clone(result),
      correlation_id: correlationId,
    });
    this.pendingCommandIds.add(payload.command_id);
    await this.#persist();
    return result;
  }

  #refused(detail) {
    return {
      status: 'failed',
      output: null,
      error: {
        code: 'rejected',
        detail,
        retryable: false,
      },
    };
  }

  #normalizeOutcome(outcome) {
    if (!outcome || !VALID_OUTCOMES.has(outcome.status)) {
      return {
        status: 'failed',
        output: null,
        error: {
          code: 'execution_error',
          detail: 'Command executor returned an invalid outcome',
          retryable: false,
        },
      };
    }
    return {
      status: outcome.status,
      output: outcome.output ?? null,
      error: outcome.error ?? null,
    };
  }

  async #persist() {
    if (!this.persistence.saveCommandState) return;
    await this.persistence.saveCommandState({
      version: 1,
      records: Object.fromEntries(
        [...this.records.entries()].map(([commandId, record]) => [
          commandId,
          clone(record),
        ]),
      ),
      pending_command_ids: [...this.pendingCommandIds],
    });
  }
}
