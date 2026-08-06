try {
  const theme = localStorage.getItem('kaede.theme') || 'system';
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
