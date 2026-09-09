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
from datetime import datetime, timezone

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

    Das Volumenfeld traegt beim Index kein Aktienvolumen, sondern das
    gehandelte Optionsvolumen der Minute - fuer ein Volumenprofil sogar
    die aussagekraeftigere Groesse, weil sie den 0DTE-Kampf abbildet.
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
            dt = datetime.fromisoformat(row["datetime"]).replace(tzinfo=timezone.utc)
        except (ValueError, KeyError, TypeError):
            continue
        v = row.get("volume") or {}
        bars.append({
            "t": int(dt.timestamp()),
            "o": float(p.get("open") or p["close"]),
            "h": float(p.get("high") or p["close"]),
            "l": float(p.get("low") or p["close"]),
            "c": float(p["close"]),
            "v": float(v.get("total_options_volume") or 0.0),
            "cv": float(v.get("calls_volume") or 0.0),
            "pv": float(v.get("puts_volume") or 0.0),
        })
    bars.sort(key=lambda b: b["t"])
    return bars, stale


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
                      "c": b["c"], "v": b["v"]}
        else:
            bucket["h"] = max(bucket["h"], b["h"])
            bucket["l"] = min(bucket["l"], b["l"])
            bucket["c"] = b["c"]
            bucket["v"] += b["v"]
    if bucket:
        out.append(bucket)
    return out
