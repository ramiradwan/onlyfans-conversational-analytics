export const supportChannelLabels = Object.freeze({
  whatsapp: 'WhatsApp', telegram: 'Telegram', tiktok: 'TikTok',
});

const localSuffixes = new Set([
  'alt', 'arpa', 'corp', 'example', 'home', 'internal', 'intranet', 'invalid',
  'lan', 'local', 'localdomain', 'localhost', 'onion', 'private', 'test',
]);
const dnsLabel = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?';
const addressPattern = new RegExp(
  "https://((?:" + dnsLabel + "\\.)+" + dnsLabel + ")(?::([0-9]{1,5}))?(/(?:[a-z0-9._~!$&'()*+,;=:@/-]|%[0-9a-f]{2})*)?",
  'i',
);

function supportAddress(value) {
  if (typeof value !== 'string' || value.length > 1024) return null;
  const match = addressPattern.exec(value);
  if (!match || match[0] !== value) return null;
  const host = match[1].toLowerCase();
  const suffix = host.split('.').at(-1);
  if (host.length > 253 || !/^[a-z]/.test(suffix) || localSuffixes.has(suffix)) return null;
  const port = Number(match[2] ?? 443);
  if (port < 1 || port > 65535) return null;
  let unsafeEscape = false;
  const path = (match[3] ?? '/').replace(/%[0-9a-f]{2}/gi, escape => {
    const character = String.fromCharCode(Number.parseInt(escape.slice(1), 16));
    unsafeEscape ||= character < '!' || character > '~' || character === '\\';
    return /^[a-z0-9._~-]$/i.test(character) ? character : escape.toUpperCase();
  });
  if (unsafeEscape || path.split('/').some(segment => segment === '.' || segment === '..')) return null;
  return 'https://' + host + (port === 443 ? '' : ':' + port) + path;
}

export function projectSupportChannels(value) {
  const channels = {};
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const channel of Object.keys(supportChannelLabels)) {
      const address = Object.hasOwn(value, channel) ? supportAddress(value[channel]) : null;
      if (address) channels[channel] = address;
    }
  }
  return Object.freeze(channels);
}
