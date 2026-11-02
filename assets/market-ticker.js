/* TrendPulse India — compact daily market ticker. */
(function () {
  'use strict';

  var ENDPOINT = 'https://snapdata.dev/api/v1/all/in/latest.json';
  var ITEMS = [
    ['XAU.24K.INR.G', 'GOLD 24K', '₹', '/g'],
    ['XAU.22K.INR.G', 'GOLD 22K', '₹', '/g'],
    ['XAG.INR.KG', 'SILVER', '₹', '/kg'],
    ['SENSEX.INR.IDX', 'SENSEX', '', ' pts'],
    ['NIFTY50.INR.IDX', 'NIFTY 50', '', ' pts'],
    ['NIFTYBANK.INR.IDX', 'BANK NIFTY', '', ' pts']
  ];

  function number(value, digits) {
    return new Intl.NumberFormat('en-IN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    }).format(value);
  }

  function makeItem(item, observation) {
    var wrapper = document.createElement('span');
    wrapper.className = 'market-ticker-item';

    var name = document.createElement('strong');
    name.className = 'market-ticker-name';
    name.textContent = item[1];

    var price = document.createElement('span');
    price.className = 'market-ticker-price';
    var digits = item[1].indexOf('SENSEX') === 0 || item[1].indexOf('NIFTY') === 0 ? 2 : 2;
    price.textContent = item[2] + number(observation.value, digits) + item[3];

    wrapper.appendChild(name);
    wrapper.appendChild(price);

    if (typeof observation.change_pct === 'number') {
      var change = document.createElement('span');
      change.className = 'market-ticker-change ' + (observation.change_pct > 0 ? 'is-up' : observation.change_pct < 0 ? 'is-down' : 'is-flat');
      change.textContent = (observation.change_pct > 0 ? '+' : '') + number(observation.change_pct, 2) + '%';
      wrapper.appendChild(change);
    }
    return wrapper;
  }

  function render(payload) {
    var ticker = document.getElementById('market-ticker');
    var dateLabel = document.getElementById('market-ticker-date');
    if (!ticker || !payload || !Array.isArray(payload.observations)) return;

    var byId = {};
    payload.observations.forEach(function (row) {
      if (row && row.instrument_id) byId[row.instrument_id] = row;
    });

    var selected = ITEMS.map(function (item) {
      return { item: item, row: byId[item[0]] };
    }).filter(function (entry) {
      return entry.row && typeof entry.row.value === 'number' &&
        entry.row.filled_from == null &&
        (entry.row.status === 'final' || entry.row.status === 'provisional' || entry.row.status === 'estimated');
    });

    ticker.textContent = '';
    if (!selected.length) {
      var empty = document.createElement('span');
      empty.className = 'market-ticker-loading';
      empty.textContent = 'Market prices temporarily unavailable';
      ticker.appendChild(empty);
      return;
    }

    var firstDate = selected.reduce(function (latest, entry) {
      return entry.row.date > latest ? entry.row.date : latest;
    }, '');
    var track = document.createElement('div');
    track.className = 'market-ticker-track';
    selected.forEach(function (entry) {
      track.appendChild(makeItem(entry.item, entry.row));
    });
    var duplicate = track.cloneNode(true);
    duplicate.setAttribute('aria-hidden', 'true');
    ticker.appendChild(track);
    ticker.appendChild(duplicate);

    if (dateLabel) dateLabel.textContent = firstDate ? 'As of ' + firstDate : 'Daily reference rates';
  }

  function load() {
    fetch(ENDPOINT, { headers: { Accept: 'application/json' }, cache: 'no-cache' })
      .then(function (response) {
        if (!response.ok) throw new Error('Market data request failed');
        return response.json();
      })
      .then(render)
      .catch(function () {
        var ticker = document.getElementById('market-ticker');
        if (ticker) ticker.textContent = 'Market prices temporarily unavailable';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load, { once: true });
  } else {
    load();
  }
})();
