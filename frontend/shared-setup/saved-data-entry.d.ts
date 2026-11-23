export const savedDataUrl: string;
export const expiredAccessTitle: string;
export const expiredAccessDescription: string;
export const savedDataLinkText: string;
export function savedDataEntry(signal?: AbortSignal, request?: typeof fetch): Promise<'authorization-expired' | 'available' | 'unavailable'>;
