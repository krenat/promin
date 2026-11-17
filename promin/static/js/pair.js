(function () {
  var key = new URLSearchParams(location.search).get('t') || '';
  var button = document.getElementById('approve');
  var error = document.getElementById('error');

  button.addEventListener('click', function () {
    button.disabled = true;
    error.textContent = '';
    fetch('/api/pair/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: key })
    }).then(function (r) {
      if (r.status === 401) { location.reload(); return; }
      if (r.ok) {
        document.getElementById('ask').hidden = true;
        document.getElementById('done').hidden = false;
        return;
      }
      return r.json().then(function (body) {
        error.textContent = (body && typeof body.detail === 'string' && body.detail) || 'Не вдалося підключити проектор.';
        button.disabled = false;
      });
    }).catch(function () {
      error.textContent = 'Немає зв’язку із сервером. Спробуйте ще раз.';
      button.disabled = false;
    });
  });
})();
