/* TrendPulse India: category-only filter over server-rendered article cards. */
(function () {
  'use strict';
  var LABELS = {
    en: { all: 'All', india: 'India', world: 'World', business: 'Business', technology: 'Technology',
      entertainment: 'Entertainment', sports: 'Sports', science: 'Science', health: 'Health', trending: 'Trending' },
    hi: { all: 'सभी', india: 'भारत', world: 'दुनिया', business: 'कारोबार', technology: 'तकनीक',
      entertainment: 'मनोरंजन', sports: 'खेल', science: 'विज्ञान', health: 'स्वास्थ्य', trending: 'ट्रेंडिंग' }
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
    var cats = [];
    cards.forEach(function (card) {
      var cat = card.getAttribute('data-cat');
      if (cat && cats.indexOf(cat) === -1) { cats.push(cat); }
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
    function apply() {
      cards.forEach(function (card) {
        card.hidden = !matches({ cat: card.getAttribute('data-cat') }, active);
      });
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