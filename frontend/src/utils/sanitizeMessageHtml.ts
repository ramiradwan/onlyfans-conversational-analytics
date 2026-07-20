import DOMPurify from 'dompurify';

// Message bodies (OnlyFans platform content, relayed through Brain) carry inline
// HTML formatting — line breaks, links, bold/italic — but they are untrusted
// external input. Never render message text as HTML anywhere in the app without
// routing it through this allowlist first; a bare dangerouslySetInnerHTML of the
// raw text is an XSS vulnerability.
const ALLOWED_TAGS = ['br', 'a', 'b', 'strong', 'i', 'em', 'u', 's', 'p', 'span', 'ul', 'ol', 'li'];
const ALLOWED_ATTR = ['href', 'title'];
// Only http(s) and mailto links survive; javascript:, data:, and every other
// scheme are stripped from the href attribute entirely.
const ALLOWED_URI_REGEXP = /^(?:https?|mailto):/i;

// Every surviving link opens safely in a new tab, regardless of what attributes
// the sender's markup asked for.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.hasAttribute('href')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});

export function sanitizeMessageHtml(rawText: string): string {
  return DOMPurify.sanitize(rawText, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOWED_URI_REGEXP,
    // Belt-and-suspenders: these are already excluded by the allowlists above,
    // but forbidding them explicitly documents the intent and survives a future
    // config change that loosens ALLOWED_TAGS/ALLOWED_ATTR by mistake.
    FORBID_TAGS: ['script', 'style', 'iframe', 'img', 'svg', 'object', 'embed'],
    FORBID_ATTR: ['style'],
  });
}
