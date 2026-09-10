"""Kursdaten aus Yahoo Finance.

Ersetzt die MT5-Anbindung des lokalen ChartTerminals: dieselben Groessen
(Bars, ATR, Volumenprofil, Session-Extreme), nur aus einer Quelle, die
von einem Server aus erreichbar ist. Kein API-Key noetig.
"""

import time
import threading
from datetime import datetime, timezone

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# Zwei gleichwertige Hosts. Yahoo drosselt einzelne Adressen stossweise mit
# HTTP 429; der zweite Host faengt das in der Regel ab.
HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
CHART_PATH = "/v8/finance/chart/{sym}"

# Yahoo-Symbol je Markt. Das Optionssymbol steht daneben, weil die Kette
# einer anderen Notation folgt (Index mit Unterstrich bei Cboe).
MARKETS = {
    "NQ": {"name": "Nasdaq 100",   "chart": "NQ=F",  "index": "^NDX",  "chain": "_NDX", "tick": 0.25},
    "ES": {"name": "S&P 500",      "chart": "ES=F",  "index": "^GSPC", "chain": "_SPX", "tick": 0.25},
    "YM": {"name": "Dow Jones",    "chart": "YM=F",  "index": "^DJI",  "chain": "_DJX", "tick": 1.0},
    "RTY": {"name": "Russell 2000", "chart": "RTY=F", "index": "^RUT",  "chain": "_RUT", "tick": 0.1},
    "GC": {"name": "Gold",         "chart": "GC=F",  "index": "GC=F",  "chain": "GLD",  "tick": 0.1},
}
DEFAULT_MARKET = "NQ"

# Yahoo erlaubt je Intervall nur eine begrenzte Historie.
INTERVALS = {
    "1m":  "5d",
    "5m":  "1mo",
    "15m": "1mo",
    "30m": "2mo",
    "1h":  "3mo",
    "1d":  "2y",
}

_cache = {}
_lock = threading.Lock()


def _get(path, params, ttl):
    """Holt einen Yahoo-Pfad mit Zeit-Cache und Ausweichkette.

    Reihenfolge: frischer Cache, dann beide Hosts, dann - wenn alles
    scheitert - der abgelaufene Cache. Ein zaeher Kurs ist brauchbarer als
    ein leerer Chart; der Aufrufer erfaehrt ueber `stale` davon.
    """
    key = (path, tuple(sorted(params.items())))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1], False

    last_err = None
    for attempt, host in enumerate(HOSTS):
        try:
            r = requests.get(host + path, params=params,
                             headers={"User-Agent": UA}, timeout=20)
            if r.status_code == 429:
                last_err = "429"
                time.sleep(0.4 * (attempt + 1))
                continue
            if not r.ok:
                last_err = str(r.status_code)
                continue
            data = r.json()
            with _lock:
                _cache[key] = (now, data)
            return data, False
        except Exception as exc:
            last_err = str(exc)

    if hit:
        return hit[1], True
    return None, True


def candles(symbol, interval="15m", ttl=20):
    """Liefert Bars als Liste von Dicts: t (Epoch-Sekunden), o, h, l, c, v."""
    rng = INTERVALS.get(interval, "1mo")
    data, stale = _get(CHART_PATH.format(sym=symbol),
                       {"interval": interval, "range": rng}, ttl)
    if not data:
        return [], {}
    try:
        res = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return [], {}
    meta = dict(res.get("meta", {}) or {})
    meta["stale"] = stale
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
    v = q.get("volume") or []
    bars = []
    for i, t in enumerate(ts):
        # Yahoo liefert Luecken als null - solche Bars fallen raus statt
        # den Chart mit Nullwerten zu verzerren.
        if i >= len(c) or c[i] is None or o[i] is None or h[i] is None or l[i] is None:
            continue
        bars.append({
            "t": int(t), "o": float(o[i]), "h": float(h[i]),
            "l": float(l[i]), "c": float(c[i]),
            "v": float(v[i]) if i < len(v) and v[i] is not None else 0.0,
        })
    return bars, meta


def spot(symbol, ttl=15):
    """Aktueller Kurs eines Symbols, oder None."""
    data, _ = _get(CHART_PATH.format(sym=symbol), {"interval": "1d", "range": "1d"}, ttl)
    try:
        return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def anchor_ratio(chart_symbol, index_symbol):
    """Verhaeltnis Chartpreis zu Indexpreis, zum selben Zeitpunkt gemessen.

    Die Optionsstrikes stehen im Index-Preisraum, der Chart laeuft auf dem
    Future. Im lokalen Terminal war genau diese doppelte Umrechnung die
    Quelle des Basis-Jitters von +/-170 Punkten. Hier wird nur ein einziges
    Verhaeltnis gebildet, aus zwei zeitgleich geholten Kursen - dieselbe
    Methode, die im Gameplan-Dokument auf 0,6 Punkte genau nachgewiesen ist.
    """
    if chart_symbol == index_symbol:
        return 1.0
    a, b = spot(chart_symbol), spot(index_symbol)
    if not a or not b:
        return 1.0
    return a / b


def atr(bars, period=14):
    """Average True Range ueber die letzten `period` Bars."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        trs.append(max(cur["h"] - cur["l"],
                       abs(cur["h"] - prev["c"]),
                       abs(cur["l"] - prev["c"])))
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def volume_profile(bars, bins=180, kind="volume"):
    """Volumenprofil mit Seitenaufteilung und Knotenerkennung.

    Deutlich feiner aufgeloest als ein blosser Streifen, und zweigeteilt:
    Cboe liefert je Minute getrennt Call- und Put-Volumen, und dieser
    Unterschied ist die eigentliche Aussage - ein Preisband, an dem fast
    nur Puts gehandelt wurden, bedeutet etwas anderes als eines mit
    ueberwiegend Calls, auch wenn die Gesamtsumme gleich ist.

    Zusaetzlich markiert:
      HVN  Volumenknoten - der Kurs hat hier Zeit verbracht, Reaktionen
           sind wahrscheinlich
      LVN  Vakuum zwischen zwei Knoten - hier laeuft der Kurs schnell
           durch, das sind die Zielkandidaten
    """
    if not bars:
        return {}
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    if hi <= lo:
        return {}
    step = (hi - lo) / bins
    tot = [0.0] * bins
    call = [0.0] * bins
    put = [0.0] * bins

    # Verteilung innerhalb der Kerze: NICHT gleichmaessig ueber die ganze
    # Spanne. Genau das waere die TPO-Rechnung - sie zaehlt, welche Preise
    # beruehrt wurden, und macht aus jedem Gewicht wieder Zeit je Preis.
    #
    # Gehandelt wird ueberwiegend im Koerper zwischen Eroeffnung und
    # Schluss; die Dochte sind Ausschlaege, an denen wenig umging.
    # Deshalb faellt der groessere Teil auf den Koerper. Das bleibt eine
    # Naeherung - exakt ginge es nur mit Tickdaten -, aber es ist die
    # Naeherung, die ein Volumenprofil von einem TPO unterscheidet.
    BODY_SHARE = 0.72

    def spread(i0, i1, amount, acc):
        span = i1 - i0 + 1
        share = amount / span
        for i in range(i0, i1 + 1):
            acc[i] += share

    def idx(price):
        return max(0, min(bins - 1, int((price - lo) / step)))

    for b in bars:
        wick0, wick1 = idx(b["l"]), idx(b["h"])
        body0, body1 = sorted((idx(min(b["o"], b["c"])), idx(max(b["o"], b["c"]))))
        v = b.get("v") or 1.0
        cv = b.get("cv") or 0.0
        pv = b.get("pv") or 0.0
        for amount, acc in ((v, tot), (cv, call), (pv, put)):
            if amount <= 0:
                continue
            spread(body0, body1, amount * BODY_SHARE, acc)
            spread(wick0, wick1, amount * (1.0 - BODY_SHARE), acc)

    total = sum(tot)
    if total <= 0:
        return {}

    # Liegt kein Volumen vor, hat oben jede Kerze den Ersatzwert 1
    # beigetragen - dann zaehlt das Profil Zeit je Preis und ist ein
    # TPO-Profil, kein Volumenprofil. Das darf nicht stillschweigend
    # passieren: die beiden Groessen bedeuten Verschiedenes.
    has_volume = any((b.get("v") or 0) > 0 for b in bars)
    basis = kind if has_volume else "time"

    poc_i = max(range(bins), key=lambda i: tot[i])
    # Value Area: vom POC aus zur jeweils volumenstaerkeren Seite wachsen,
    # bis 70 Prozent erreicht sind.
    lo_i = hi_i = poc_i
    acc = tot[poc_i]
    while acc < total * 0.7 and (lo_i > 0 or hi_i < bins - 1):
        down = tot[lo_i - 1] if lo_i > 0 else -1
        up = tot[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += tot[hi_i]
        else:
            lo_i -= 1
            acc += tot[lo_i]

    price = lambda i: lo + (i + 0.5) * step
    peak = max(tot) or 1.0

    # Knoten: lokale Maxima ueber dem Mittel, Vakuum: lokale Minima darunter.
    mean = total / bins
    hvn, lvn = [], []
    win = max(2, bins // 40)
    for i in range(win, bins - win):
        seg = tot[i - win:i + win + 1]
        if tot[i] == max(seg) and tot[i] > mean * 1.4:
            if not hvn or abs(price(i) - hvn[-1]["p"]) > step * win:
                hvn.append({"p": price(i), "w": tot[i] / peak})
        if tot[i] == min(seg) and tot[i] < mean * 0.45 and lo_i < i < hi_i + win:
            if not lvn or abs(price(i) - lvn[-1]["p"]) > step * win:
                lvn.append({"p": price(i), "w": tot[i] / peak})

    return {
        "basis": basis,
        "poc": price(poc_i),
        "vah": price(hi_i),
        "val": price(lo_i),
        "lo": lo, "hi": hi, "step": step, "bins_n": bins,
        "peak": peak,
        "call_total": sum(call), "put_total": sum(put),
        "hvn": hvn[:6], "lvn": lvn[:4],
        "bins": [{
            "p": price(i),
            "w": tot[i] / peak,
            # Anteil der Call-Seite an diesem Band, 0.5 heisst ausgeglichen
            "c": (call[i] / (call[i] + put[i])) if (call[i] + put[i]) > 0 else 0.5,
            "va": lo_i <= i <= hi_i,
        } for i in range(bins)],
    }


def session_levels(bars_1d):
    """Vortages- und Vorwochen-Marken aus Tagesbars."""
    out = {}
    if len(bars_1d) >= 2:
        pd = bars_1d[-2]
        out["pdh"], out["pdl"], out["pdc"] = pd["h"], pd["l"], pd["c"]
    if len(bars_1d) >= 6:
        wk = bars_1d[-6:-1]
        out["pwh"] = max(b["h"] for b in wk)
        out["pwl"] = min(b["l"] for b in wk)
    if bars_1d:
        out["dh"], out["dl"] = bars_1d[-1]["h"], bars_1d[-1]["l"]
    return out


def overnight_range(bars_intraday):
    """Spanne der zuletzt vorhandenen Sitzung.

    Nicht "heute" - bei geschlossener Boerse liefert Cboe die letzte
    abgeschlossene Sitzung, und ein Filter auf das Tagesdatum haette
    dann gar nichts gefunden.
    """
    if not bars_intraday:
        return {}
    last_day = datetime.fromtimestamp(bars_intraday[-1]["t"], timezone.utc).date()
    seg = [b for b in bars_intraday
           if datetime.fromtimestamp(b["t"], timezone.utc).date() == last_day]
    if not seg:
        return {}
    return {"on_high": max(b["h"] for b in seg),
            "on_low": min(b["l"] for b in seg),
            "on_day": last_day.isoformat()}


# --- Bar-Kaskade -----------------------------------------------------------
# Cboe liefert Minutenbars im Preisraum des Index, aber nur die laufende
# Sitzung. Yahoo liefert Historie ueber alle Zeitebenen, drosselt dafuer
# stossweise mit HTTP 429. Die Kaskade nimmt, was da ist.

_TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}


def bars(market_key, interval="15m"):
    """Kerzen fuer einen Markt, mit Quellenangabe.

    Beide Quellen liefern im Preisraum des Index - demselben Raum, in dem
    auch die Optionsstrikes stehen. Deshalb gibt es hier keine Umrechnung
    und damit keinen Basis-Jitter.
    """
    from . import cboe

    conf = MARKETS.get(market_key) or MARKETS[DEFAULT_MARKET]
    ybars, meta = candles(conf["index"], interval)
    if len(ybars) >= 30:
        return ybars, {"source": "yahoo", "symbol": conf["index"],
                       "stale": bool(meta.get("stale"))}

    minutes = _TF_MIN.get(interval, 15)
    if minutes <= 60:
        raw, stale = cboe.intraday(conf["chain"])
        agg = cboe.aggregate(raw, minutes)
        if agg:
            return agg, {"source": "cboe", "symbol": conf["chain"], "stale": stale}
    return ybars, {"source": "yahoo", "symbol": conf["index"],
                   "stale": True, "thin": True}


def profile_bars(market_key, interval=None, index_spot=None):
    """Kerzen fuer das Volumenprofil - aus dem Future, nicht aus dem Index.

    Der Index selbst wird nicht gehandelt und hat kein Volumen. Fuer ein
    Volumenprofil braucht es das gehandelte Volumen, und das liegt im
    Future: NQ=F traegt bei Yahoo das CME-Handelsvolumen je Kerze.

    Gemessen ist der Unterschied deutlich - beim Future betraegt das
    Verhaeltnis von Spitze zu Mittel rund das Zehnfache, beim
    Optionsvolumen der Kette nur das Fuenffache. Bei flachem Gewicht
    bleibt von "Volumen je Preis" faktisch "Zeit je Preis" uebrig, also
    ein TPO-Profil unter falschem Namen.

    Die Future-Preise werden ueber ein einziges Verhaeltnis in den
    Index-Preisraum gehoben, damit Profil, Kerzen und Strikes weiter auf
    derselben Achse liegen.
    """
    conf = MARKETS.get(market_key) or MARKETS[DEFAULT_MARKET]
    sym = conf["chart"]
    # Immer Minutenkerzen, unabhaengig von der angezeigten Zeitebene.
    # Gemessen an derselben Sitzung: mittlere Kerzenspanne 6,7 statt
    # 22,1 Punkte, und die Value Area schrumpft von 97 auf 42 Punkte.
    # Aus 5-Minuten-Kerzen war sie mehr als doppelt so breit, wie sie
    # ist - reine Verschmierung innerhalb der Kerze.
    fut, meta = candles(sym, "1m", ttl=45)
    if not fut or not any((b.get("v") or 0) > 0 for b in fut):
        return [], 1.0, None

    fut_spot = meta.get("regularMarketPrice") or fut[-1]["c"]
    ratio = (index_spot / fut_spot) if (index_spot and fut_spot) else 1.0
    if not (0.8 < ratio < 1.25):
        ratio = 1.0        # unplausibel - lieber ungewandelt als verzerrt

    mapped = [{
        "t": b["t"], "v": b["v"],
        "o": b["o"] * ratio, "h": b["h"] * ratio,
        "l": b["l"] * ratio, "c": b["c"] * ratio,
    } for b in fut]
    return mapped, ratio, sym
