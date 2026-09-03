/**
 * Canonical JSON for a Legal instrument bindings document.
 *
 * The Legal release bindings contract defines this serialization, and both the
 * release gate that verifies a fetched document and the extension build that
 * records its digest read it from here so there is one implementation.
 *
 * The module depends on nothing outside the Node standard library, so the gate
 * runs before any dependency install.
 */

/** Escape a JSON string so the result carries ASCII code points only. */
function asciiJsonString(text) {
  return JSON.stringify(text).replace(
    /[\u007f-\uffff]/g,
    (unit) => `\\u${unit.charCodeAt(0).toString(16).padStart(4, '0')}`,
  );
}

/**
 * Serialize a Legal instrument bindings document the way the Legal release
 * bindings contract defines it: object member names sorted lexicographically,
 * array order preserved, compact separators, non-ASCII code points escaped,
 * UTF-8, and no insignificant whitespace, byte order mark or trailing newline.
 *
 * This is not stableJson. stableJson keeps authored member order and
 * pretty-prints with a trailing newline, and it stays the serialization for the
 * packaged signing rule and for build metadata. The two must not be swapped.
 *
 * A document carrying a duplicate member name or a non-standard numeric cannot
 * round-trip through this function, so a caller that compares the result with
 * the bytes it parsed rejects both.
 */
export function canonicalLegalBindingsJson(value) {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new Error(`canonical JSON has no representation for ${value}`);
    }
    return JSON.stringify(value);
  }
  if (typeof value === 'string') return asciiJsonString(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalLegalBindingsJson(item)).join(',')}]`;
  }
  if (typeof value === 'object') {
    const members = Object.keys(value)
      .sort()
      .map((name) => `${asciiJsonString(name)}:${canonicalLegalBindingsJson(value[name])}`);
    return `{${members.join(',')}}`;
  }
  throw new Error(`canonical JSON has no representation for ${typeof value}`);
}
