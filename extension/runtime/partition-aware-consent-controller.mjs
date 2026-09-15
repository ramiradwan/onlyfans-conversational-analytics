import { ConsentController } from './consent-controller.mjs';

export const ACTIVE_ACCOUNT_PARTITION_KEY = 'active_account_partition_v5';

/**
 * The packaged companion publishes its active encrypted account partition into
 * chrome.storage.session while establishing a Full-mode binding. That first
 * publication is local initialization, not an external account change, and
 * must not recursively reconcile/tear down the channel that just produced it.
 *
 * Once a partition already exists, replacement or removal still delegates to
 * ConsentController so real account switches and binding loss remain
 * fail-closed.
 */
export class PartitionAwareConsentController extends ConsentController {
  constructor(options) {
    super(options);
    const baseStorageListener = this.storageListener;
    this.storageListener = (changes, areaName) => {
      if (
        areaName === 'session'
        && changes !== null
        && typeof changes === 'object'
        && Object.hasOwn(changes, ACTIVE_ACCOUNT_PARTITION_KEY)
      ) {
        const partition = changes[ACTIVE_ACCOUNT_PARTITION_KEY];
        if (
          partition !== null
          && typeof partition === 'object'
          && (
            partition.oldValue === undefined
            || partition.oldValue === partition.newValue
          )
        ) return;
      }
      return baseStorageListener(changes, areaName);
    };
  }
}
