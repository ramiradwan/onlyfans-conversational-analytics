/** Reference reducer for authenticated local adapters; never an authorization check. */
export function createOnboardingProjection(journeyId, { operationLimit = 256 } = {}) {
  const sources = new Map();
  const operations = new Map();
  const owners = new Set(['brain', 'extension']);
  const sameScope = (a, b) => a.account_generation === b.account_generation
    && a.consent_generation === b.consent_generation;

  function channel(source, epoch) {
    if (!owners.has(source)) throw new TypeError('Local source required');
    // Adapters call this only after authenticating a new channel, never on an event's say-so.
    sources.set(source, { epoch, revision: -1, certain: false, snapshot: null });
    for (const operation of operations.values()) {
      if (operation.owner === source && operation.status === 'pending') operation.status = 'unknown';
    }
  }

  function receive(source, message) {
    const current = sources.get(source);
    if (!current || message.profile !== 'local-onboarding-state.v1'
      || message.source !== source || message.journey_id !== journeyId
      || message.epoch !== current.epoch || message.revision <= current.revision) return 'ignored';
    if (message.kind === 'event' && (!current.certain || message.revision !== current.revision + 1)) {
      current.certain = false;
      return 'snapshot_required';
    }
    if (current.snapshot && (message.account_generation < current.snapshot.account_generation
      || message.consent_generation < current.snapshot.consent_generation)) return 'ignored';
    current.snapshot = structuredClone(message);
    current.revision = message.revision;
    current.certain = true;
    for (const operation of operations.values()) {
      if (operation.owner === source && operation.status === 'pending' && !sameScope(operation, message)) {
        operation.status = 'unknown';
      }
    }
    return 'accepted';
  }

  function begin(command) {
    const current = sources.get(command.owner);
    if (command.profile !== 'local-onboarding-command.v1' || command.journey_id !== journeyId
      || !current?.certain || !sameScope(command, current.snapshot)) return false;
    const previous = operations.get(command.operation_id);
    if (previous) return previous.owner === command.owner && previous.action === command.action
      && sameScope(previous, command);
    // Do not discard unresolved commands to make room for another mutation.
    // A bounded page projection can stop accepting new work until reconciled.
    if (operations.size >= operationLimit) return false;
    operations.set(command.operation_id, { ...structuredClone(command), command_epoch: current.epoch, status: 'pending' });
    return true;
  }

  function settle(source, result) {
    const current = sources.get(source);
    const operation = operations.get(result.operation_id);
    const versionTwo = result.profile === 'local-onboarding-result.v2';
    const requestedScope = versionTwo ? {
      account_generation: result.command_account_generation,
      consent_generation: result.command_consent_generation,
    } : result;
    if ((!versionTwo && result.profile !== 'local-onboarding-result.v1') || result.journey_id !== journeyId
      || result.source !== source || !current?.certain || result.epoch !== current.epoch
      || !operation || operation.owner !== source || !sameScope(operation, requestedScope)
      || !sameScope(current.snapshot, result)
      || (versionTwo ? result.command_epoch !== operation.command_epoch : result.epoch !== operation.command_epoch)) return false;
    // Counters are comparable only within the epoch that admitted the command.
    // Across restart, the authenticated owner reconciles its durable receipt
    // against current account/consent evidence before publishing a result.
    if (operation.command_epoch === current.epoch
      && (operation.account_generation !== current.snapshot.account_generation
        || result.consent_generation < operation.consent_generation)) return false;
    // An ack cannot race ahead of the corresponding committed state.
    if (result.revision > current.revision) return false;
    if (operation.result_epoch === result.epoch && result.revision <= operation.result_revision) return false;
    if (operation.status === 'confirmed' || operation.status === 'rejected') return false;
    operation.status = result.status;
    operation.result_revision = result.revision;
    operation.result_epoch = result.epoch;
    return true;
  }

  return {
    channel, receive, begin, settle,
    unconfirmed(operationId) {
      const operation = operations.get(operationId);
      if (operation?.status === 'pending') operation.status = 'unknown';
    },
    disconnect(source) {
      const current = sources.get(source);
      if (current) current.certain = false;
      for (const operation of operations.values()) {
        if (operation.owner === source && operation.status === 'pending') operation.status = 'unknown';
      }
    },
    read() {
      return structuredClone({ sources: Object.fromEntries(sources), operations: Object.fromEntries(operations) });
    },
  };
}
