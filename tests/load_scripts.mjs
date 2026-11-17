// Loads a page's script the way a browser would, with a stand-in for the page itself.
//
// Run by test_scripts_load: `node load_scripts.mjs <path to the script>`. Loading fails if
// an import names something that is not exported, if a script has a syntax error, or if a
// module uses what it imports while it is still loading (see static/js/index/main.js).

// Something that can be called, read from and written to, and always gives itself back.
function standIn() {
  return new Proxy(function () {}, {
    get: function (target, key) {
      if (key === 'then') return undefined;  // not a promise
      if (key === Symbol.toPrimitive) return function () { return ''; };
      return standIn();
    },
    set: function () { return true; },
    apply: function () { return standIn(); },
    construct: function () { return standIn(); },
  });
}

globalThis.document = standIn();
globalThis.localStorage = { getItem: function () { return null; }, setItem: function () {} };
globalThis.location = { host: 'localhost', protocol: 'http:', href: '' };
// Requests never answer: only what runs while the script loads is checked.
globalThis.fetch = function () { return new Promise(function () {}); };
globalThis.WebSocket = function () {};

await import(process.argv[2]);
console.log('loaded');
