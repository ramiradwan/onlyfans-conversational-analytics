import { expect, it, vi } from 'vitest';
import { exportSavedJson } from '../src/services/savedDataExport';

it('opens the native file picker first and writes the exact export before reporting success', async () => {
  const text = '{"messages":["OFF-04 synthetic item"]}';
  const chunks: Uint8Array[] = [];
  const closed = vi.fn();
  const destination = new WritableStream({ write: bytes => { chunks.push(bytes); }, close: closed });
  const open = vi.fn(async () => ({ createWritable: async () => destination }));
  const read = vi.fn(async () => new Response(text));
  vi.stubGlobal('showSaveFilePicker', open);
  try {
    await exportSavedJson('archive.json', read, new AbortController().signal);
    expect(open.mock.invocationCallOrder[0]).toBeLessThan(read.mock.invocationCallOrder[0]);
    expect(new TextDecoder().decode(chunks[0])).toBe(text);
    expect(closed).toHaveBeenCalledOnce();
  } finally { vi.unstubAllGlobals(); }
});

it('leaves archived data on the device when the picker is cancelled', async () => {
  const read = vi.fn();
  vi.stubGlobal('showSaveFilePicker', vi.fn().mockRejectedValue(new DOMException('Cancelled', 'AbortError')));
  try {
    await expect(exportSavedJson('archive.json', read, new AbortController().signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(read).not.toHaveBeenCalled();
  } finally { vi.unstubAllGlobals(); }
});
