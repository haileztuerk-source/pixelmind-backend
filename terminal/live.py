"""Live-Kurse aus der Plattform des Nutzers.

Der Terminal holt seine Daten aus oeffentlichen Quellen, und die sind
verzoegert: Cboe liefert 15 Minuten alt, Yahoo zehn. Fuer die Waende ist
das gleichgueltig - Open Interest aendert sich nicht im Sekundentakt.
Fuer den Chart, auf den jemand einen Einstieg setzt, ist es nicht
gleichgueltig.

Einen freien Live-Kurs auf den Nasdaq 100 gibt es nicht: CFD-Kurse
gehoeren dem Broker und kommen aus dessen Feed. Der Nutzer hat diesen
Feed aber bereits - in seiner Handelsplattform. Was fehlt, ist der Weg
von dort zum Server.

Den geht eine Bruecke: ein kleines Programm auf dem Rechner des
Nutzers liest die Plattform aus und schiebt Kerzen und Ticks hierher.
Dieses Modul ist die Gegenseite. Es kennt die Bruecke nicht - es nimmt
entgegen, was hereinkommt, und sagt, wie alt es ist.

Zwei getrennte Dinge kommen herein:

  Kerzen  die Historie des gehandelten Instruments, komplett und in
          dessen eigenem Preisraum. Damit ist der Hauptchart der Chart
          des Nutzers und nicht mehr eine Naeherung daraus.
  Tick    der letzte Kurs, sekuendlich. Er formt die laufende Kerze
          weiter, zwischen zwei Kerzenlieferungen.

Und alles hat ein Verfallsdatum. Ein Live-Feed, der stehenbleibt, ist
gefaehrlicher als gar keiner: er sieht aus wie ein Kurs und ist eine
Erinnerung. Bleibt die Bruecke weg, faellt der Terminal nach STALE_TICK
zurueck auf die oeffentliche Quelle und sagt es.
"""

import os
import threading
import time

# Ab hier gilt ein Tick nicht mehr als lebendig. Grosszuegig genug fuer
# eine Bruecke, die im Sekundentakt sendet, und fuer eine kurze Stoerung
# der Verbindung - knapp genug, dass niemand eine Minute lang einen
# eingefrorenen Kurs fuer einen aktuellen haelt.
STALE_TICK = 20.0
# Kerzen duerfen aelter sein: solange Ticks kommen, wird die laufende
# Kerze daraus weitergeformt.
STALE_BARS = 180.0

MAX_BARS = 3000


def token():
    """Gemeinsames Geheimnis der Bruecke. Ohne das nimmt der Server nichts an.

    Bewusst kein Standardwert. Der Terminal laeuft unter einer
    oeffentlichen Adresse; ohne Pruefung koennte jeder Kerzen
    hineinschreiben und damit den Chart faelschen, auf den jemand einen
    Einstieg setzt. Fehlt das Geheimnis, ist die Annahme geschlossen -
    nicht offen.
    """
    return os.environ.get("LIVE_TOKEN") or ""


class Feed:
    """Der Live-Zustand eines Marktes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._m = {}          # market -> {bars, tick, symbol, at_bars, at_tick}

    def _slot(self, market):
        return self._m.setdefault(market, {
            "bars": [], "tick": None, "symbol": None,
            "at_bars": 0.0, "at_tick": 0.0,
        })

    # ------------------------------------------------------------ schreiben
    def put_bars(self, market, symbol, bars):
        """Nimmt Minutenkerzen der Bruecke entgegen.

        Wird zusammengefuehrt statt ersetzt: die Bruecke schickt in der
        Regel nur das letzte Stueck, und der Terminal soll die laengere
        Historie behalten, die er schon hat.
        """
        clean = []
        for b in bars or []:
            try:
                t = int(b["t"])
                o, h, l, c = (float(b["o"]), float(b["h"]),
                              float(b["l"]), float(b["c"]))
            except (KeyError, TypeError, ValueError):
                continue
            if min(o, h, l, c) <= 0 or h < l:
                continue
            clean.append({"t": t, "o": o, "h": h, "l": l, "c": c,
                          "v": float(b.get("v") or 0.0)})
        if not clean:
            return 0

        with self._lock:
            s = self._slot(market)
            merged = {b["t"]: b for b in s["bars"]}
            merged.update({b["t"]: b for b in clean})
            s["bars"] = sorted(merged.values(), key=lambda b: b["t"])[-MAX_BARS:]
            s["symbol"] = symbol or s["symbol"]
            s["at_bars"] = time.time()
            return len(s["bars"])

    def put_tick(self, market, symbol, price, ts=None):
        """Nimmt den letzten Kurs entgegen und formt die laufende Kerze."""
        try:
            price = float(price)
        except (TypeError, ValueError):
            return False
        if price <= 0:
            return False

        now = time.time()
        ts = int(ts or now)
        with self._lock:
            s = self._slot(market)
            s["tick"] = {"p": price, "t": ts}
            s["symbol"] = symbol or s["symbol"]
            s["at_tick"] = now

            # Die laufende Minute mitfuehren, damit der Chart zwischen
            # zwei Kerzenlieferungen nicht stehenbleibt.
            slot = ts - (ts % 60)
            bars = s["bars"]
            if bars and bars[-1]["t"] == slot:
                b = bars[-1]
                b["h"] = max(b["h"], price)
                b["l"] = min(b["l"], price)
                b["c"] = price
            elif bars and slot > bars[-1]["t"]:
                bars.append({"t": slot, "o": price, "h": price,
                             "l": price, "c": price, "v": 0.0})
                del bars[:-MAX_BARS]
        return True

    # --------------------------------------------------------------- lesen
    def state(self, market):
        """Was gerade da ist, mit Alter - der Aufrufer entscheidet."""
        with self._lock:
            s = self._m.get(market)
            if not s:
                return {"live": False, "reason": "keine Bruecke verbunden"}
            now = time.time()
            age_t = now - s["at_tick"] if s["at_tick"] else None
            age_b = now - s["at_bars"] if s["at_bars"] else None
            live = age_t is not None and age_t < STALE_TICK
            return {
                "live": live,
                "symbol": s["symbol"],
                "price": (s["tick"] or {}).get("p"),
                "tick_age": age_t,
                "bars_age": age_b,
                "bars_n": len(s["bars"]),
                "reason": None if live else (
                    "Bruecke seit %.0f s still" % age_t if age_t is not None
                    else "noch kein Tick"),
            }

    def bars(self, market):
        """Kerzen, wenn sie frisch genug sind - sonst nichts."""
        with self._lock:
            s = self._m.get(market)
            if not s or not s["bars"]:
                return []
            if time.time() - s["at_bars"] > STALE_BARS:
                return []
            return [dict(b) for b in s["bars"]]


    def at(self, market, ts):
        """Kurs des Instruments zu einem bestimmten Zeitpunkt.

        Der Sinn: die Basis gegen den Index laesst sich nur synchron
        messen. Der Indexkurs ist 15 Minuten alt - ihn mit dem
        Live-Kurs zu vergleichen ergaebe nicht die Basis, sondern die
        Basis plus eine Viertelstunde Marktbewegung. Aus den Kerzen der
        Bruecke laesst sich aber ablesen, wo das Instrument in genau
        jener Minute stand.
        """
        with self._lock:
            s = self._m.get(market)
            bars = (s or {}).get("bars") or []
        if not bars:
            return None
        best, dist = None, None
        for b in bars:
            d = abs(b["t"] - ts)
            if dist is None or d < dist:
                best, dist = b, d
        # Mehr als fuenf Minuten daneben ist keine gleichzeitige Messung.
        return best["c"] if (best and dist is not None and dist <= 300) else None


FEED = Feed()
