document.addEventListener('htmx:configRequest', (event) => {
  const token = document.querySelector('meta[name="csrf-token"]').content;
  event.detail.headers['X-CSRF-Token'] = token;
});
