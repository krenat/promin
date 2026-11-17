// The phone page. Starts here: who is logged in, then the connection and the list.
//
// The modules import each other in circles (the list needs the folders, the folders need
// the list). That is fine for functions, but a module must not use what it imports while
// it is loading, only inside its functions: the other module may not have run yet.

import { api } from '../api.js';
import { disk, refresh } from './library.js';
import { closePlayer, player } from './player.js';
import { connect } from './remote.js';
import { setSelecting } from './selection.js';
import { state } from './state.js';
import { uploadBtn } from './upload.js';

// ------------------------------------------------- pairing a projector

var pairBox = document.getElementById('pair');
document.getElementById('pair-url').textContent = location.host + '/tv';

function togglePair(open) {
  pairBox.hidden = open === undefined ? !pairBox.hidden : !open;
  document.getElementById('projector').setAttribute('aria-expanded', String(!pairBox.hidden));
  if (!pairBox.hidden) pairBox.scrollIntoView({ block: 'nearest' });
}

document.getElementById('projector').addEventListener('click', function () { togglePair(); });
document.getElementById('pair-link').addEventListener('click', function (e) {
  e.preventDefault();
  togglePair(true);
});

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  if (!player.hidden) closePlayer();
  else if (state.selecting) setSelecting(false);
});
document.addEventListener('click', function (e) {
  var menus = document.querySelectorAll('details.menu[open]');
  for (var i = 0; i < menus.length; i++) {
    if (!menus[i].contains(e.target)) menus[i].open = false;
  }
});

api('GET', '/api/me')
  .then(function (me) {
    state.isAdmin = me.role === 'admin';
    document.getElementById('who').textContent = 'Ви увійшли як ' + me.username;
    state.canManage = me.role !== 'device';
    document.getElementById('select-bar').hidden = !state.isAdmin;
    uploadBtn.hidden = !state.canManage;
    disk.hidden = !state.canManage;
    document.getElementById('users-link').hidden = !state.isAdmin;
    document.getElementById('settings-link').hidden = !state.canManage;
    connect();
    return refresh();
  })
  .catch(function () {});
