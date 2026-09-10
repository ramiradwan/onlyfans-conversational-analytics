// Synthetic fixture: capture directly imports a transport implementation
import { createDurableOutbox } from '../transport/durable-outbox.mjs';

export function observeSomething(event) {
  return createDurableOutbox(event);
}
