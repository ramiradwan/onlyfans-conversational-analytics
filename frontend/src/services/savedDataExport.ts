interface SavePicker {
  showSaveFilePicker?(options: {
    suggestedName: string;
    types: { description: string; accept: Record<string, string[]> }[];
  }): Promise<FileSystemFileHandle>;
}

/** Ask for the destination during the click, then request the authenticated export. */
export async function exportSavedJson(name: string, read: () => Promise<Response>, signal: AbortSignal) {
  const picker = window as Window & SavePicker;
  if (!picker.showSaveFilePicker) throw new Error('Save dialog unavailable');
  const file = await picker.showSaveFilePicker({
    suggestedName: name, types: [{ description: 'JSON file', accept: { 'application/json': ['.json'] } }],
  });
  signal.throwIfAborted();
  const response = await read();
  if (!response.body) throw new Error('Empty export');
  signal.throwIfAborted();
  const destination = await file.createWritable();
  await response.body.pipeTo(destination, { signal });
}
