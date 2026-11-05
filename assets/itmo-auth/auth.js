(() => {
  'use strict';
  const ticket = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  const form = document.getElementById('login');
  const status = document.getElementById('status');
  const submit = document.getElementById('submit');
  if (!/^[A-Za-z0-9_-]{43}$/.test(ticket)) {
    form.hidden = true;
    status.textContent = 'Откройте новую ссылку через кнопку «Подключить ИТМО» в боте.';
    return;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    status.textContent = 'Выполняется вход в ИТМО…';
    const username = document.getElementById('username');
    const password = document.getElementById('password');
    try {
      const request = fetch('/itmo/session', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticket, username: username.value, password: password.value})
      });
      password.value = '';
      const response = await request;
      const body = await response.json().catch(() => ({error: 'Сервис не ответил. Повторите через минуту.'}));
      if (!response.ok) throw new Error(body.error || 'Не удалось войти. Повторите позже.');
      form.hidden = true;
      status.textContent = 'Аккаунт ИТМО: ' + body.account;
      document.getElementById('success').hidden = false;
    } catch (error) {
      password.value = '';
      status.textContent = error.message || 'Нет соединения. Повторите позже.';
    } finally { submit.disabled = false; }
  });
})();
