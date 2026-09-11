"""Optionsketten und Intraday-Bars vom offenen Cboe-CDN.

Kein Key, keine Registrierung. Zwei Endpunkte:

  options/<SYM>.json           volle Kette mit OI, Volumen und Cboes
                               eigenen Greeks je Kontrakt
  charts/intraday/<SYM>.json   Minutenbars der laufenden Sitzung

Beide liefern denselben Preisraum wie die Strikes. Genau daran sind im
lokalen Terminal drei Blocker gescheitert (Basis-Jitter, doppelte
Umrechnung, "drei Preise namens NQ") - hier entstehen sie gar nicht erst.

Der Feed ist rund 15 Minuten verzoegert. Fuer Zonen und Waende, die
Stunden halten, ist das folgenlos; fuer Entry-Timing ist es unbrauchbar.
Die Verzoegerung wird im Frontend ausgewiesen statt versteckt.
"""

import re
import time
import threading
from datetime import datetime, timedelta, timezone

# Cboe schreibt die Zeitstempel seiner Minutenbars OHNE Zonenangabe:
# "2026-09-10T09:31:00". Gelesen als UTC ergab das eine Sitzung von 09:31
# bis 15:59 UTC - eine Handelszeit, die es nirgends gibt. In New Yorker
# Zeit sind es 09:31 bis 15:59, also genau die US-Regelsitzung von 9:30
# bis 16:00. Das ist kein Auslegungsspielraum, sondern die Zeitzone.
#
# Der Fehler betrug im Sommer vier Stunden, im Winter fuenf, und er lief
# durch alles hindurch: die Zeitachse las sich um vier Stunden verschoben
# (auf einem deutschen Telefon stand 18 Uhr, wo 22 Uhr hingehoerte), die
# Sitzungserkennung setzte an der falschen Stelle an, und das Zonenbuch
# fror seine Marken zur falschen Zeit ein.
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:                                    # ohne Zonendatenbank
    NY = None


def _ny(naiv):
    """Naive New Yorker Ortszeit in einen Zeitpunkt mit Zone wandeln.

    Ohne zoneinfo faellt die Funktion auf die US-Sommerzeitregel zurueck:
    zweiter Sonntag im Maerz bis erster Sonntag im November, seit 2007
    unveraendert. Das ist kein voller Ersatz fuer die Zonendatenbank,
    aber es ist besser als vier Stunden Fehler - und der Regelfall
    trifft zu, weil tzdata in den Laufzeitumgebungen vorhanden ist.
    """
    if NY is not None:
        return naiv.replace(tzinfo=NY)

    def _sonntag(jahr, monat, n):
        d = datetime(jahr, monat, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)   # erster Sonntag
        return d + timedelta(weeks=n - 1)

    start = _sonntag(naiv.year, 3, 2).replace(hour=2)
    ende = _sonntag(naiv.year, 11, 1).replace(hour=2)
    sommer = start <= naiv < ende
    return naiv.replace(tzinfo=timezone(timedelta(hours=-4 if sommer else -5)))

import requests

BASE = "https://cdn.cboe.com/api/global/delayed_quotes"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Ein Kontraktsymbol endet immer auf 6 Datumsziffern, C oder P und
# 8 Ziffern Strike (mal 1000). Davor steht die Wurzel, die je nach
# Abrechnungsart variiert (NDX, NDXP, SPXW ...).
OSI = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

_cache = {}
_lock = threading.Lock()


def _fetch(path, ttl):
    """Holt einen Cboe-Pfad mit Zeit-Cache; bei Fehlern den alten Stand."""
    now = time.time()
    with _lock:
        hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1], False
    try:
        r = requests.get(f"{BASE}/{path}.json", headers={"User-Agent": UA}, timeout=45)
        if r.ok:
            data = r.json()
            with _lock:
                _cache[path] = (now, data)
            return data, False
    except Exception:
        pass
    if hit:
        return hit[1], True
    return None, True


def parse_contract(sym):
    """Zerlegt ein OSI-Kontraktsymbol. Gibt None bei unbekanntem Format."""
    m = OSI.match(sym or "")
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        exp = datetime(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]),
                       tzinfo=timezone.utc)
    except ValueError:
        return None
    return {
        "root": m.group("root"),
        "expiry": exp,
        "is_call": m.group("cp") == "C",
        "strike": int(m.group("strike")) / 1000.0,
    }


def chain(symbol, ttl=60, max_dte=90):
    """Optionskette als flache Kontraktliste plus Spot.

    Nur Kontrakte mit Restlaufzeit <= max_dte werden zurueckgegeben. Der
    60-Tage-Deckel des Originals hatte die groessten OI-Waende
    abgeschnitten - deshalb hier bewusst weiter gefasst.
    """
    data, stale = _fetch(f"options/{symbol}", ttl)
    if not data:
        return {"spot": None, "contracts": [], "stale": True, "ts": None}

    d = data.get("data", {}) or {}
    spot = d.get("current_price")
    now = datetime.now(timezone.utc)
    out = []
    for o in d.get("options", []) or []:
        meta = parse_contract(o.get("option"))
        if not meta:
            continue
        dte = (meta["expiry"] - now).total_seconds() / 86400.0
        if dte < -1 or dte > max_dte:
            continue
        oi = o.get("open_interest") or 0.0
        vol = o.get("volume") or 0.0
        if oi <= 0 and vol <= 0:
            continue  # tote Strikes tragen nichts bei und kosten nur Rechenzeit
        out.append({
            "strike": meta["strike"],
            "is_call": meta["is_call"],
            "dte": max(dte, 0.0),
            "expiry": meta["expiry"].strftime("%Y-%m-%d"),
            "oi": float(oi),
            "volume": float(vol),
            "gamma": float(o.get("gamma") or 0.0),
            "delta": float(o.get("delta") or 0.0),
            "vega": float(o.get("vega") or 0.0),
            "iv": float(o.get("iv") or 0.0),
            "bid": float(o.get("bid") or 0.0),
            "ask": float(o.get("ask") or 0.0),
            "last": float(o.get("last_trade_price") or 0.0),
            "last_time": o.get("last_trade_time"),
        })
    return {
        "spot": float(spot) if spot else None,
        "contracts": out,
        "stale": stale,
        "ts": data.get("timestamp"),
    }


def intraday(symbol, ttl=45):
    """Minutenbars der laufenden Cboe-Sitzung im Preisraum des Index.

    Die Datei fuehrt zwei verschiedene Volumina, und der Unterschied ist
    fuer das Profil entscheidend:

    `stock_volume` ist gehandeltes Stueckvolumen - beim Index leer, beim
    ETF (QQQ, SPY) das echte Tapevolumen der Minute. Das ist die Groesse,
    die ein Volumenprofil braucht.

    `total_options_volume` ist gehandeltes Optionsvolumen. Es gibt es
    auch beim Index, wo es den 0DTE-Kampf abbildet - aber es ist eine
    andere Aussage und darf nicht stillschweigend an die Stelle des
    ersten treten.

    Deshalb werden beide getrennt weitergereicht: `sv` und `v`.
    """
    data, stale = _fetch(f"charts/intraday/{symbol}", ttl)
    if not data:
        return [], True
    bars = []
    for row in data.get("data", []) or []:
        p = row.get("price") or {}
        if p.get("close") is None:
            continue
        try:
            dt = _ny(datetime.fromisoformat(row["datetime"]))
        except (ValueError, KeyError, TypeError):
            continue
        # Kaputte Kerzen aussortieren. Die Daten enthalten vereinzelt
        # Zeilen mit low = 0 - bei GLD gemessen. Ein Preis von null ist
        # bei einem gehandelten Papier unmoeglich, und eine solche Zeile
        # ist nicht bloss ungenau: Volumenprofil und TPO spannen ihre
        # Skala zwischen Tief und Hoch auf, also zwischen 0 und 406, und
        # das ganze Profil faellt in ein einziges Band zusammen. Eine
        # einzelne Zeile loescht damit die Aussage der uebrigen 388.
        o, h, l, c = (p.get("open"), p.get("high"), p.get("low"), p["close"])
        o, h, l = (o if o else c), (h if h else c), (l if l else c)
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        v = row.get("volume") or {}
        bars.append({
            "t": int(dt.timestamp()),
            "o": o, "h": h, "l": l, "c": c,
            "v": float(v.get("total_options_volume") or 0.0),
            "sv": float(v.get("stock_volume") or 0.0),
            "cv": float(v.get("calls_volume") or 0.0),
            "pv": float(v.get("puts_volume") or 0.0),
        })
    bars.sort(key=lambda b: b["t"])
    return bars, stale


def historical(symbol, ttl=3600, tage=750):
    """Tagesbalken vom Cboe-CDN - der einzige freie Weg zur Historie.

    Die Tagesebene war tot. Sie haengt als einzige an Yahoo, und Yahoo
    drosselt Serverabfragen mit HTTP 429: gemessen lieferte /api/candles
    fuer 1d in der Produktion null Kerzen, und der Knopf tat schlicht
    nichts.

    Dieser Pfad hier liegt auf demselben CDN, das die Ketten und die
    Minutenbars traegt, braucht keinen Schluessel und reicht je nach
    Symbol bis 1975 zurueck. Nicht jedes Symbol ist dabei: _SPX, _DJX,
    _RUT und die ETFs ja, _NDX nicht - die Nasdaq vergibt ihre Historie
    nicht zur freien Weitergabe. Fuer den Nasdaq hebt market.bars()
    deshalb QQQ in den Indexraum, so wie es das Volumenprofil laengst
    tut.

    Der Zeitstempel sitzt auf dem Sitzungsschluss, 16 Uhr New Yorker
    Zeit. Ein Tagesbalken ohne Uhrzeit landete sonst auf Mitternacht
    UTC und damit auf dem falschen Kalendertag.
    """
    data, stale = _fetch(f"charts/historical/{symbol}", ttl)
    if not data:
        return [], True
    reihen = (data.get("data") or [])[-max(1, tage):]
    out = []
    for row in reihen:
        try:
            d = datetime.fromisoformat(row["date"])
            o, h, l, c = (float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]))
        except (ValueError, KeyError, TypeError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        out.append({
            "t": int(_ny(d.replace(hour=16, minute=0)).timestamp()),
            "o": o, "h": h, "l": l, "c": c,
            "v": 0.0, "sv": float(row.get("volume") or 0.0),
            "cv": 0.0, "pv": 0.0,
        })
    out.sort(key=lambda b: b["t"])
    return out, stale


def aggregate(bars, minutes):
    """Fasst Minutenbars zu groesseren Kerzen zusammen."""
    if minutes <= 1 or not bars:
        return bars
    step = minutes * 60
    out, bucket = [], None
    for b in bars:
        slot = b["t"] - (b["t"] % step)
        if bucket is None or bucket["t"] != slot:
            if bucket:
                out.append(bucket)
            bucket = {"t": slot, "o": b["o"], "h": b["h"], "l": b["l"],
                      "c": b["c"], "v": b["v"],
                      # Call- und Put-Volumen getrennt weiterreichen: daraus
                      # entsteht spaeter ein Profil, das nicht nur zeigt WO
                      # gehandelt wurde, sondern auf welcher Seite.
                      "cv": b.get("cv", 0.0), "pv": b.get("pv", 0.0),
                      "sv": b.get("sv", 0.0)}
        else:
            bucket["h"] = max(bucket["h"], b["h"])
            bucket["l"] = min(bucket["l"], b["l"])
            bucket["c"] = b["c"]
            bucket["v"] += b["v"]
            bucket["cv"] += b.get("cv", 0.0)
            bucket["pv"] += b.get("pv", 0.0)
            bucket["sv"] += b.get("sv", 0.0)
    if bucket:
        out.append(bucket)
    return out
