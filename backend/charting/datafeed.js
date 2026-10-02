/*
 * MarginWealth custom Datafeed for TradingView Advanced Charts.
 *
 * Implements the TradingView "JS Datafeed API" against MarginWealth's own
 * backend instead of a UDF server — so NO new backend endpoints are needed.
 * Historical candles come from:
 *     GET /api/v1/user/instruments/{token}/history?interval=&days=
 * (the same endpoint the old lightweight charts used). Live candles are
 * driven by the React Native host: it postMessages every WS tick into the
 * WebView, which calls window.__tick(price, ts) -> the active subscriber's
 * bar is extended/rolled, exactly like Upstox/Zerodha streaming bars.
 *
 * Config is injected by the RN WebView BEFORE this script runs, as
 *   window.__CFG = { base, jwt, token, symbol, interval, theme, pricescale }
 * so the auth JWT never appears in the URL / server logs.
 */
(function () {
  function cfg() {
    return window.__CFG || {};
  }

  // TradingView resolution -> our backend interval + a sensible lookback.
  var RES_TO_INTERVAL = {
    "1": "minute", "3": "minute", "5": "5minute", "15": "15minute",
    "30": "30minute", "60": "60minute", "120": "60minute", "240": "60minute",
    "1D": "day", D: "day", "1W": "day", W: "day", "1M": "day", M: "day",
  };
  var RES_TO_DAYS = {
    "1": 5, "3": 10, "5": 20, "15": 40, "30": 60, "60": 90,
    "120": 150, "240": 250, "1D": 400, D: 400, "1W": 900, W: 900,
  };
  function intervalSecForRes(res) {
    switch (RES_TO_INTERVAL[res] || "5minute") {
      case "minute": return 60;
      case "5minute": return 300;
      case "15minute": return 900;
      case "30minute": return 1800;
      case "60minute": return 3600;
      case "day": return 86400;
    }
    return 300;
  }

  function MarginWealthDatafeed() {
    this._subs = {};        // listenerGuid -> { onTick, lastBar, intervalSec }
    this._lastHistBar = null; // most recent bar from the last getBars (per chart)
  }

  MarginWealthDatafeed.prototype.onReady = function (cb) {
    setTimeout(function () {
      cb({
        supported_resolutions: ["1", "5", "15", "30", "60", "1D"],
        supports_marks: false,
        supports_timescale_marks: false,
        supports_time: true,
      });
    }, 0);
  };

  MarginWealthDatafeed.prototype.searchSymbols = function (_u, _e, _t, onResult) {
    onResult([]); // symbol search is driven by the app, not the chart
  };

  MarginWealthDatafeed.prototype.resolveSymbol = function (symbolName, onResolve) {
    var c = cfg();
    var ps = Number(c.pricescale) || 100;
    var info = {
      name: c.symbol || symbolName,
      ticker: symbolName,
      description: c.symbol || symbolName,
      type: "stock",
      session: "24x7",
      timezone: "Asia/Kolkata",
      exchange: c.exchange || "",
      listed_exchange: c.exchange || "",
      minmov: 1,
      pricescale: ps,
      has_intraday: true,
      has_daily: true,
      has_weekly_and_monthly: false,
      supported_resolutions: ["1", "5", "15", "30", "60", "1D"],
      volume_precision: 0,
      data_status: "streaming",
      format: "price",
    };
    setTimeout(function () { onResolve(info); }, 0);
  };

  MarginWealthDatafeed.prototype.getBars = function (
    symbolInfo, resolution, periodParams, onResult, onError
  ) {
    var self = this;
    var c = cfg();
    // Our /history returns a single "last N days" window — it has no
    // arbitrary range / pagination. So only the first request fetches; any
    // older-data follow-up requests report noData so the chart stops paging.
    if (!periodParams.firstDataRequest) {
      onResult([], { noData: true });
      return;
    }
    var interval = RES_TO_INTERVAL[resolution] || "5minute";
    var days = RES_TO_DAYS[resolution] || 30;
    var token = symbolInfo.ticker;
    var url =
      c.base + "/api/v1/user/instruments/" + encodeURIComponent(token) +
      "/history?interval=" + interval + "&days=" + days;
    fetch(url, { headers: { Authorization: "Bearer " + (c.jwt || "") } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var rows = (j && j.data) || [];
        var bars = [];
        for (var i = 0; i < rows.length; i++) {
          var rr = rows[i];
          var t;
          if (rr.date != null) t = Date.parse(rr.date);
          else if (rr.time != null) {
            t = typeof rr.time === "number"
              ? (rr.time > 1e12 ? rr.time : rr.time * 1000)
              : Date.parse(rr.time);
          } else t = NaN;
          if (!isFinite(t)) continue;
          bars.push({
            time: t,
            open: +rr.open, high: +rr.high, low: +rr.low, close: +rr.close,
            volume: rr.volume != null ? +rr.volume : undefined,
          });
        }
        bars.sort(function (a, b) { return a.time - b.time; });
        if (!bars.length) { onResult([], { noData: true }); return; }
        self._lastHistBar = bars[bars.length - 1];
        onResult(bars, { noData: false });
      })
      .catch(function (e) {
        if (onError) onError(String(e && e.message ? e.message : e));
      });
  };

  MarginWealthDatafeed.prototype.subscribeBars = function (
    symbolInfo, resolution, onTick, guid
  ) {
    this._subs[guid] = {
      onTick: onTick,
      // Seed from the latest historical bar so the first live tick continues
      // the correct candle instead of opening a phantom one.
      lastBar: this._lastHistBar
        ? {
            time: this._lastHistBar.time, open: this._lastHistBar.open,
            high: this._lastHistBar.high, low: this._lastHistBar.low,
            close: this._lastHistBar.close, volume: this._lastHistBar.volume || 0,
          }
        : null,
      intervalSec: intervalSecForRes(resolution),
    };
  };

  MarginWealthDatafeed.prototype.unsubscribeBars = function (guid) {
    delete this._subs[guid];
  };

  // Called from the RN host on every WS tick (via window.__tick).
  MarginWealthDatafeed.prototype._applyTick = function (price, ts) {
    var p = Number(price);
    if (!isFinite(p) || p <= 0) return;
    var nowSec = Math.floor((ts || Date.now()) / 1000);
    for (var g in this._subs) {
      var s = this._subs[g];
      if (!s) continue;
      var bucketMs = Math.floor(nowSec / s.intervalSec) * s.intervalSec * 1000;
      if (!s.lastBar || bucketMs > s.lastBar.time) {
        s.lastBar = { time: bucketMs, open: p, high: p, low: p, close: p, volume: 0 };
      } else {
        s.lastBar.high = Math.max(s.lastBar.high, p);
        s.lastBar.low = Math.min(s.lastBar.low, p);
        s.lastBar.close = p;
      }
      try {
        s.onTick({
          time: s.lastBar.time, open: s.lastBar.open, high: s.lastBar.high,
          low: s.lastBar.low, close: s.lastBar.close, volume: s.lastBar.volume,
        });
      } catch (e) {}
    }
  };

  window.MarginWealthDatafeed = MarginWealthDatafeed;
  window.__tick = function (price, ts) {
    if (window.__df) window.__df._applyTick(price, ts);
  };
})();
