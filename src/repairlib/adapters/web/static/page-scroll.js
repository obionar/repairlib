(() => {
  // A confirmed pop-up supplies the fixed audit reason the domain requires.
  for (const form of document.querySelectorAll('form[data-confirm]')) {
    form.addEventListener('submit', event => {
      if (window.confirm(form.dataset.confirm)) form.elements.reason.value = form.dataset.reason;
      else event.preventDefault();
    });
  }
  // Choosing a picture uploads it at once; without JavaScript the upload button appears instead.
  for (const input of document.querySelectorAll('input[data-autosubmit]')) {
    input.addEventListener('change', () => { if (input.files.length) input.form.requestSubmit(); });
  }
  const statusForm = document.querySelector('#stage-form, #stage-review-form');
  const button = document.querySelector('#ticket-controls > button');
  if (statusForm && button) {
    const controls = Array.from(statusForm.elements).filter(element => element.tagName === 'SELECT');
    function updateButton() {
      button.disabled = !controls.some(select => !select.disabled && select.value !== '');
    }
    for (const select of controls) {
      const current = select.querySelector('option[value=""]');
      if (current) current.disabled = current.hidden = false;
      select.addEventListener('change', updateButton);
    }
    updateButton();
  }
  // A form or pager that returns to the same page keeps the reader where they were; pages
  // and redirects carry no #fragment. The page is the record path (/repairs/2, /inventory/5).
  const pageOf = path => path.match(/^\/[a-z-]+(?:\/\d+)?/)?.[0] || '/';
  const page = pageOf(location.pathname);
  const key = 'repairlib:scroll';
  const activity = document.querySelector('section[aria-labelledby="activity-heading"] > details');
  try {
    const saved = JSON.parse(sessionStorage.getItem(key));
    sessionStorage.removeItem(key);
    if (saved && saved.page === page) {
      if (activity) activity.open = saved.activity;
      window.scrollTo(0, saved.scroll);
    }
  } catch {
    // The native form still works with storage disabled.
  }
  function remember() {
    try {
      sessionStorage.setItem(key, JSON.stringify({page, scroll: window.scrollY, activity: !!activity?.open}));
    } catch {
      // Do not interrupt the navigation.
    }
  }
  document.addEventListener('submit', event => { if (!event.defaultPrevented) remember(); });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (link && link.origin === location.origin && pageOf(link.pathname) === page && !link.hash) remember();
  });
})();
