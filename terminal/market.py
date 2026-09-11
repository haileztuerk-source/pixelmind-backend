"""Kursdaten aus Yahoo Finance.

Ersetzt die MT5-Anbindung des lokalen ChartTerminals: dieselben Groessen
(Bars, ATR, Volumenprofil, Session-Extreme), nur aus einer Quelle, die
von einem Server aus erreichbar ist. Kein API-Key noetig.
"""

import time
import threading
from datetime import datetime, timedelta, timezone

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# Zwei gleichwertige Hosts. Yahoo drosselt einzelne Adressen stossweise mit
# HTTP 429; der zweite Host faengt das in der Regel ab.
HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
# So lange nach einem Fehlschlag gar nicht erst fragen. Zwei Minuten sind
# lang genug, dass eine Drosselung abklingt, und kurz genug, dass die
# Historie nach einer Stoerung zeitnah zurueckkommt.
DOWN_QUIET = 120.0
_down_until = 0.0
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

    # Kurzschluss: nach einem Fehlschlag eine Weile gar nicht erst fragen.
    #
    # Der Cache merkte sich nur Erfolge. Yahoo drosselt aber stossweise
    # mit HTTP 429, und dann kostete JEDER Aufruf erneut den vollen
    # Weg ueber beide Hosts - gemessen rund zwei Sekunden, die nichts
    # einbringen. Beim Start zahlte die App das einmal, bei jedem
    # Wechsel der Zeitebene noch einmal, und das war der Grossteil der
    # Wartezeit.
    #
    # Ein alter Stand aus dem Cache ist waehrend der Sperre besser als
    # zwei Sekunden Warten auf nichts; gibt es keinen, faellt der
    # Aufrufer auf das Cboe-CDN zurueck, das nicht drosselt.
    global _down_until
    if now < _down_until:
        return (hit[1], True) if hit else (None, True)

    last_err = None
    for attempt, host in enumerate(HOSTS):
        try:
            r = requests.get(host + path, params=params,
                             headers={"User-Agent": UA}, timeout=20)
            if r.status_code == 429:
                # KEIN Warten vor dem naechsten Host. Gemessen antwortet
                # jeder der beiden in 0,4 Sekunden mit 429, die Pausen
                # dazwischen summierten sich aber auf 1,2 - mehr als die
                # Abfragen selbst. Und sie halfen nicht: der zweite Host
                # ist ein anderer Server, seine Drosselung klingt nicht
                # ab, weil man vier Zehntel gewartet hat. Wenn beide
                # drosseln, uebernimmt der Kurzschluss oben.
                last_err = "429"
                continue
            if not r.ok:
                last_err = str(r.status_code)
                continue
            data = r.json()
            with _lock:
                _cache[key] = (now, data)
                _down_until = 0.0            # Yahoo antwortet wieder
            return data, False
        except Exception as exc:
            last_err = str(exc)

    # Beide Hosts verweigert - fuer DOWN_QUIET Sekunden nicht mehr fragen.
    with _lock:
        _down_until = now + DOWN_QUIET
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


def volume_profile(bars, bins=1000, kind="volume"):
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

    # Bezugsgroesse fuer die Balkenbreite: NICHT das Maximum, sondern das
    # 99. Perzentil der belegten Baender.
    #
    # Der Grund ist gemessen. Je feiner die Aufloesung, desto spitzer
    # wird die staerkste einzelne Zeile - und weil alle anderen an ihr
    # normiert werden, schrumpft das ganze Profil mit. Bei 180 Baendern
    # liegt das mittlere Band bei 10,4 Prozent der Spitze, bei 1000 nur
    # noch bei 4,7. Das Profil war bei voller Aufloesung sichtbar
    # duenner als bei grober, obwohl es mehr Information trug: 99
    # Prozent der Baender draengten sich in das untere Drittel der
    # verfuegbaren Breite, die oberen zwei Drittel blieben fuer einen
    # einzigen Ausreisser reserviert.
    #
    # Gegen das Perzentil normiert nutzt die Breite wieder ihren ganzen
    # Bereich. Das oberste Prozent laeuft dabei an den Anschlag - beim
    # POC ist das kein Verlust, denn dass er der staerkste Bereich ist,
    # sagt ohnehin seine eigene Linie. `peak` bleibt als Rohwert erhalten.
    nz = sorted(x for x in tot if x > 0)
    ref = nz[min(len(nz) - 1, int(len(nz) * 0.99))] if nz else peak
    ref = max(ref, peak * 0.05) or 1.0
    wgt = lambda x: min(1.0, x / ref)

    # Knoten: lokale Maxima ueber dem Mittel, Vakuum: lokale Minima darunter.
    mean = total / bins
    hvn, lvn = [], []
    win = max(2, bins // 40)
    for i in range(win, bins - win):
        seg = tot[i - win:i + win + 1]
        if tot[i] == max(seg) and tot[i] > mean * 1.4:
            if not hvn or abs(price(i) - hvn[-1]["p"]) > step * win:
                hvn.append({"p": price(i), "w": wgt(tot[i])})
        if tot[i] == min(seg) and tot[i] < mean * 0.45 and lo_i < i < hi_i + win:
            if not lvn or abs(price(i) - lvn[-1]["p"]) > step * win:
                lvn.append({"p": price(i), "w": wgt(tot[i])})

    # Uebertragung als parallele Ganzzahl-Reihen statt als Liste von
    # Objekten. Bei 180 Baendern war der Unterschied gleichgueltig, bei
    # 1000 ist er es nicht: ein Objekt je Band mit ausgeschriebenem
    # Preis, Gewicht, Call-Anteil und Value-Area-Flag kostet gemessen
    # 89 KB je Abruf. Denselben Inhalt tragen zwei Zahlenreihen in rund
    # 8 KB - der Preis folgt aus `lo` und `step`, die Value Area aus
    # ihren beiden Randindizes.
    #
    # Aufloesung der Reihen: Gewicht in Promille der Spitze, Call-Anteil
    # in Prozent. Beides feiner, als ein Bildschirm darstellen kann -
    # ein Band ist auf dem Telefon knapp zwei Geraetepixel breit.
    return {
        "basis": basis,
        "poc": price(poc_i),
        "vah": price(hi_i),
        "val": price(lo_i),
        "lo": lo, "hi": hi, "step": step, "bins_n": bins,
        "peak": peak,
        "call_total": sum(call), "put_total": sum(put),
        "hvn": hvn[:6], "lvn": lvn[:4],
        # Gewicht je Band, 0..1000 = Anteil an der staerksten Zeile
        "w": [int(round(wgt(tot[i]) * 1000)) for i in range(bins)],
        # Anteil der Call-Seite je Band in Prozent, 50 heisst ausgeglichen
        "cs": [int(round(call[i] / (call[i] + put[i]) * 100))
               if (call[i] + put[i]) > 0 else 50 for i in range(bins)],
        "va0": lo_i, "va1": hi_i,
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
    # Gruppiert nach dem NEW YORKER Datum, nicht dem UTC-Datum. Eine
    # Handelssitzung ist ein New Yorker Tag; in UTC gerechnet zerfaellt
    # sie, sobald erweiterte Handelszeiten dabei sind - die laufen bis
    # 20 Uhr Ortszeit und damit im Winter ueber Mitternacht UTC hinaus.
    from .cboe import NY
    zone = NY or timezone.utc
    tag = lambda t: datetime.fromtimestamp(t, zone).date()
    last_day = tag(bars_intraday[-1]["t"])
    seg = [b for b in bars_intraday if tag(b["t"]) == last_day]
    if not seg:
        return {}
    return {"on_high": max(b["h"] for b in seg),
            "on_low": min(b["l"] for b in seg),
            "on_day": last_day.isoformat()}


def us_session(now=None):
    """Zustand der US-Regelsitzung, in New Yorker Zeit gerechnet.

    Der Grund fuer diese Funktion ist eine Rueckmeldung, die genau
    richtig war: "der Kurs laeuft nicht live". Um 12:33 deutscher Zeit
    laeuft er tatsaechlich nicht - die US-Boerse oeffnet erst um 15:30.
    Ein Terminal, das dann einfach stillsteht, sieht aus wie eines, das
    kaputt ist. Es soll stattdessen sagen, woran es liegt.

    Feiertage kennt die Funktion NICHT - dafuer gibt es keine freie
    Quelle, und einen Kalender zu raten waere schlechter als zu
    schweigen. Sie sagt deshalb nur, was aus Wochentag und Uhrzeit
    folgt; ob wirklich gehandelt wird, verraet das Alter der Balken,
    und das steht daneben.
    """
    from .cboe import NY, _ny
    zone = NY or timezone.utc
    jetzt = now or datetime.now(timezone.utc)
    et = jetzt.astimezone(zone) if NY else _ny(jetzt.replace(tzinfo=None))
    auf, zu = 9 * 60 + 30, 16 * 60
    minute = et.hour * 60 + et.minute
    werktag = et.weekday() < 5
    offen = werktag and auf <= minute < zu

    # Naechste Grenze: bis wann noch offen, oder ab wann wieder.
    if offen:
        grenze = et.replace(hour=16, minute=0, second=0, microsecond=0)
    else:
        grenze = et.replace(hour=9, minute=30, second=0, microsecond=0)
        if werktag and minute < auf:
            pass                                  # heute frueh, oeffnet noch
        else:
            grenze += timedelta(days=1)
            while grenze.weekday() >= 5:
                grenze += timedelta(days=1)
    # Wie lange laeuft die Sitzung schon? Das entscheidet, ob fehlende
    # frische Balken ein Befund sind oder der Normalfall.
    #
    # Cboe liefert mit einer Viertelstunde Abstand. In den ersten
    # Minuten nach der Eroeffnung gibt es deshalb ZWANGSLAEUFIG noch
    # keinen Balken von heute - das ist die Verzoegerung, kein Ausfall.
    # Ohne diesen Wert meldete die Oberflaeche um Punkt 15:30 "keine
    # frischen Kurse seit 1065 min" und sah aus wie ein Fehler,
    # waehrend alles seinen Gang ging.
    seit = None
    if offen:
        auf_heute = et.replace(hour=9, minute=30, second=0, microsecond=0)
        seit = int((et - auf_heute).total_seconds())
    return {
        "open": offen,
        "et": et.strftime("%H:%M"),
        "next_ts": int(grenze.timestamp()),
        # Was next_ts BEDEUTET. Bei offener Boerse ist es der Schluss,
        # bei geschlossener die naechste Eroeffnung. Die Oberflaeche
        # schrieb blind "oeffnet" davor und behauptete damit um 15:30,
        # die Boerse oeffne um 22 Uhr - das ist der Handelsschluss.
        "next_is": "close" if offen else "open",
        "since_open": seit,
        "delay_min": 15,
        "opens_at": "15:30",                      # nur zur Anzeige, ET 9:30
    }


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
    else:
        dbars, src = _tagesbalken(market_key, conf)
        if dbars:
            return dbars, src
    return ybars, {"source": "yahoo", "symbol": conf["index"],
                   "stale": True, "thin": True}


def _tagesbalken(market_key, conf):
    """Tagesbalken ohne Yahoo.

    Die Tagesebene hing als einzige allein an Yahoo, und Yahoo drosselt:
    in der Produktion gemessen null Kerzen, der Knopf "1d" tat nichts.
    Das Cboe-CDN fuehrt eine freie Historie - fuer _SPX bis 1975 zurueck.

    Fuer den Nasdaq gibt es sie nicht: die Nasdaq gibt ihren Indexverlauf
    nicht frei weiter, _NDX antwortet mit 403. Dafuer liegt QQQ dort, und
    QQQ in den Indexraum zu heben ist in diesem Terminal kein Notbehelf,
    sondern der eingefuehrte Weg - das Volumenprofil tut seit jeher genau
    das.

    Das Verhaeltnis wird aus DERSELBEN Sitzung genommen, Index und ETF am
    selben Tag. Aus verschiedenen Tagen gemessen traegt es die
    Tagesbewegung des ETF als Fehler in jeden Balken der Historie - bei
    QQQ waeren das am Stichtag 1,1 Prozent gewesen.

    Was dieser Weg NICHT kann: die Dividende. Der ETF schuettet aus, der
    Preisindex nicht, also driftet das Verhaeltnis ueber die Jahre - ueber
    zwei Jahrzehnte rund drei Prozent. Deshalb reicht die Reihe nur drei
    Jahre zurueck, und die Quelle steht als "QQQ x 41,1" in der Zeile.
    """
    from . import cboe

    dbars, stale = cboe.historical(conf["chain"])
    if dbars:
        return dbars, {"source": "cboe", "symbol": conf["chain"], "stale": stale}

    etf = ETFS.get(market_key)
    if not etf:
        return [], {}
    ebars, stale = cboe.historical(etf)
    idx, _s1 = cboe.intraday(conf["chain"])
    eint, _s2 = cboe.intraday(etf)
    if not (ebars and idx and eint):
        return [], {}
    ref = eint[-1]["c"]
    spot = idx[-1]["c"]
    if not (ref and spot) or ref <= 0 or spot <= 0:
        return [], {}
    ratio = spot / ref
    # Plausibilitaet: der Verlauf muss nach dem Heben dort liegen, wo der
    # Index steht. Ein Verhaeltnis aus zwei unpassenden Reihen faellt hier
    # auf, statt einen um Faktoren verschobenen Chart zu zeichnen.
    if not (0.5 < (ebars[-1]["c"] * ratio) / spot < 2.0):
        return [], {}
    out = [{**b, "o": b["o"] * ratio, "h": b["h"] * ratio,
            "l": b["l"] * ratio, "c": b["c"] * ratio} for b in ebars]
    return out, {"source": "cboe", "symbol": f"{etf} x{ratio:.1f}",
                 "stale": stale, "lifted": True}


ETFS = {"NQ": "QQQ", "ES": "SPY", "YM": "DIA", "RTY": "IWM", "GC": "GLD"}


def profile_bars(market_key, interval=None, index_spot=None):
    """Kerzen mit echtem gehandeltem Volumen, in den Index-Preisraum gehoben.

    Der Index selbst wird nicht gehandelt und hat kein Volumen. Ein
    Volumenprofil braucht aber gehandeltes Volumen, sonst zaehlt es nur
    Zeit je Preis und ist ein TPO unter falschem Namen.

    Es gibt zwei Quellen dafuer, und beide sind echtes Tapevolumen:

    1. Der Future (NQ=F) bei Yahoo - das CME-Kontraktvolumen. Die erste
       Wahl, weil der Future rund um die Uhr laeuft und die Nacht
       mitnimmt.
    2. Der ETF (QQQ) beim Cboe-CDN - Feld `stock_volume`, das echte
       Stueckvolumen der Minute an den Aktienboersen. Rund 350.000
       Stueck in der Eroeffnungsminute, minuetlich aufgeloest.

    Der zweite Weg ist kein Notbehelf, sondern nur ein anderer
    Handelsplatz desselben Basiswerts - und er ist der belastbarere
    Zugang: Yahoo drosselt Serverabfragen mit HTTP 429, das Cboe-CDN
    nicht. Faellt der Future aus, aendert sich also die Boerse, nicht die
    Art der Groesse.

    Beide Preisreihen werden ueber ein einziges gemessenes Verhaeltnis in
    den Index-Preisraum gehoben, damit Profil, Kerzen und Strikes auf
    derselben Achse liegen.
    """
    key = market_key if market_key in MARKETS else DEFAULT_MARKET
    conf = MARKETS[key]

    def mapped(bars, ref, sym, kind):
        """Hebt eine Kursreihe ueber ein Verhaeltnis in den Index-Preisraum.

        Das Verhaeltnis wird nicht gegen feste Baender geprueft. Ein
        erster Versuch tat das - und liess nur NQ durch: NDX zu QQQ ist
        rund 41, SPX zu SPY aber rund 10, DJX zu DIA rund 1. Die Baender
        beschrieben also nicht "plausibel", sondern "Nasdaq", und ES und
        RTY fielen still auf das Optionsvolumen zurueck.

        Geprueft wird stattdessen, was wirklich schiefgehen kann: ein
        fehlender oder eingefrorener Kurs. Beide Preise muessen positiv
        sein, und die Tagesspanne der Reihe muss die eines Handelstages
        sein - nicht null (eingefroren) und nicht ein Vielfaches
        (falsches Symbol).
        """
        if not (index_spot and ref) or index_spot <= 0 or ref <= 0:
            return [], 1.0, None, None
        span = (max(b["h"] for b in bars) - min(b["l"] for b in bars)) / ref
        if not (0.0002 < span < 0.25):
            return [], 1.0, None, None
        ratio = index_spot / ref
        out = [{
            "t": b["t"], "v": b["v"], "cv": b.get("cv", 0.0), "pv": b.get("pv", 0.0),
            "o": b["o"] * ratio, "h": b["h"] * ratio,
            "l": b["l"] * ratio, "c": b["c"] * ratio,
        } for b in bars]
        return out, ratio, sym, kind

    # Immer Minutenkerzen, unabhaengig von der angezeigten Zeitebene.
    # Gemessen an derselben Sitzung: mittlere Kerzenspanne 6,7 statt
    # 22,1 Punkte, und die Value Area schrumpft von 97 auf 42 Punkte.
    # Aus 5-Minuten-Kerzen war sie mehr als doppelt so breit, wie sie
    # ist - reine Verschmierung innerhalb der Kerze.
    sym = conf["chart"]
    fut, meta = candles(sym, "1m", ttl=45)
    if fut and any((b.get("v") or 0) > 0 for b in fut):
        ref = meta.get("regularMarketPrice") or fut[-1]["c"]
        return mapped(fut, ref, sym, "cme")

    # Der ETF: Stueckvolumen statt Kontraktvolumen, gleiche Aussage.
    from . import cboe
    etf = ETFS.get(key)
    if etf:
        raw, _stale = cboe.intraday(etf)
        raw = [b for b in raw if (b.get("sv") or 0) > 0]
        if raw:
            # Breite des Bandes aus dem gehandelten Stueckvolumen, die
            # Faerbung weiter aus dem Call-Put-Verhaeltnis der Optionen.
            # Die Faerbung ist je Band ein Anteil, kein Absolutwert -
            # die beiden Groessen muessen daher nicht dieselbe Einheit
            # haben, und keine wird in die andere umgedeutet.
            bars = [dict(b, v=b["sv"]) for b in raw]
            return mapped(bars, bars[-1]["c"], etf, "etf")

    return [], 1.0, None, None


def tpo_profile(bars, bins=180, bracket_min=30):
    """Market Profile: zaehlt Zeit je Preis, nicht Volumen.

    Das ist die eigentliche TPO-Rechnung und etwas anderes als ein
    Volumenprofil. Die Sitzung wird in Perioden geteilt (klassisch 30
    Minuten). Fuer jede Periode wird vermerkt, WELCHE Preise beruehrt
    wurden - jeder Preis genau einmal je Periode, unabhaengig davon, wie
    oft oder mit wie viel Volumen er gehandelt wurde.

    Ein Preis, an dem der Markt in acht Perioden war, traegt also acht
    TPOs; ein Preis, durch den er in einer Periode nur durchgerauscht
    ist, genau einen. Daraus folgt der Kern der Aussage: das Profil misst
    Akzeptanz ueber die Zeit, nicht Umsatz.

    Single Prints - Preise mit genau einer Periode - sind die Stellen, an
    denen der Markt ohne Gegenwehr durchgelaufen ist.
    """
    if not bars:
        return {}
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    if hi <= lo:
        return {}
    step = (hi - lo) / bins
    width = bracket_min * 60

    # Je Periode die Menge der beruehrten Bins - eine Menge, kein Zaehler:
    # mehrfaches Beruehren innerhalb derselben Periode zaehlt einmal.
    brackets = {}
    for b in bars:
        key = int(b["t"] // width)
        touched = brackets.setdefault(key, set())
        i0 = max(0, min(bins - 1, int((b["l"] - lo) / step)))
        i1 = max(0, min(bins - 1, int((b["h"] - lo) / step)))
        touched.update(range(i0, i1 + 1))

    order = sorted(brackets.keys())
    counts = [0] * bins
    per_bin = [[] for _ in range(bins)]
    for pos, key in enumerate(order):
        for i in brackets[key]:
            counts[i] += 1
            per_bin[i].append(pos)

    total = sum(counts)
    if total <= 0:
        return {}

    poc_i = max(range(bins), key=lambda i: counts[i])
    lo_i = hi_i = poc_i
    acc = counts[poc_i]
    while acc < total * 0.7 and (lo_i > 0 or hi_i < bins - 1):
        down = counts[lo_i - 1] if lo_i > 0 else -1
        up = counts[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += counts[hi_i]
        else:
            lo_i -= 1
            acc += counts[lo_i]

    price = lambda i: lo + (i + 0.5) * step
    peak = max(counts) or 1

    # Initial Balance: die Spanne der ersten beiden Perioden
    ib_lo = ib_hi = None
    if len(order) >= 1:
        first = set()
        for key in order[:2]:
            first |= brackets[key]
        if first:
            ib_lo, ib_hi = price(min(first)), price(max(first))

    return {
        "basis": "tpo",
        "poc": price(poc_i),
        "vah": price(hi_i),
        "val": price(lo_i),
        "lo": lo, "hi": hi, "step": step, "bins_n": bins,
        "brackets": len(order),
        "bracket_min": bracket_min,
        "peak": peak,
        "ib_low": ib_lo, "ib_high": ib_hi,
        "singles": [price(i) for i in range(bins) if counts[i] == 1][:12],
        "bins": [{
            "p": price(i),
            "n": counts[i],
            "w": counts[i] / peak,
            "va": lo_i <= i <= hi_i,
        } for i in range(bins) if counts[i] > 0],
    }
