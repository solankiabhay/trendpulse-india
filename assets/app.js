/* TrendPulse India: client-side search and category filter over server-rendered cards.
   No network requests; uses textContent only (no HTML injection). */
(function () {
  'use strict';
  function matches(item, query, cat) {
    var q = (query || '').trim().toLocaleLowerCase();
    var catOk = !cat || cat === 'all' || item.cat === cat;
    return catOk && (!q || item.text.indexOf(q) !== -1);
  }
  if (typeof module !== 'undefined' && module.exports) { module.exports = { matches: matches }; }
  if (typeof document === 'undefined') { return; }

  document.addEventListener('DOMContentLoaded', function () {
    var grid = document.getElementById('grid');
    var input = document.getElementById('q');
    if (!grid || !input) { return; }
    var form = document.getElementById('searchform');
    var status = document.getElementById('status');
    var nomatch = document.getElementById('nomatch');
    var buttons = Array.prototype.slice.call(document.querySelectorAll('.cat-btn'));
    var items = Array.prototype.slice.call(grid.querySelectorAll('[data-cat]')).map(function (el) {
      return { el: el, cat: el.getAttribute('data-cat'), text: el.getAttribute('data-text') || '' };
    });
    var cat = 'all';

    function apply() {
      var shown = 0;
      items.forEach(function (it) {
        var ok = matches(it, input.value, cat);
        it.el.hidden = !ok;
        if (ok) { shown++; }
      });
      if (status && items.length) {
        status.textContent = (status.getAttribute('data-tpl') || '{n}').replace('{n}', String(shown));
      }
      if (nomatch) { nomatch.hidden = shown !== 0 || items.length === 0; }
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-cat') === cat)); });
    }

    var params = new URLSearchParams(window.location.search);
    if (params.get('q')) { input.value = params.get('q'); }
    var wanted = params.get('cat');
    if (wanted && buttons.some(function (b) { return b.getAttribute('data-cat') === wanted; })) { cat = wanted; }

    input.addEventListener('input', apply);
    if (form) { form.addEventListener('submit', function (e) { e.preventDefault(); apply(); }); }
    buttons.forEach(function (b) {
      b.addEventListener('click', function () { cat = b.getAttribute('data-cat'); apply(); });
    });
    apply();
  });
})();
