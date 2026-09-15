import { customerReleaseConfig, validateCustomerReleaseConfig } from '../runtime/customer-release-config.mjs';

validateCustomerReleaseConfig(customerReleaseConfig, { requireDesktopDownload: true });
console.log('Customer release configuration is ready for Chrome packaging.');
