(() => {
  let csrf = '';

  async function prepareServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map(registration => {
      const worker = registration.active || registration.waiting || registration.installing;
      if (!worker) return registration.unregister();
      const path = new URL(worker.scriptURL).pathname;
      return path === '/service-worker.js' ? registration.update() : registration.unregister();
    }));
    await navigator.serviceWorker.register('/service-worker.js', {
      scope: '/',
      updateViaCache: 'none',
    });
  }

  async function prepare() {
    const response = await fetch('/api/v1/auth/csrf', {credentials: 'same-origin'});
    if (!response.ok) throw new Error('Could not initialize secure sign-in.');
    csrf = (await response.json()).csrf_token;
  }

  function bindLoginForm() {
    const form = document.getElementById('login-form');
    if (!form || form.dataset.quantLoginBound === 'true') return false;
    form.dataset.quantLoginBound = 'true';
    const error = document.getElementById('login-error');
    const button = document.getElementById('login-button');

    form.addEventListener('submit', async event => {
      event.preventDefault();
      event.stopPropagation();
      error.textContent = '';
      button.disabled = true;
      try {
        if (!csrf) await prepare();
        const response = await fetch('/api/v1/auth/login', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body: JSON.stringify({
            username: document.getElementById('username').value,
            password: document.getElementById('password').value,
          }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || 'Sign-in failed.');
        window.location.replace('/');
      } catch (reason) {
        error.textContent = reason.message || 'Sign-in failed.';
        csrf = '';
        await prepare().catch(() => {});
      } finally {
        button.disabled = false;
      }
    });

    prepare().catch(reason => { error.textContent = reason.message; });
    return true;
  }

  prepareServiceWorker().catch(() => {});
  bindLoginForm();
})();
