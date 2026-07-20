import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { MessageBubble } from '../src/components/inbox/MessageBubble';
import type { MessageView } from '../src/protocol';
import { theme } from '../src/theme';

function message(text: string): MessageView {
  return {
    message_id: 'message-1',
    text,
    sent_at: '2026-07-20T12:00:00Z',
    direction: 'inbound',
    sentiment: 'neutral',
  };
}

function renderBubble(text: string) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <ul>
        <MessageBubble message={message(text)} />
      </ul>
    </ThemeProvider>,
  );
}

afterEach(() => cleanup());

describe('MessageBubble message HTML sanitization', () => {
  it('renders allowed formatting as real markup instead of literal tags', () => {
    const { container } = renderBubble(
      'Hello<br>World <a href="https://example.com/offer">the link</a>',
    );

    const article = container.querySelector('[role="article"]');
    expect(article?.querySelector('br')).toBeTruthy();

    const link = article?.querySelector('a');
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toBe('https://example.com/offer');
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');

    expect(article?.textContent).not.toContain('<br>');
    expect(article?.textContent).not.toContain('<a ');
  });

  it('neutralizes script tags, event-handler attributes, and javascript: links', () => {
    const { container } = renderBubble(
      '<script>window.__pwned = true;</script>' +
        '<img src="x" onerror="window.__pwned = true">' +
        '<a href="javascript:window.__pwned=true" onclick="window.__pwned=true">click me</a>',
    );

    const article = container.querySelector('[role="article"]');
    expect(article?.querySelector('script')).toBeNull();
    expect(article?.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toContain('onerror');
    expect(container.innerHTML).not.toContain('onclick');
    expect(container.innerHTML).not.toContain('javascript:');

    const link = article?.querySelector('a');
    expect(link?.getAttribute('href')).toBeNull();
    expect(link?.textContent).toBe('click me');
  });
});
