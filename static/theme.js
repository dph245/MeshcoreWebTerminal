// Load before the stylesheet so the saved theme is applied on the first paint.
(() => {
  const storageKey = 'meshcore-theme';
  const themes = {'dark-green': '#101614', 'dark-blue': '#0d1523', light: '#f3f6fa'};
  function applyTheme(value) {
    const theme = Object.hasOwn(themes, value) ? value : 'dark-green';
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = themes[theme];
    return theme;
  }
  let savedTheme;
  try { savedTheme = localStorage.getItem(storageKey); } catch { /* Storage may be blocked. */ }
  applyTheme(savedTheme);
  document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('theme-select');
    select.value = document.documentElement.dataset.theme;
    select.addEventListener('change', () => {
      const theme = applyTheme(select.value);
      try { localStorage.setItem(storageKey, theme); } catch { /* Switching still works without storage. */ }
    });
  });
})();
