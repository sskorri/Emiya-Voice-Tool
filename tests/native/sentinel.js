// --smoke only (ui\launch.py inlines this file in the head): counts what goes wrong in the page.
// window errors, failed resource loads, unhandled promise rejections and console.error calls are kept
// in window.__vtSentinel; the smoke plan reads it with run_javascript. It changes nothing on the page.
(function () {
  if (window.__vtSentinel) return;
  var MAX_ITEMS = 50;
  var s = window.__vtSentinel = {
    started: Date.now(),
    counts: { error: 0, resource: 0, rejection: 0, console: 0 },
    items: [],
    total: function () { return this.counts.error + this.counts.resource + this.counts.rejection + this.counts.console; },
  };
  function note(kind, text) {
    s.counts[kind] += 1;
    if (s.items.length < MAX_ITEMS) s.items.push({ kind: kind, text: String(text).slice(0, 300), t: Date.now() });
  }
  window.addEventListener('error', function (e) {
    var target = e && e.target;
    if (target && target !== window && (target.src || target.href)) {
      note('resource', (target.tagName || '?') + ' ' + (target.src || target.href));
    } else {
      note('error', (e && e.message ? e.message : 'error') + (e && e.filename ? ' @ ' + e.filename + ':' + e.lineno : ''));
    }
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var reason = e && e.reason;
    note('rejection', reason && reason.message ? reason.message : reason);
  });
  var original = console.error;
  console.error = function () {
    try {
      note('console', Array.prototype.map.call(arguments, function (a) {
        return a && a.message ? a.message : String(a);
      }).join(' '));
    } catch (ignored) { /* never break console.error */ }
    return original.apply(console, arguments);
  };
})();
