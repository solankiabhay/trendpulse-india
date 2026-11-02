/* TrendPulse India — compact daily market ticker. */
(function () {
  'use strict';

  var DATA_ENDPOINT = 'https://snapdata.dev/api/v1/all/in/latest.json';
  var META_ENDPOINT = 'https://snapdata.dev/api/v1/meta.json';
  var ITEMS = [
    ['XAU.24K.INR.G', 'GOLD 24K', '₹', '/g'],
    ['XAU.22K.INR.G', 'GOLD 22K', '₹', '/g'],
    ['XAG.INR.KG', 'SILVER', '₹', '/kg'],
    ['SENSEX.INR.IDX', 'SENSEX', '', ' pts'],
    ['NIFTY50.INR.IDX', 'NIFTY 50', '', ' pts'],
    ['NIFTYBANK.INR.IDX', 'BANK NIFTY', '', ' pts']
  ];

  function number(value) {
    return new Intl.NumberFormat('en-IN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
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
    price.textContent = item[2] + number(observation.value) + item[3];

    var date = document.createElement('small');
    date.className = 'market-ticker-item-date';
    date.textContent = observation.date ? ' ' + observation.date : '';

    wrapper.appendChild(name);
    wrapper.appendChild(price);
    wrapper.appendChild(date);

    if (typeof observation.change_pct === 'number' && isFinite(observation.change_pct)) {
      var change = document.createElement('span');
      change.className = 'market-ticker-change ' +
        (observation.change_pct > 0 ? 'is-up' : observation.change_pct < 0 ? 'is-down' : 'is-flat');
      change.textContent = (observation.change_pct > 0 ? '+' : '') + number(observation.change_pct) + '%';
      wrapper.appendChild(change);
    }
    return wrapper;
  }

  function showUnavailable(ticker, message) {
    ticker.textContent = '';
    var empty = document.createElement('span');
    empty.className = 'market-ticker-loading';
    empty.textContent = message;
    ticker.appendChild(empty);
  }

  function render(payload, meta) {
    var ticker = document.getElementById('market-ticker');
    var dateLabel = document.getElementById('market-ticker-date');
    if (!ticker) return;

    if (!payload || !Array.isArray(payload.observations)) {
      showUnavailable(ticker, 'Market prices temporarily unavailable');
      if (dateLabel) dateLabel.textContent = 'Data feed unavailable';
      return;
    }

    var byId = {};
    payload.observations.forEach(function (row) {
      if (row && row.instrument_id) byId[row.instrument_id] = row;
    });

    var selected = ITEMS.map(function (item) {
      return { item: item, row: byId[item[0]] };
    }).filter(function (entry) {
      return entry.row && typeof entry.row.value === 'number' &&
        isFinite(entry.row.value) && entry.row.filled_from == null &&
        (entry.row.status === 'final' || entry.row.status === 'provisional' || entry.row.status === 'estimated');
    });

    ticker.textContent = '';
    if (!selected.length) {
      showUnavailable(ticker, 'No verified market values available');
      if (dateLabel) dateLabel.textContent = 'Waiting for the next data update';
      return;
    }

    var track = document.createElement('div');
    track.className = 'market-ticker-track';
    selected.forEach(function (entry) {
      track.appendChild(makeItem(entry.item, entry.row));
    });
    ticker.appendChild(track);
    ticker.appendChild(track.cloneNode(true));
    ticker.lastChild.setAttribute('aria-hidden', 'true');

    if (dateLabel) {
      var providerStatus = meta && typeof meta.status === 'string' ? meta.status : 'unknown';
      dateLabel.textContent = providerStatus === 'ok'
        ? 'Daily snapshot · not live'
        : 'Provider status: ' + providerStatus + ' · check dates';
    }
  }

  function load() {
    var ticker = document.getElementById('market-ticker');
    var dateLabel = document.getElementById('market-ticker-date');
    if (!ticker) return;

    fetch(DATA_ENDPOINT, { headers: { Accept: 'application/json' }, cache: 'no-cache' })
      .then(function (response) {
        if (!response.ok) throw new Error('Market data request failed');
        return response.json();
      })
      .then(function (payload) {
        return fetch(META_ENDPOINT, { headers: { Accept: 'application/json' }, cache: 'no-cache' })
          .then(function (response) {
            if (!response.ok) throw new Error('Market metadata request failed');
            return response.json();
          })
          .then(function (meta) { render(payload, meta); })
          .catch(function () { render(payload, null); });
      })
      .catch(function () {
        showUnavailable(ticker, 'Market prices temporarily unavailable');
        if (dateLabel) dateLabel.textContent = 'Could not load market data';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load, { once: true });
  } else {
    load();
  }
})();
