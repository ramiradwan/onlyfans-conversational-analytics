const exact = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
export const WORKSPACE_STATE_KEY = 'onboarding_workspace_state_v3';
const stableJson = value => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);

/** One worker owns one closed storage envelope. There is no legacy-key import. */
export function createWorkspaceStore({ storage, keys, validRecord, validIdentity }) {
  const empty = () => ({ version: 3, generation: 0, touched_at: null, record: null, identity: null });
  const valid = value => exact(value, ['version', 'generation', 'touched_at', 'record', 'identity'])
    && value.version === 3 && Number.isSafeInteger(value.generation) && value.generation > 0
    && (value.touched_at === null || Number.isSafeInteger(value.touched_at))
    && (value.record === null ? value.identity === null : validRecord(value.record) && validIdentity(value.identity, value.record));
  async function read() {
    const value = (await storage.get(WORKSPACE_STATE_KEY))[WORKSPACE_STATE_KEY];
    if (value !== undefined) {
      if (!valid(value)) throw Error('workspace_storage_invalid');
      return value;
    }
    return empty();
  }
  async function write(value) {
    const previous = await read();
    const next = { ...value, version: 3, generation: previous.generation + 1 };
    if (!valid(next)) throw Error('workspace_storage_invalid');
    await storage.set({ [WORKSPACE_STATE_KEY]: next });
    const stored = await read();
    if (stableJson(stored) !== stableJson(next)) throw Error('workspace_write_unconfirmed');
    return stored;
  }
  return {
    read, write,
    async get(key) {
      const value = await read();
      return { [key]: key === keys.record ? value.record ?? undefined
        : key === keys.identity ? value.identity ?? undefined : key === keys.activity ? value.touched_at ?? undefined : undefined };
    },
    async set(fields) {
      const value = await read();
      if (Object.hasOwn(fields, keys.record)) value.record = fields[keys.record];
      if (Object.hasOwn(fields, keys.identity)) value.identity = fields[keys.identity];
      else if (Object.hasOwn(fields, keys.record) && value.record) value.identity = {
        version: 1, journey_id: value.record.journey_id, ...value.record.draft_scope,
        account_digest: value.identity?.account_digest ?? null,
      };
      if (Object.hasOwn(fields, keys.activity)) value.touched_at = fields[keys.activity];
      await write(value);
    },
    async remove(names) {
      const value = await read();
      if (names.includes(keys.record) || names.includes(keys.identity)) { value.record = null; value.identity = null; }
      if (names.includes(keys.activity)) value.touched_at = null;
      await write(value);
    },
  };
}
