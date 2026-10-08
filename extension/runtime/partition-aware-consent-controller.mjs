import { ConsentController } from './consent-controller.mjs';
export { ACTIVE_ACCOUNT_PARTITION_KEY } from './consent-controller.mjs';

/**
 * Retain the packaged entry's controller name. Partition transition policy is
 * shared in ConsentController so normal and packaged workers both preserve
 * first binding and fence actual account replacement or removal.
 */
export class PartitionAwareConsentController extends ConsentController {}
