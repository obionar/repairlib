(() => {
  const statusForm = document.querySelector('#stage-form, #stage-review-form');
  const forms = [statusForm, document.querySelector('#manual-part-form'), document.querySelector('#fees-form')].filter(Boolean);
  if (!forms.length) return;
  const key = `repairlib:ticket-view:${new URL(forms[0].action).pathname.replace(/\/(stage|consume-part)$/, '')}`;
  const activity = document.querySelector('section[aria-labelledby="activity-heading"] > details');
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
  try {
    const saved = JSON.parse(sessionStorage.getItem(key));
    sessionStorage.removeItem(key);
    if (saved) {
      if (activity) activity.open = saved.activity;
      if (!location.hash) window.scrollTo(0, saved.scroll);
    }
  } catch {
    // The native form still works with storage disabled.
  }
  for (const form of forms) {
    form.addEventListener('submit', () => {
      try {
        sessionStorage.setItem(key, JSON.stringify({scroll: window.scrollY, activity: !!activity?.open}));
      } catch {
        // Do not interrupt the form submission.
      }
    });
  }
})();
