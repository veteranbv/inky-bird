// A classic script in the oldest syntax, so it runs where the app's modules
// cannot. If the app never replaces the start-up message, it says what to do.
(function () {
  var status = document.getElementById('status');
  if (!status) return;
  var waiting = status.textContent;
  function stuck() {
    if (document.getElementById('status') !== status || status.textContent !== waiting) return;
    status.textContent =
      'Inky could not start in this browser. Update the browser or open Inky in another one.';
    var main = document.getElementById('main');
    if (main) main.setAttribute('aria-busy', 'false');
  }
  if (!('noModule' in document.createElement('script'))) {
    stuck();
    return;
  }
  // A module that fails to load reports on its script element; one that the
  // browser cannot parse or run reports on the window. A failed image does not.
  function failed(event) {
    var target = event.target;
    if (!target || !target.tagName || target.tagName === 'SCRIPT') stuck();
  }
  window.addEventListener('error', failed, true);
  window.setTimeout(stuck, 20000);
})();
