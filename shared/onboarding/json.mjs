export const ONBOARDING_JSON_BYTES = 4096;

/** Parse a bounded wire document without JSON.parse's duplicate-key overwrite. */
export function parseOnboardingJson(text, byteLimit = ONBOARDING_JSON_BYTES) {
  if (typeof text !== 'string' || new TextEncoder().encode(text).byteLength > byteLimit) {
    throw new TypeError('Invalid onboarding document');
  }
  const stack = [];
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '{') stack.push(new Set());
    else if (char === '[') stack.push(null);
    else if (char === '}' || char === ']') stack.pop();
    else if (char === '"') {
      const start = index;
      for (index += 1; index < text.length; index += 1) {
        if (text[index] === '\\') index += 1;
        else if (text[index] === '"') break;
      }
      let next = index + 1;
      while (/\s/u.test(text[next] ?? '') && next < text.length) next += 1;
      if (text[next] === ':' && stack.at(-1) instanceof Set) {
        const key = JSON.parse(text.slice(start, index + 1));
        if (stack.at(-1).has(key)) throw new TypeError('Duplicate onboarding field');
        stack.at(-1).add(key);
      }
    }
  }
  // The scan only rejects duplicates. JSON.parse still owns JSON grammar.
  return JSON.parse(text);
}
