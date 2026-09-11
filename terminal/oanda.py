"""Live-Kurse aus einem OANDA-Uebungskonto.

WOZU

Die oeffentlichen Quellen sind verzoegert - gemessen, nicht vermutet:
der Cboe-Kurs laeuft seinen eigenen Minutenkerzen nicht voraus, die
Differenz rauscht um null. Ein Live-Kurs auf den Nasdaq gehoert einem
Broker.

Die Bruecke holt ihn beim eigenen Broker ab, verlangt dafuer aber einen
laufenden PC - MetaTrader spricht nur mit dem Programm auf demselben
Rechner. OANDA hat stattdessen eine Schnittstelle, die der SERVER
erreicht. Damit laeuft der Kurs auch dann, wenn zuhause alles aus ist.

Der Preis dafuer ist ehrlich zu benennen: das ist OANDAs CFD, nicht der
des eigenen Brokers. Zwei CFDs auf denselben Index unterscheiden sich um
Spread und Finanzierung - ein paar Punkte, und die sind ueber Stunden
nahezu konstant. Genau dafuer gibt es den gemessenen Versatz im
Preisraum: er wird gegen den Index bestimmt und die Anzeige legt ihn
wieder drauf. Wer seinen eigenen Kurs auf den Punkt will, nimmt die
Bruecke; wer ihn ohne PC will, nimmt das hier.

RANGFOLGE

Die Bruecke gewinnt immer. Laeuft sie, ruehrt dieses Modul den Feed
nicht an - der eigene Broker ist naeher an der Wahrheit als ein
fremder. Faellt sie aus, springt OANDA ein; faellt auch das aus, bleibt
Cboe verzoegert, und die Kopfzeile sagt jedes Mal, was gerade traegt.

EINRICHTEN

Ein Uebungskonto bei OANDA anlegen und dort unter "Manage API Access"
einen Zugangsschluessel erzeugen. Den auf dem Server setzen:

    OANDA_TOKEN = der-schluessel

Mehr nicht - die Kontonummer sucht dieses Modul selbst. Ohne Schluessel
ist es still und aendert nichts.
"""

import os
import threading
import time

import requests

from .live import FEED

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# Die Instrumente je Markt. OANDA fuehrt alle fuenf, und anders als die
# Cboe-Kette laufen sie fast rund um die Uhr - der CFD handelt weiter,
# wenn die US-Kassaboerse laengst zu ist.
INSTRUMENTE = {
    "NQ": "NAS100_USD",
    "ES": "SPX500_USD",
    "YM": "US30_USD",
    "RTY": "US2000_USD",
    "GC": "XAU_USD",
}

TICK_EVERY = 5.0          # Sekunden zwischen zwei Kursabfragen je Markt
BARS_EVERY = 60.0         # Sekunden zwischen zwei Kerzenabfragen je Markt
BARS_COUNT = 500          # Minutenkerzen je Abfrage - gut acht Stunden
TIMEOUT = 12

_lock = threading.Lock()
_account = None           # einmal gesuchte Kontonummer
_last_bars = {}           # markt -> Zeitpunkt der letzten Kerzenabfrage
_status = {"ok": None, "reason": "nicht eingerichtet"}


def token():
    """Der Zugangsschluessel. Ohne ihn bleibt das Modul still.

    Bewusst kein Standardwert und keine Datei im Verzeichnis: der
    Schluessel gehoert in die Umgebung des Dienstes, nicht in die
    Versionsverwaltung.
    """
    return os.environ.get("OANDA_TOKEN") or ""


def host():
    art = (os.environ.get("OANDA_ENV") or "practice").strip().lower()
    return HOSTS.get(art, HOSTS["practice"])


def configured():
    return bool(token())


def status():
    """Was die Anbindung gerade meldet - fuer /health und die Oberflaeche."""
    with _lock:
        return dict(_status, configured=configured(),
                    account=bool(_account))


def _set(ok, reason):
    with _lock:
        _status["ok"] = ok
        _status["reason"] = reason


def _get(pfad, params=None):
    """Eine Abfrage. Fehler werden gemeldet, nicht geworfen.

    Zeitstempel als UNIX-Sekunden statt RFC3339: der Terminal rechnet
    ohnehin in Epochensekunden, und eine Zeichenkette mit Nanosekunden
    muesste erst wieder zerlegt werden.
    """
    t = token()
    if not t:
        return None
    try:
        r = requests.get(
            host() + pfad,
            params=params or {},
            headers={"Authorization": "Bearer " + t,
                     "Accept-Datetime-Format": "UNIX"},
            timeout=TIMEOUT,
        )
        if r.status_code == 401:
            _set(False, "Schluessel abgelehnt")
            return None
        if not r.ok:
            _set(False, "OANDA %d" % r.status_code)
            return None
        return r.json()
    except Exception as exc:
        _set(False, "Netz: %s" % type(exc).__name__)
        return None


def _konto():
    """Die Kontonummer, einmal gesucht und gemerkt.

    Der Kursabruf braucht sie, die Kerzen nicht. Sie abzufragen statt
    abzufragen zu lassen spart dem Nutzer eine zweite Einstellung, bei
    der er sich vertippen koennte.
    """
    global _account
    if _account:
        return _account
    d = _get("/v3/accounts")
    kontos = (d or {}).get("accounts") or []
    if not kontos:
        if d is not None:
            _set(False, "kein Konto zum Schluessel gefunden")
        return None
    _account = kontos[0].get("id")
    return _account


def _zahl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kerzen(instrument, anzahl=BARS_COUNT, takt="M1"):
    """Minutenkerzen als Liste im Format des Terminals.

    Die laufende, noch unfertige Kerze wird mitgenommen: sie ist der
    aktuelle Stand, und genau den soll der Chart zeigen.
    """
    d = _get("/v3/instruments/%s/candles" % instrument,
             {"granularity": takt, "count": int(anzahl), "price": "M"})
    out = []
    for c in (d or {}).get("candles") or []:
        m = c.get("mid") or {}
        t = _zahl(c.get("time"))
        o, h, l, s = (_zahl(m.get("o")), _zahl(m.get("h")),
                      _zahl(m.get("l")), _zahl(m.get("c")))
        if t is None or None in (o, h, l, s):
            continue
        if min(o, h, l, s) <= 0 or h < l:
            continue
        out.append({"t": int(t), "o": o, "h": h, "l": l, "c": s,
                    "v": float(c.get("volume") or 0.0)})
    out.sort(key=lambda b: b["t"])
    return out


def kurs(instrument):
    """Der letzte Kurs als Mitte zwischen Geld und Brief.

    Die Mitte, nicht der Briefkurs: der Chart zeigt den Markt, nicht
    die Seite, auf der man gerade kaufen wuerde. Der Spread gehoert in
    die Ausfuehrung, nicht in die Kerze.
    """
    konto = _konto()
    if not konto:
        return None, None
    d = _get("/v3/accounts/%s/pricing" % konto, {"instruments": instrument})
    for p in (d or {}).get("prices") or []:
        if p.get("instrument") != instrument:
            continue
        if p.get("tradeable") is False:
            _set(True, "Markt geschlossen")
            return None, None
        geld = _zahl((p.get("bids") or [{}])[0].get("price"))
        brief = _zahl((p.get("asks") or [{}])[0].get("price"))
        if geld and brief:
            return (geld + brief) / 2.0, _zahl(p.get("time"))
        mitte = _zahl(p.get("closeoutBid")), _zahl(p.get("closeoutAsk"))
        if all(mitte):
            return sum(mitte) / 2.0, _zahl(p.get("time"))
    return None, None


def _einmal(markt):
    """Ein Durchgang fuer einen Markt: Kerzen bei Bedarf, Kurs immer."""
    inst = INSTRUMENTE.get(markt)
    if not inst:
        return

    # Eigene Kurse haben Vorrang. Laeuft die Bruecke oder cTrader,
    # nicht dazwischenfunken - beide liefern den Kurs des eigenen
    # Brokers, dieser hier den eines fremden. Ein fremder CFD, der den
    # eigenen ueberschreibt, waere ein stiller Rueckschritt.
    st = FEED.state(markt)
    if st.get("live") and st.get("src") in ("bridge", "ctrader"):
        return

    now = time.time()
    if now - _last_bars.get(markt, 0.0) >= BARS_EVERY:
        b = kerzen(inst)
        if b:
            FEED.put_bars(markt, inst, b, src="oanda")
            _last_bars[markt] = now

    p, ts = kurs(inst)
    if p:
        FEED.put_tick(markt, inst, p, ts, src="oanda")
        _set(True, None)


def _lauf(aktive):
    while True:
        try:
            if configured():
                for markt in (aktive() or []):
                    _einmal(markt)
            else:
                _set(None, "nicht eingerichtet")
        except Exception:
            pass          # ein Fehler im Takt darf den Takt nicht beenden
        time.sleep(TICK_EVERY)


def start(aktive):
    """Startet den Abruf im Hintergrund.

    `aktive` liefert die Maerkte, die gerade jemand ansieht. Alle fuenf
    dauernd abzufragen waere Arbeit fuer Charts, die niemand offen hat -
    und auf einem Dienst mit 750 Freistunden im Monat zaehlt das.
    """
    if not configured():
        return False
    threading.Thread(target=_lauf, args=(aktive,), daemon=True).start()
    return True
