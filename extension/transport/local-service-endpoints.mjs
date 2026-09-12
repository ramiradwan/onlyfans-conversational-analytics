export const LOCAL_SERVICE_ORIGIN = 'http://bridge.localhost:17871';
export const LOCAL_SERVICE_PATTERN = `${LOCAL_SERVICE_ORIGIN}/*`;
export const LOCAL_SERVICE_WS = 'ws://127.0.0.1:17871/ws/agent';
export const LOCAL_PAIRING_WS = 'ws://127.0.0.1:17871/ws/agent/pairing';
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
