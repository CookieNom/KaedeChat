try {
  let theme = localStorage.getItem('kaede.theme') || 'system';
  if (window.__TAURI__?.core?.invoke) {
    try {
      const cached = JSON.parse(localStorage.getItem('kaede.desktop-theme-cache') || 'null');
      if (
        cached?.id === localStorage.getItem('kaede.desktop-theme') &&
        typeof cached.css === 'string' &&
        ['system', 'light', 'dark'].includes(cached.base)
      ) {
        theme = cached.base;
        const style = document.createElement('style');
        style.id = 'kaede-desktop-theme';
        style.textContent = cached.css;
        document.head.append(style);
      }
    } catch {
      // A damaged desktop theme cache must not prevent the default appearance.
    }
  }
  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);

  document.documentElement.dataset.theme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark ? '#181715' : '#f3efe8');

  const locale = localStorage.getItem('kaede.locale');
  if (locale && /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(locale)) {
    document.documentElement.lang = locale;
  }
} catch {
  // CSS and HTML defaults remain usable when storage is unavailable.
}
