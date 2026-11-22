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
  // Every form or pager on the ticket returns to the same ticket: keep the reader where they were.
  const ticket = location.pathname.match(/^\/repairs\/\d+/)?.[0];
  if (!ticket) return;
  const key = 'repairlib:ticket-view';
  const activity = document.querySelector('section[aria-labelledby="activity-heading"] > details');
  try {
    const saved = JSON.parse(sessionStorage.getItem(key));
    sessionStorage.removeItem(key);
    if (saved && saved.ticket === ticket) {
      if (activity) activity.open = saved.activity;
      window.scrollTo(0, saved.scroll);
    }
  } catch {
    // The native form still works with storage disabled.
  }
  function remember() {
    try {
      sessionStorage.setItem(key, JSON.stringify({ticket, scroll: window.scrollY, activity: !!activity?.open}));
    } catch {
      // Do not interrupt the navigation.
    }
  }
  document.addEventListener('submit', event => { if (!event.defaultPrevented) remember(); });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (link && link.pathname === ticket && !link.hash) remember();
  });
})();
