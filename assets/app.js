/* TrendPulse India: category-only filter over server-rendered article cards. */
(function () {
  'use strict';
  var LABELS = {
    en: { all: 'All', india: 'India', world: 'World', business: 'Business', technology: 'Tech',
      entertainment: 'Entertainment', politics: 'Politics', gaming: 'Gaming', sports: 'Sports', science: 'Science', health: 'Health', trending: 'Trending' },
    hi: { all: 'सभी', india: 'भारत', world: 'दुनिया', business: 'कारोबार', technology: 'टेक',
      entertainment: 'मनोरंजन', politics: 'राजनीति', gaming: 'गेमिंग', sports: 'खेल', science: 'विज्ञान', health: 'स्वास्थ्य', trending: 'ट्रेंडिंग' }
  };
  function matches(item, cat) { return !cat || cat === 'all' || item.cat === cat; }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { matches: matches };
  }
  if (typeof document === 'undefined') { return; }

  document.addEventListener('DOMContentLoaded', function () {
    var grid = document.getElementById('grid');
    var nav = document.getElementById('categories');
    if (!grid || !nav) { return; }
    var cards = Array.prototype.slice.call(grid.querySelectorAll('[data-cat]'));
    var lang = document.documentElement.lang === 'hi' ? 'hi' : 'en';
    var labels = LABELS[lang];
    var present = {};
    cards.forEach(function (card) { present[card.getAttribute('data-cat')] = true; });
    var preferred = ['gaming', 'technology', 'business', 'sports', 'politics'];
    var cats = preferred.slice();
    Object.keys(present).sort().forEach(function (cat) {
      if (cat && cats.indexOf(cat) === -1) { cats.push(cat); }
    });
    document.querySelectorAll('img.card-image[data-fallback-category]').forEach(function (img) {
      img.addEventListener('error', function () {
        var fallback = document.createElement('div');
        fallback.className = 'card-image card-image-fallback cat-' + img.getAttribute('data-fallback-category');
        fallback.setAttribute('aria-hidden', 'true');
        var label = document.createElement('span');
        label.textContent = img.getAttribute('data-fallback-label') || 'News';
        fallback.appendChild(label);
        img.replaceWith(fallback);
      }, { once: true });
    });
    var buttons = [];
    function addButton(cat) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'cat-btn';
      button.setAttribute('data-cat', cat);
      button.setAttribute('aria-pressed', String(cat === 'all'));
      button.textContent = labels[cat] || cat;
      button.addEventListener('click', function () {
        active = cat;
        apply();
      });
      nav.appendChild(button);
      buttons.push(button);
    }
    addButton('all');
    cats.forEach(addButton);
    var active = 'all';
    var empty = document.createElement('p');
    empty.className = 'empty';
    empty.hidden = true;
    empty.textContent = lang === 'hi' ? 'इस श्रेणी में अभी कोई खबर नहीं है।' : 'No stories in this category yet.';
    grid.parentNode.insertBefore(empty, grid.nextSibling);
    function apply() {
      var visible = 0;
      cards.forEach(function (card) {
        card.hidden = !matches({ cat: card.getAttribute('data-cat') }, active);
        if (!card.hidden) { visible += 1; }
      });
      empty.hidden = visible > 0;
      buttons.forEach(function (button) {
        button.setAttribute('aria-pressed', String(button.getAttribute('data-cat') === active));
      });
    }
    var params = new URLSearchParams(window.location.search);
    var requested = params.get('cat');
    if (requested && cats.indexOf(requested) !== -1) { active = requested; }
    apply();
  });
})();