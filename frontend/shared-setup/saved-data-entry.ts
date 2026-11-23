export const savedDataUrl = '/?view=saved-data';

export async function savedDataAvailable(signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch('/api/v1/saved-data/available', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
    });
    return response.ok && (await response.json()).available === true;
  } catch {
    return false;
  }
}
