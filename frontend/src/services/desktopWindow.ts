/** Window presentation only; this cannot authorize a browser or setup operation. */
export async function focusDesktopWindow(): Promise<void> {
  const host = globalThis as typeof globalThis & {
    __TAURI__?: { core?: { invoke(command: string, args: { action: string }): Promise<unknown> } };
  };
  await host.__TAURI__?.core?.invoke('desktop_window', { action: 'focus' });
}
