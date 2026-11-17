// The users page: accounts and paired projectors, for administrators.

import { api } from './api.js';
import { el } from './dom.js';

var list = document.getElementById('list');
var message = document.getElementById('message');
var form = document.getElementById('add');
var roleNames = { admin: 'адміністратор', viewer: 'глядач' };

function fail(error) {
  if (error.message !== 'login') message.textContent = error.message;
}

function render(users) {
  list.textContent = '';
  users.forEach(function (user) {
    var card = el('div', 'user');
    var top = el('div', 'top');
    top.appendChild(el('span', 'name', user.username));
    top.appendChild(el('span', 'role', roleNames[user.role] || user.role));
    card.appendChild(top);
    if (user.builtin) {
      card.appendChild(el('div', 'note', 'Вбудований обліковий запис. Його пароль задається на сервері в PROMIN_TOKEN.'));
    } else {
      var actions = el('div', 'actions');
      var change = el('button', 'outline', 'Змінити пароль');
      change.type = 'button';
      change.addEventListener('click', function () {
        var password = prompt('Новий пароль для ' + user.username + ' (щонайменше 8 символів):');
        if (password == null) return;
        message.textContent = '';
        api('POST', '/api/users/' + encodeURIComponent(user.username) + '/password', { password: password })
          .then(function () { message.textContent = ''; })
          .catch(fail);
      });
      var remove = el('button', 'danger', 'Видалити');
      remove.type = 'button';
      remove.addEventListener('click', function () {
        if (!confirm('Видалити користувача ' + user.username + '? Він одразу втратить доступ.')) return;
        message.textContent = '';
        api('DELETE', '/api/users/' + encodeURIComponent(user.username)).then(load).catch(fail);
      });
      actions.appendChild(change);
      actions.appendChild(remove);
      card.appendChild(actions);
    }
    list.appendChild(card);
  });
}

function renderDevices(devices) {
  var box = document.getElementById('devices');
  box.textContent = '';
  if (!devices.length) {
    box.appendChild(el('div', 'empty', 'Жодного. Проектор підключається кодом зі сторінки /tv.'));
    return;
  }
  devices.forEach(function (device) {
    var card = el('div', 'user');
    var top = el('div', 'top');
    top.appendChild(el('span', 'name', device.name));
    card.appendChild(top);
    var when = new Date(device.created * 1000).toLocaleDateString('uk-UA');
    var seen = new Date(device.seen * 1000).toLocaleDateString('uk-UA');
    card.appendChild(el('div', 'note', 'Підключив ' + device.approved_by + ', ' + when));
    card.appendChild(el('div', 'note', 'Востаннє на зв\'язку ' + seen));
    var actions = el('div', 'actions');
    var remove = el('button', 'danger', 'Відключити');
    remove.type = 'button';
    remove.addEventListener('click', function () {
      if (!confirm('Відключити цей проектор? Щоб повернути, доведеться знову ввести код.')) return;
      message.textContent = '';
      api('DELETE', '/api/devices/' + encodeURIComponent(device.id)).then(load).catch(fail);
    });
    actions.appendChild(remove);
    card.appendChild(actions);
    box.appendChild(card);
  });
}

function load() {
  return Promise.all([api('GET', '/api/users'), api('GET', '/api/devices')])
    .then(function (results) {
      render(results[0].users);
      renderDevices(results[1].devices);
    })
    .catch(fail);
}

form.addEventListener('submit', function (e) {
  e.preventDefault();
  message.textContent = '';
  api('POST', '/api/users', {
    username: form.username.value.trim().toLowerCase(),
    password: form.password.value,
    role: form.role.value
  }).then(function () { form.reset(); return load(); }).catch(fail);
});

load();
