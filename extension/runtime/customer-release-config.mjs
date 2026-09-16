const SOURCE = Object.freeze({
  schema: 'ofca-customer-release/v1',
  desktop_app_download_url: '',
});

function exact(value, fields) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === fields.length
    && fields.every((field) => Object.hasOwn(value, field));
}

export function validateCustomerReleaseConfig(value, { requireDesktopDownload = false } = {}) {
  if (!exact(value, ['schema', 'desktop_app_download_url'])
    || value.schema !== 'ofca-customer-release/v1'
    || typeof value.desktop_app_download_url !== 'string') {
    throw new Error('invalid_customer_release_config');
  }
  if (value.desktop_app_download_url === '') {
    if (requireDesktopDownload) throw new Error('desktop_app_download_url_required');
    return Object.freeze({ ...value });
  }
  const url = new URL(value.desktop_app_download_url);
  if (url.protocol !== 'https:'
    || url.username !== ''
    || url.password !== ''
    || url.hostname.endsWith('.invalid')) {
    throw new Error('invalid_desktop_app_download_url');
  }
  return Object.freeze({ ...value, desktop_app_download_url: url.href });
}

export const customerReleaseConfig = validateCustomerReleaseConfig(SOURCE);
