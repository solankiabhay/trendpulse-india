/* TrendPulse India: category filter across fresh stories and the archive. */
(function () {
  'use strict';
  function matches(item, cat) { return !cat || cat === 'all' || item.cat === cat; }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { matches: matches };
  }
  if (typeof document === 'undefined') { return; }

  document.addEventListener('DOMContentLoaded', function () {
    var nav = document.getElementById('categories');
    if (!nav) { return; }
    // Do not limit filtering to #grid: the Hindi page can have no fresh stories
    // while still containing many older stories in its archive.
    var cards = Array.prototype.slice.call(document.querySelectorAll('main .card[data-cat]'));
    var buttons = Array.prototype.slice.call(nav.querySelectorAll('.cat-btn'));
    var active = 'all';
    var lang = document.documentElement.lang === 'hi' ? 'hi' : 'en';
    var empty = document.createElement('p');
    empty.className = 'empty category-empty';
    empty.hidden = true;
    empty.textContent = lang === 'hi'
      ? 'इस श्रेणी में अभी कोई खबर नहीं है। दूसरी श्रेणी चुनें।'
      : 'No stories in this category yet. Choose another category.';
    nav.parentNode.appendChild(empty);

    function apply() {
      var visible = 0;
      cards.forEach(function (card) {
        card.hidden = !matches({ cat: card.getAttribute('data-cat') }, active);
        if (!card.hidden) { visible += 1; }
      });
      // Hide an entire section only when it contains cards and none match.
      document.querySelectorAll('main section').forEach(function (section) {
        var sectionCards = section.querySelectorAll('.card[data-cat]');
        if (sectionCards.length) {
          section.hidden = !Array.prototype.some.call(sectionCards, function (card) {
            return !card.hidden;
          });
        }
      });
      empty.hidden = visible > 0;
      buttons.forEach(function (button) {
        button.setAttribute('aria-pressed', String(button.getAttribute('data-cat') === active));
      });
    }

    buttons.forEach(function (button) {
      button.addEventListener('click', function () {
        active = button.getAttribute('data-cat') || 'all';
        apply();
      });
    });
    var params = new URLSearchParams(window.location.search);
    var requested = params.get('cat');
    if (requested && buttons.some(function (button) {
      return button.getAttribute('data-cat') === requested;
    })) { active = requested; }
    apply();
  });
})();
