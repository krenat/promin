// What the projector page does, checked without a projector: run with `node --test`.
// The stand-in browser is in tv_page.mjs.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { connectedPage, openPage } from './tv_page.mjs';

function playing(page, id) {
  page.socket.command('load', { video_id: id || 'aaa', name: 'Фільм.mp4' });
  page.video.loaded(600);
  page.socket.take();
}

// ------------------------------------------------------------ getting started

test('a screen nobody is logged in on shows a QR code and waits to be approved', () => {
  const page = openPage();
  page.request('GET', '/api/me').respond(401, {});
  assert.equal(page.screen(), 'pair');
  page.request('POST', '/api/pair/start').respond(200, { qr: '<svg/>', token: 'tok' });
  assert.equal(page.el('pair-qr').className, 'on');
  assert.match(page.el('pair-qr').src, /^data:image\/svg\+xml/);
  assert.equal(page.sockets.length, 0);

  page.advance(2000);
  const poll = page.request('POST', '/api/pair/status');
  assert.deepEqual(poll.body, { token: 'tok' });
  poll.respond(200, { status: 'pending' });
  page.advance(2000);
  page.request('POST', '/api/pair/status').respond(200, { status: 'approved' });

  assert.equal(page.screen(), 'start');
  assert.equal(page.socket.url, 'ws://promin.test/ws?role=tv');
});

test('an expired QR code is replaced by a new one, and a busy server is asked again later', () => {
  const page = openPage();
  page.request('GET', '/api/me').respond(401, {});
  page.request('POST', '/api/pair/start').respond(200, { qr: '<svg/>', token: 'old' });
  page.advance(2000);
  page.request('POST', '/api/pair/status').respond(200, { status: 'expired' });
  const again = page.requests.filter((r) => r.url === '/api/pair/start');
  assert.equal(again.length, 2);

  again[1].respond(429, {});
  assert.match(page.el('pair-text').textContent, /Забагато спроб/);
  page.advance(59000);
  assert.equal(page.requests.filter((r) => r.url === '/api/pair/start').length, 2);
  page.advance(1000);
  assert.equal(page.requests.filter((r) => r.url === '/api/pair/start').length, 3);
});

test('a server that does not answer makes the page reload itself', () => {
  const page = openPage();
  page.request('GET', '/api/me').fail();
  assert.equal(page.screen(), 'waiting');
  page.advance(5000);
  assert.equal(page.reloads, 1);
});

test('nothing plays before the start button is pressed', () => {
  const page = connectedPage({ started: false });
  assert.equal(page.screen(), 'start');
  page.socket.command('load', { video_id: 'aaa', name: 'Фільм.mp4' });
  assert.equal(page.video.src, '/media/aaa');
  assert.equal(page.video.plays, 0);
  assert.equal(page.screen(), 'start');
  assert.equal(page.socket.state().started, false);

  page.click('start-btn');
  assert.equal(page.screen(), 'playing');
  assert.equal(page.video.plays, 1);
  assert.equal(page.fullscreen, 1);
  assert.deepEqual(
    page.socket.state(),
    { type: 'state', video_id: 'aaa', name: 'Фільм.mp4', position: 0, duration: 0, paused: false, volume: 1, started: true },
  );
});

test('a browser that refuses to play with sound asks for the press again', () => {
  const page = connectedPage();
  page.video.refuses = { name: 'NotAllowedError' };
  page.socket.command('load', { video_id: 'aaa', name: 'Фільм.mp4' });
  assert.equal(page.screen(), 'start');
  assert.equal(page.focused, 'start-btn');
  assert.equal(page.socket.state().started, false);

  page.video.refuses = null;
  page.click('start-btn');
  assert.equal(page.screen(), 'playing');
  assert.equal(page.video.paused, false);
});

// ------------------------------------------------------------------ the remote

test('the remote pauses, plays, seeks and sets the volume, within what the video allows', () => {
  const page = connectedPage();
  playing(page);

  page.socket.command('pause');
  assert.equal(page.video.paused, true);
  assert.equal(page.el('bar').className, 'on');  // the progress bar shows on pause
  assert.equal(page.el('bar-title').textContent, 'Фільм.mp4');
  page.advance(3000);
  assert.equal(page.el('bar').className, '');    // and hides again
  page.socket.command('play');
  assert.equal(page.video.paused, false);

  page.socket.command('seek', { value: 120 });
  assert.equal(page.video.currentTime, 120);
  page.socket.command('seek_by', { value: -10 });
  assert.equal(page.video.currentTime, 110);
  page.socket.command('seek', { value: 9999 });
  assert.equal(page.video.currentTime, 599.5);   // never onto the very end
  page.socket.command('seek', { value: -5 });
  assert.equal(page.video.currentTime, 0);

  page.socket.command('volume', { value: 0.3 });
  assert.equal(page.video.volume, 0.3);
  page.socket.command('volume', { value: 7 });
  assert.equal(page.video.volume, 1);
  page.socket.command('volume', { value: -1 });
  assert.equal(page.video.volume, 0);

  page.socket.take();
  page.socket.command('stop');
  assert.equal(page.screen(), 'waiting');
  assert.equal(page.video.src, '');
  assert.equal(page.socket.state().video_id, null);
});

test('commands for a video mean nothing when none is loaded', () => {
  const page = connectedPage();
  ['play', 'pause', 'stop'].forEach((action) => page.socket.command(action));
  page.socket.command('seek', { value: 50 });
  assert.equal(page.video.plays, 0);
  assert.equal(page.video.currentTime, 0);
  assert.equal(page.screen(), 'waiting');
});

test('what is not a command is ignored', () => {
  const page = connectedPage();
  page.socket.onmessage({ data: 'not json' });
  page.socket.onmessage({ data: JSON.stringify({ type: 'state', action: 'load', video_id: 'x' }) });
  page.socket.onmessage({ data: 'null' });
  assert.equal(page.video.src, '');
});

test('the page reports its state every second', () => {
  const page = connectedPage();
  playing(page);
  page.video.at(42);
  page.advance(1000);
  const state = page.socket.state();
  assert.equal(state.position, 42);
  assert.equal(state.duration, 600);
});

// ------------------------------------------------------ the end and the errors

test('the end of a video is reported before the page forgets what was playing', () => {
  const page = connectedPage();
  playing(page);
  page.video.emit('ended');
  const sent = page.socket.take();
  assert.deepEqual(sent[0], { type: 'ended', video_id: 'aaa' });
  assert.equal(sent[sent.length - 1].video_id, null);
  assert.equal(page.screen(), 'waiting');

  page.video.emit('ended');  // a second report of the same end
  assert.deepEqual(page.socket.take('ended'), []);
});

test('a playback error reloads the video and continues from where it stopped', () => {
  const page = connectedPage();
  playing(page);
  page.video.at(250);
  page.video.src = '';
  page.video.emit('error');
  assert.deepEqual(page.socket.take('ended'), []);

  page.advance(1999);
  assert.equal(page.video.src, '');
  page.advance(1);
  assert.equal(page.video.src, '/media/aaa');
  page.video.at(0);            // the reloaded video starts from zero
  page.video.loaded(600);
  assert.equal(page.video.currentTime, 250);
  assert.equal(page.screen(), 'playing');
});

test('after three failures in a row the video counts as over', () => {
  const page = connectedPage();
  playing(page);
  page.video.at(250);
  for (let attempt = 1; attempt <= 3; attempt++) {
    page.video.emit('error');
    page.advance(2000 * attempt);  // each retry waits longer
    assert.deepEqual(page.socket.take('ended'), [], 'failure ' + attempt);
  }
  page.video.emit('error');
  assert.deepEqual(page.socket.take('ended'), [{ type: 'ended', video_id: 'aaa' }]);
  assert.equal(page.screen(), 'waiting');
});

test('ten seconds of playback after a failure make the next one count as the first', () => {
  const page = connectedPage();
  playing(page);
  page.video.at(250);
  for (let round = 0; round < 5; round++) {
    page.video.emit('error');
    page.advance(2000);
    page.video.loaded(600);
    page.video.at(page.video.currentTime + 11);
  }
  assert.deepEqual(page.socket.take('ended'), []);
  assert.equal(page.screen(), 'playing');
});

test('a retry does not bring back a video that was stopped or replaced meanwhile', () => {
  const page = connectedPage();
  playing(page);
  page.video.emit('error');
  page.socket.command('load', { video_id: 'bbb', name: 'Інший.mp4' });
  page.advance(10000);
  assert.equal(page.video.src, '/media/bbb');

  page.video.emit('error');
  page.socket.command('stop');
  page.advance(10000);
  assert.equal(page.video.src, '');
});

// ------------------------------------------------------------- the connection

test('a dropped connection is retried, each time later, up to five seconds', () => {
  const page = connectedPage();
  const waits = [1000, 2000, 4000, 5000, 5000];
  waits.forEach((wait, i) => {
    page.socket.close();
    assert.match(page.el('waiting-text').textContent, /Немає зв’язку/);
    page.advance(wait - 1);
    assert.equal(page.sockets.length, i + 1, 'too early after ' + wait);
    page.advance(1);
    assert.equal(page.sockets.length, i + 2);
  });

  page.socket.open();  // connected again: the hint is back and the wait starts over
  assert.equal(page.el('waiting-text').textContent, 'Оберіть відео на телефоні');
  page.socket.close();
  page.advance(1000);
  assert.equal(page.sockets.length, waits.length + 2);
});

test('a video keeps playing while the connection is away', () => {
  const page = connectedPage();
  playing(page);
  page.socket.close();
  assert.equal(page.video.paused, false);
  assert.equal(page.screen(), 'playing');
  page.advance(1000);
  page.socket.open();
  assert.equal(page.socket.state().video_id, 'aaa');
});

for (const [code, text] of [[4000, /іншому екрані/], [4001, /від’єднали з телефона/]]) {
  test('closed with ' + code + ', the page stops being the projector until asked', () => {
    const page = connectedPage();
    playing(page);
    page.socket.close(code);
    assert.equal(page.screen(), 'replaced');
    assert.match(page.el('replaced-text').textContent, text);
    assert.equal(page.video.paused, true);
    assert.equal(page.focused, 'takeover-btn');
    page.advance(60000);
    assert.equal(page.sockets.length, 1);

    page.click('takeover-btn');
    assert.equal(page.sockets.length, 2);
    assert.equal(page.screen(), 'playing');
  });
}

test('a projector that was unpaired goes back to the QR code', () => {
  const page = connectedPage();
  playing(page);
  page.socket.close(4401);
  assert.equal(page.screen(), 'pair');
  assert.equal(page.video.paused, true);
  assert.equal(page.requests.filter((r) => r.url === '/api/pair/start').length, 1);
  page.advance(60000);
  assert.equal(page.sockets.length, 1);
});

test('the close of a connection that was already replaced changes nothing', () => {
  const page = connectedPage();
  const first = page.socket;
  first.close(4000);
  page.click('takeover-btn');
  page.socket.open();
  first.onclose({ code: 1006 });
  page.advance(10000);
  assert.equal(page.sockets.length, 2);
});

// ---------------------------------------------------------- the projector check

const CHECK = {
  run: 'r1', seconds: 6,
  speed: { url: '/tvtest/speed', seconds: 6 },
  clips: [
    { id: '720p30', height: 720, fps: 30, needs: null, url: '/tvtest/clip/720p30' },
    { id: '720p60', height: 720, fps: 60, needs: '720p30', url: '/tvtest/clip/720p60' },
    { id: '1080p30', height: 1080, fps: 30, needs: '720p30', url: '/tvtest/clip/1080p30' },
  ],
};

function playClip(page, advance) {
  const node = page.inserted[page.inserted.length - 1];
  assert.equal(node.muted, true);
  node.emit('playing');
  node.currentTime = 6 * advance;
  page.advance(6000);
}

test('the check measures the speed, plays the clips in turn and skips what cannot work', () => {
  const page = connectedPage();
  page.socket.command('test', CHECK);
  assert.deepEqual(page.socket.take('test'), [{ type: 'test', run: 'r1', step: 0 }]);

  // 3 MB in the six seconds of the measurement: 4000 kbit/s.
  page.request('GET', '/tvtest/speed').respond(200, undefined, { byteLength: 3000000 });
  page.advance(6000);

  assert.match(page.el('waiting-text').textContent, /720p, 30/);
  page.request('GET', '/tvtest/clip/720p30').respond(200, undefined, 'blob');
  assert.equal(page.screen(), 'playing');
  playClip(page, 1);

  page.request('GET', '/tvtest/clip/720p60').respond(200, undefined, 'blob');
  playClip(page, 0.5);  // kept up with half of the clock only

  page.request('GET', '/tvtest/clip/1080p30').respond(404);

  const report = page.socket.take('test').pop();
  assert.equal(report.done, true);
  assert.equal(report.speed_kbps, 4000);
  assert.deepEqual(report.clips.map((c) => [c.id, c.outcome, c.smooth]), [
    ['720p30', 'played', true], ['720p60', 'played', false], ['1080p30', 'failed', undefined],
  ]);
  assert.equal(report.clips[2].reason, 'download');
  assert.deepEqual(report.device, { agent: 'TestTV/1.0', screen_width: 1920, screen_height: 1080 });
  assert.equal(page.screen(), 'waiting');
  assert.ok(page.inserted.every((node) => node.parentNode === null));
});

test('a harder clip is not tried when the easier one did not play smoothly', () => {
  const page = connectedPage();
  page.socket.command('test', Object.assign({}, CHECK, { speed: {} }));
  page.request('GET', '/tvtest/clip/720p30').respond(200, undefined, 'blob');
  playClip(page, 0.2);
  const report = page.socket.take('test').pop();
  assert.deepEqual(report.clips.map((c) => c.outcome), ['played', 'skipped', 'skipped']);
  assert.equal(page.requests.filter((r) => r.url.indexOf('/tvtest/clip/') === 0).length, 1);
});

test('the check does not interrupt a video, and a video chosen on the phone ends the check', () => {
  const page = connectedPage();
  playing(page);
  page.socket.command('test', CHECK);
  assert.deepEqual(page.socket.take('test'), [{ type: 'test', run: 'r1', aborted: true }]);
  assert.equal(page.screen(), 'playing');

  const idle = connectedPage();
  idle.socket.command('test', CHECK);
  idle.socket.take();
  idle.socket.command('load', { video_id: 'aaa', name: 'Фільм.mp4' });
  assert.deepEqual(idle.socket.take('test'), [{ type: 'test', run: 'r1', aborted: true }]);
  assert.equal(idle.request('GET', '/tvtest/speed').aborted, true);
  assert.equal(idle.screen(), 'playing');
});

test('a check is dropped without a word when the connection drops', () => {
  const page = connectedPage();
  page.socket.command('test', CHECK);
  const first = page.socket;
  first.take();
  first.close();
  assert.deepEqual(first.take('test'), []);
  assert.equal(page.request('GET', '/tvtest/speed').aborted, true);
  page.advance(1000);
  page.socket.open();
  page.advance(60000);
  assert.deepEqual(page.socket.take('test'), []);
});
