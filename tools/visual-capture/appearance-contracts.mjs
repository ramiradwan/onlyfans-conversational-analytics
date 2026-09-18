/** Browser checks for local font loading, numeric hierarchy and text containment. */
export async function assertNumericTypography(page, viewport) {
  await page.evaluate((width) => {
    const metrics = [...document.querySelectorAll('.MuiTypography-kpi, .MuiTypography-metric, .MuiTypography-insight, [data-visual="reply-metric-value"]')];
    if (metrics.length === 0) return;
    const loaded = [...document.fonts].some((face) => face.family.includes('Space Grotesk Variable') && face.status === 'loaded');
    if (!loaded) throw new Error('The bundled numeric font did not load');
    for (const value of metrics) {
      const family = getComputedStyle(value).fontFamily.split(',')[0].replaceAll('"', '').trim();
      if (family !== 'Space Grotesk Variable') throw new Error('Numeric role lost its display font');
      if (value.clientWidth && value.scrollWidth > value.clientWidth + 1) throw new Error('Numeric value is clipped');
    }
    for (const heading of document.querySelectorAll('h1,h2,h3')) {
      if (!heading.matches('main[data-journey-state="desktop.passkey_sign_in"] h1.MuiTypography-passkeyTitle') && getComputedStyle(heading).fontFamily.includes('Space Grotesk')) throw new Error('Display font leaked into a heading');
    }
    const total = document.querySelector('[data-visual="conversation-total"]');
    const messages = document.querySelector('[data-visual="message-total"]');
    if (!total || !messages) return;
    if (parseFloat(getComputedStyle(total).fontSize) < parseFloat(getComputedStyle(messages).fontSize) * 1.5) {
      throw new Error('Dashboard lost its dominant total');
    }
    if (width < 900) return;
    const baseline = (element) => {
      const marker = document.createElement('span');
      marker.style.cssText = 'display:inline-block;width:0;height:0;padding:0;margin:0';
      element.appendChild(marker);
      const y = marker.getBoundingClientRect().top;
      marker.remove();
      return y;
    };
    if (Math.abs(baseline(total) - baseline(messages)) > 1) throw new Error('Dashboard number baselines diverged');
  }, viewport.width);
}
