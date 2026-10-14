import { createOnboardingProjection } from './projection.mjs';

const OWNERS = new Set(['brain', 'extension']);

/**
 * Bounded presentation client for authenticated local transports. The adapter
 * establishes who it is connected to; a message's source field never does.
 * No timers poll state and no protected command is replayed after a lost reply.
 */
export function createOnboardingClient({ journeyId, validate, bufferLimit = 128, operationLimit = 256 }) {
  const projection = createOnboardingProjection(journeyId, { operationLimit });
  const channels = new Map();
  const listeners = new Set();
  let view = projection.read();
  const publish = () => {
    view = projection.read();
    for (const listener of listeners) listener();
  };
  const valid = (value, profile, source) => {
    try {
      return validate(value) === true && value.profile === profile
        && value.journey_id === journeyId && value.source === source;
    } catch { return false; }
  };

  function disconnect(source, channel) {
    if (channels.get(source) !== channel) return;
    channels.delete(source);
    channel.closed = true;
    channel.unsubscribe?.();
    channel.buffer.length = 0;
    channel.results.clear();
    projection.disconnect(source);
    publish();
  }

  function settleBuffered(source, channel) {
    for (const [id, result] of channel.results) {
      if (projection.settle(source, result)) channel.results.delete(id);
    }
  }

  function accept(source, channel, message) {
    if (valid(message, 'local-onboarding-state.v1', source)) {
      if (message.epoch !== channel.epoch) {
        // Epoch changes require a newly authenticated transport, not a claim
        // inside a frame on the old connection.
        disconnect(source, channel);
        channel.adapter.invalidate?.();
        return;
      }
      const outcome = projection.receive(source, message);
      settleBuffered(source, channel);
      publish();
      if (outcome === 'snapshot_required') void snapshot(source, channel);
    } else if (valid(message, 'local-onboarding-result.v1', source)
      || valid(message, 'local-onboarding-result.v2', source)) {
      if (message.epoch !== channel.epoch) return;
      const state = projection.read();
      if (!state.operations[message.operation_id]) return;
      if (!projection.settle(source, message)
        && message.revision > (state.sources[source]?.revision ?? -1)) {
        if (channel.results.size >= bufferLimit) {
          disconnect(source, channel);
          channel.adapter.invalidate?.();
          return;
        }
        channel.results.set(message.operation_id, message);
      }
      publish();
    }
  }

  async function snapshot(source, channel) {
    if (channel.reading || channel.closed) return;
    channel.reading = true;
    try {
      const message = await channel.adapter.readSnapshot();
      if (channel.closed || channels.get(source) !== channel) return;
      if (!valid(message, 'local-onboarding-state.v1', source) || message.kind !== 'snapshot') {
        throw new Error('Invalid onboarding snapshot');
      }
      if (channel.epoch === null) {
        channel.epoch = message.epoch;
        projection.channel(source, message.epoch);
      } else if (channel.epoch !== message.epoch) {
        throw new Error('Onboarding epoch changed');
      }
      projection.receive(source, message);
      const buffered = channel.buffer.splice(0);
      // Do not request another read while draining. A gap after this read stays
      // uncertain and ends the subscription; endless snapshot retries are not
      // a replacement for replay or an ordered transport.
      for (const event of buffered) {
        if (channel.closed) break;
        accept(source, channel, event);
      }
      settleBuffered(source, channel);
      if (!projection.read().sources[source]?.certain) throw new Error('Onboarding stream gap');
      publish();
      channel.reading = false;
      // Read receipts without replaying mutations: one lookup per operation on
      // an authenticated channel. A later reconnect can reconcile again.
      for (const operation of Object.values(projection.read().operations)) {
        if (operation.owner !== source || operation.status !== 'unknown'
          || channel.lookups.has(operation.operation_id) || !channel.adapter.readOperation) continue;
        channel.lookups.add(operation.operation_id);
        try { await channel.adapter.readOperation(operation.operation_id); } catch { /* Outcome stays unknown. */ }
      }
    } catch {
      disconnect(source, channel);
      channel.adapter.invalidate?.();
    } finally {
      channel.reading = false;
    }
  }

  return Object.freeze({
    getState: () => view,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    /** Subscribe first, then read. Call only with a source-authenticated adapter. */
    attach(source, adapter) {
      if (!OWNERS.has(source)) throw new TypeError('Local owner required');
      const previous = channels.get(source);
      if (previous) disconnect(source, previous);
      const channel = { adapter, epoch: null, reading: false, closed: false,
        unsubscribe: null, buffer: [], results: new Map(), lookups: new Set() };
      channels.set(source, channel);
      try { channel.unsubscribe = adapter.subscribe((message) => {
        if (channel.closed || channels.get(source) !== channel) return;
        if (channel.epoch === null || channel.reading) {
          if (channel.buffer.length >= bufferLimit) {
            disconnect(source, channel);
            adapter.invalidate?.();
            return;
          }
          if (valid(message, 'local-onboarding-state.v1', source)
            || valid(message, 'local-onboarding-result.v1', source)
            || valid(message, 'local-onboarding-result.v2', source)) channel.buffer.push(message);
        } else accept(source, channel, message);
      }, () => disconnect(source, channel)); }
      catch { disconnect(source, channel); adapter.invalidate?.(); return () => {}; }
      if (channel.closed) channel.unsubscribe?.();
      else void snapshot(source, channel);
      return () => disconnect(source, channel);
    },
    async command(command) {
      try {
        if (!validate(command) || command.profile !== 'local-onboarding-command.v1'
          || command.journey_id !== journeyId) return false;
      } catch { return false; }
      const channel = channels.get(command.owner);
      if (!channel || channel.closed) return false;
      // Even identical repeat intent must not resend an operation. The owner
      // ledger is reconciled by result/snapshot lookup, never by command retry.
      const previous = projection.read().operations[command.operation_id];
      if (previous) return projection.begin(command);
      if (!projection.begin(command)) return false;
      publish(); // Immediate pending feedback precedes network delivery.
      try { await channel.adapter.sendCommand(command, channel.epoch); }
      catch { projection.unconfirmed(command.operation_id); publish(); }
      return true;
    },
    disconnect(source) {
      const channel = channels.get(source);
      if (channel) disconnect(source, channel);
    },
  });
}
