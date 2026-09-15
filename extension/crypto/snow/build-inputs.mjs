import path from 'node:path';

export const compareCodePoints = (left, right) => left < right ? -1 : left > right ? 1 : 0;
export const normalizeNotice = (value) => value.replace(/\r\n?/gu, '\n').trim();

// rustc remaps text, not path components. Remapping only a parent directory
// retains Windows separators in file!()/panic locations. Map the complete
// source filename so the replacement has no host-dependent suffix.
export function sourcePathMappings(packageRoot, packageName, packageVersion, files, { local = false } = {}) {
  const windows = /^[A-Za-z]:[\\/]/u.test(packageRoot);
  const paths = windows ? path.win32 : path.posix;
  const mappings = new Map();
  for (const file of files) {
    const relative = paths.relative(packageRoot, file);
    if (!relative || relative.startsWith('..') || paths.isAbsolute(relative)) throw new Error('rust_source_path_invalid');
    const canonical = `/crate/${packageName}-${packageVersion}/${relative.replaceAll('\\', '/')}`;
    const sources = new Set([file, file.replaceAll('\\', '/')]);
    if (local) {
      sources.add(relative);
      sources.add(relative.replaceAll('\\', '/'));
      sources.add(relative.replaceAll('/', '\\'));
    }
    for (const source of sources) {
      if (/[\r\n=]/u.test(source) || /[\r\n=]/u.test(canonical)) throw new Error('rust_source_path_invalid');
      mappings.set(source, canonical);
    }
  }
  return [...mappings].sort(([left], [right]) => compareCodePoints(left, right));
}
