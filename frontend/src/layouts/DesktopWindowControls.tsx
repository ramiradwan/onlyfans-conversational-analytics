import { createElement } from 'react';

/** The native host defines this element. It is inert in ordinary browsers. */
export function hasDesktopWindowControls() {
  return typeof customElements !== 'undefined' && Boolean(customElements.get('desktop-window-controls'));
}

export function DesktopWindowControls() {
  return hasDesktopWindowControls() ? createElement('desktop-window-controls') : null;
}
