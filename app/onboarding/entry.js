/* The landing is nonauthorizing. Consume the one-use entry after removing it from the URL. */
const fields = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, '', location.pathname);
const code = fields.get('code');
const browser = location.pathname === '/browser-tasks/entry';
const endpoint = browser ? '/api/v1/browser-tasks/entry/consume' : '/api/v1/desktop/entry/consume';
const status = document.getElementById('status');
if (fields.size !== 1 || !/^[A-Za-z0-9_-]{43}$/.test(code ?? '')) {
  status.textContent = 'This setup link is unavailable. Open a fresh link from the desktop app.';
} else {
  fetch(endpoint, { method: 'POST', credentials: 'same-origin', redirect: 'error',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
    .then(response => {
      if (!response.ok) throw Error('entry unavailable');
      return response.json();
    })
    .then(session => {
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(session.generation)) throw Error('entry unavailable');
      // The new document receives the generation from this exact consumption.
      // Session reads cannot discover a replacement generation from shared cookies.
      try { sessionStorage.setItem(browser ? 'browser-task-launch' : 'desktop-presentation', session.generation); }
      catch { /* A locator and one-shot presentation intent, not authority. */ }
      location.replace((browser ? '/browser-tasks' : '/onboarding') + '#presentation=' + session.generation);
    })
    .catch(() => { status.textContent = 'This setup link expired or was already used. Open a fresh link from the desktop app.'; });
}
