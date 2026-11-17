/* Runs only in a browser too old for the page's scripts, so it says so instead of leaving
   the page empty. Deliberately plain JavaScript. */
(function () {
  var message = document.getElementById('message');
  if (message) {
    message.textContent = 'Цей браузер застарий для цієї сторінки. Відкрийте її на телефоні або в новішому браузері.';
  }
})();
