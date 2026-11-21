(() => {
  // Refresh a register's results while typing; the plain GET form still works without this.
  for (const form of document.querySelectorAll('form[data-live]')) {
    let timer;
    let pending;
    const refresh = () => {
      const url = new URL(form.action, location.href);
      for (const [name, value] of new FormData(form)) if (value) url.searchParams.set(name, value);
      pending?.abort();
      pending = new AbortController();
      fetch(url, {signal: pending.signal, credentials: 'same-origin'})
        .then(response => response.ok ? response.text() : Promise.reject(response.status))
        .then(html => {
          const fresh = new DOMParser().parseFromString(html, 'text/html').querySelector(form.dataset.live);
          const current = document.querySelector(form.dataset.live);
          if (!fresh || !current) return;
          current.replaceWith(fresh);
          history.replaceState(null, '', url);
        })
        .catch(() => {
          // Aborted or refused: the previous results stay; Enter still submits the form.
        });
    };
    form.addEventListener('input', event => {
      clearTimeout(timer);
      timer = setTimeout(refresh, event.target.type === 'search' ? 250 : 0);
    });
  }
})();
