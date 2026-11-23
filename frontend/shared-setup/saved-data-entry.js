export const savedDataUrl = '/?view=saved-data';
export const expiredAccessTitle = 'Account authorization has expired';
export const expiredAccessDescription = 'New capture and analysis are paused on this computer.';
export const savedDataLinkText = 'Open saved data with your passkey';

export async function savedDataEntry(signal, request = fetch) {
  try {
    const response = await request('/api/v1/saved-data/available', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
    });
    if (response.ok) {
      const result = await response.json();
      if (result?.available === true) return result.authorization_expired === true ? 'authorization-expired' : 'available';
    }
  } catch {
    // A failed local check cannot establish either ownership or expiry.
  }
  return 'unavailable';
}
