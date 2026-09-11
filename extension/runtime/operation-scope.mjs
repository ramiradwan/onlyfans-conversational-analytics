export class OperationScope {
  #controller = new AbortController();
  #open = false;
  #pending = new Set();

  get isOpen() {
    return this.#open;
  }

  reopen() {
    if (this.#pending.size !== 0) {
      throw new Error('Cannot reopen operation scope before pending work drains');
    }
    this.#controller = new AbortController();
    this.#open = true;
  }

  close(code = 'operation_cancelled') {
    this.#open = false;
    if (!this.#controller.signal.aborted) {
      const error = new Error(code);
      error.code = code;
      this.#controller.abort(error);
    }
  }

  run(work) {
    if (typeof work !== 'function') throw new TypeError('Operation scope work is required');
    if (!this.#open) {
      const error = new Error('scope_closed');
      error.code = 'scope_closed';
      return Promise.reject(error);
    }

    const controller = this.#controller;
    const lease = Object.freeze({
      signal: controller.signal,
      assertCurrent: () => {
        controller.signal.throwIfAborted();
        if (!this.#open || controller !== this.#controller) {
          const error = new Error('stale_operation');
          error.code = 'stale_operation';
          throw error;
        }
      },
    });

    const operation = Promise.resolve().then(async () => {
      lease.assertCurrent();
      const result = await work(lease);
      lease.assertCurrent();
      return result;
    });
    this.#pending.add(operation);
    const remove = () => this.#pending.delete(operation);
    void operation.then(remove, remove);
    return operation;
  }

  async drain() {
    await Promise.allSettled([...this.#pending]);
  }
}

export class SerialExecutor {
  #tail = Promise.resolve();

  run(work) {
    if (typeof work !== 'function') throw new TypeError('Serial executor work is required');
    const operation = this.#tail.then(work);
    this.#tail = operation.catch(() => undefined);
    return operation;
  }
}
