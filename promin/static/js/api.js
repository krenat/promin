// Calling the server's API, the same way from every page.

// Resolves with the JSON of the answer (null if there is none). Rejects with an Error whose
// message can be shown to the user: the server's own explanation, or `fallback`; its
// `status` is the HTTP status of the answer.
// Someone who is no longer logged in is sent to the login page; the Error then says 'login'
// and should not be shown.
export function api(method, url, body, fallback) {
  var options = { method: method, headers: {} };
  if (body) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  return fetch(url, options).then(function (r) {
    if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
    if (r.ok) return r.status === 204 ? null : r.json();
    return r.json().catch(function () { return {}; }).then(function (data) {
      var error = new Error(typeof data.detail === 'string' ? data.detail : fallback || 'Помилка ' + r.status);
      error.status = r.status;
      throw error;
    });
  });
}

// What to tell the user about a failed call: a request that never reached the server has
// no explanation of its own.
export function failureText(error) {
  return error.message === 'Failed to fetch'
    ? 'Немає зв’язку із сервером. Спробуйте ще раз.' : error.message;
}
