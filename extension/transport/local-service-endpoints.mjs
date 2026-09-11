export const LOCAL_SERVICE_ORIGIN = 'https://bridge.localhost:17871';
export const LOCAL_SERVICE_PATTERN = `${LOCAL_SERVICE_ORIGIN}/*`;
export const LOCAL_SERVICE_WS = 'wss://bridge.localhost:17871/ws/agent';
export const LOCAL_SERVICE_CONFIG = `${LOCAL_SERVICE_ORIGIN}/api/v1/agent/config`;
export const LOCAL_SERVICE_STORAGE_UNSEAL = `${LOCAL_SERVICE_ORIGIN}/api/v1/agent/storage/unseal`;
export const LOCAL_SERVICE_STORAGE_ROTATE = `${LOCAL_SERVICE_ORIGIN}/api/v1/agent/storage/rotate`;
export const LOCAL_SERVICE_HEALTH = `${LOCAL_SERVICE_ORIGIN}/health`;

export function assertLocalServiceUrl(value) {
  const url = new URL(value);
  if (
    url.origin !== LOCAL_SERVICE_ORIGIN
    || url.username !== ''
    || url.password !== ''
  ) throw new Error('invalid_local_service_endpoint');
  return url;
}
