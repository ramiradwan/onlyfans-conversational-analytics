const clone = (value) => structuredClone(value);

function compareKeys(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

class FakeDomStringList {
  constructor(stores) {
    this.stores = stores;
  }

  contains(name) {
    return this.stores.has(name);
  }

  [Symbol.iterator]() {
    return this.stores.keys();
  }
}

class UpgradeObjectStore {
  constructor(schema) {
    this.schema = schema;
  }

  createIndex(name, keyPath) {
    if (this.schema.indexes.has(name)) throw new Error(`Index ${name} already exists`);
    this.schema.indexes.set(name, keyPath);
    return { name, keyPath };
  }
}

class FakeObjectStore {
  constructor(transaction, name) {
    this.transaction = transaction;
    this.name = name;
  }

  get schema() {
    return this.transaction.stores.get(this.name);
  }

  get(key) {
    return this.transaction.request(() => {
      const value = this.schema.records.get(key);
      return value === undefined ? undefined : clone(value);
    });
  }

  getAll() {
    return this.transaction.request(() => [...this.schema.records]
      .sort(([left], [right]) => compareKeys(left, right))
      .map(([, value]) => clone(value)));
  }

  getAllKeys() {
    return this.transaction.request(() => [...this.schema.records.keys()]
      .sort(compareKeys)
      .map(clone));
  }

  put(value, suppliedKey = undefined) {
    return this.transaction.request(() => {
      this.transaction.requireWritable();
      const key = suppliedKey ?? value?.[this.schema.keyPath];
      if (key === undefined) throw new Error(`A key is required for ${this.name}`);
      this.schema.records.set(key, clone(value));
      return clone(key);
    });
  }

  delete(key) {
    return this.transaction.request(() => {
      this.transaction.requireWritable();
      this.schema.records.delete(key);
      return undefined;
    });
  }

  clear() {
    return this.transaction.request(() => {
      this.transaction.requireWritable();
      this.schema.records.clear();
      return undefined;
    });
  }

  index(name) {
    const keyPath = this.schema.indexes.get(name);
    if (keyPath === undefined) throw new Error(`Unknown index ${name}`);
    return Object.freeze({
      getAllKeys: (key) => this.transaction.request(() => [...this.schema.records]
        .filter(([, value]) => value?.[keyPath] === key)
        .map(([primaryKey]) => clone(primaryKey))
        .sort(compareKeys)),
    });
  }
}

class FakeTransaction {
  constructor(databaseState, storeNames, mode) {
    this.databaseState = databaseState;
    this.storeNames = [...new Set(storeNames)];
    this.mode = mode;
    this.active = true;
    this.pending = 0;
    this.generation = 0;
    this.error = null;
    this.oncomplete = null;
    this.onabort = null;
    this.onerror = null;
    this.stores = new Map(this.storeNames.map((name) => {
      const source = databaseState.stores.get(name);
      if (!source) throw new Error(`Unknown object store ${name}`);
      return [name, {
        keyPath: source.keyPath,
        indexes: new Map(source.indexes),
        records: new Map([...source.records].map(([key, value]) => [key, clone(value)])),
      }];
    }));
    this.scheduleCompletionCheck();
  }

  objectStore(name) {
    if (!this.active) throw new Error('IndexedDB transaction is inactive');
    if (!this.stores.has(name)) throw new Error(`Object store ${name} is outside this transaction`);
    return new FakeObjectStore(this, name);
  }

  requireWritable() {
    if (this.mode !== 'readwrite') throw new Error('IndexedDB transaction is readonly');
  }

  request(operation) {
    if (!this.active) throw new Error('IndexedDB transaction is inactive');
    this.pending += 1;
    this.generation += 1;
    const request = {
      result: undefined,
      error: null,
      onsuccess: null,
      onerror: null,
    };
    queueMicrotask(() => {
      if (!this.active) return;
      try {
        request.result = operation();
        request.onsuccess?.({ target: request });
      } catch (error) {
        request.error = error;
        this.error = error;
        request.onerror?.({ target: request });
        this.onerror?.({ target: this });
        this.abort(error);
      } finally {
        this.pending -= 1;
        this.scheduleCompletionCheck();
      }
    });
    return request;
  }

  abort(error = new Error('IndexedDB transaction was aborted')) {
    if (!this.active) return;
    this.active = false;
    this.error = error;
    queueMicrotask(() => this.onabort?.({ target: this }));
  }

  scheduleCompletionCheck() {
    const generation = ++this.generation;
    setImmediate(() => {
      if (!this.active || this.pending !== 0 || generation !== this.generation) return;
      if (this.mode === 'readwrite') {
        for (const [name, store] of this.stores) {
          const target = this.databaseState.stores.get(name);
          target.records = new Map(
            [...store.records].map(([key, value]) => [key, clone(value)]),
          );
        }
      }
      this.active = false;
      this.oncomplete?.({ target: this });
    });
  }
}

class FakeDatabase {
  constructor(state) {
    this.state = state;
    this.onversionchange = null;
  }

  get objectStoreNames() {
    return new FakeDomStringList(this.state.stores);
  }

  createObjectStore(name, options = {}) {
    if (this.state.stores.has(name)) throw new Error(`Object store ${name} already exists`);
    const schema = {
      keyPath: options.keyPath ?? null,
      indexes: new Map(),
      records: new Map(),
    };
    this.state.stores.set(name, schema);
    return new UpgradeObjectStore(schema);
  }

  transaction(storeNames, mode) {
    return new FakeTransaction(this.state, storeNames, mode);
  }

  close() {}
}

/** Minimal deterministic IndexedDB implementation for storage and worker-restart tests. */
export class FakeIndexedDb {
  constructor() {
    this.databases = new Map();
  }

  open(name, version) {
    const request = {
      result: undefined,
      error: null,
      onupgradeneeded: null,
      onblocked: null,
      onsuccess: null,
      onerror: null,
    };
    queueMicrotask(() => {
      let state = this.databases.get(name);
      if (!state) {
        state = { version: 0, stores: new Map() };
        this.databases.set(name, state);
      }
      if (version < state.version) {
        request.error = new Error(`Database ${name} is newer than requested version ${version}`);
        request.onerror?.({ target: request });
        return;
      }
      const oldVersion = state.version;
      const database = new FakeDatabase(state);
      request.result = database;
      if (version > oldVersion) {
        state.version = version;
        request.onupgradeneeded?.({
          oldVersion,
          newVersion: version,
          target: request,
        });
      }
      request.onsuccess?.({ target: request });
    });
    return request;
  }
}
