"""Anzeige im Preisraum des Brokers.

Der Terminal rechnet alles im Preisraum des Index: die Optionsketten
stehen dort, die Waende, das Zero-Gamma, das Zonenbuch. Gehandelt wird
aber ein CFD - USTEC bei GBE, NAS100 anderswo -, und dessen Kurs steht
nicht auf demselben Wert. Ein CFD auf den Nasdaq 100 folgt in der Regel
dem Future, nicht dem Kassaindex, und traegt damit dessen Basis: je nach
Restlaufzeit einige zehn Punkte.

Wer eine Wand bei 29.500 im Terminal sieht und in seiner Plattform auf
29.543 schaut, rechnet bei jedem Blick im Kopf um - und genau dabei
passieren die Fehler, die einen Einstieg kosten.

Deshalb dieser Versatz. Er wird EINMAL gemessen: der Nutzer gibt den
Kurs ein, den seine Plattform gerade zeigt, und daneben steht der
gleichzeitig gemessene Indexkurs. Die Differenz ist der Versatz.

Bewusst nicht gerechnet, sondern gemessen. Die Basis eines Future
haengt an Restlaufzeit, Zinssatz und erwarteten Dividenden; sie liesse
sich schaetzen, aber jeder Broker legt zusaetzlich seinen eigenen
Aufschlag darauf. Was der Nutzer in seiner Plattform sieht, weiss nur
seine Plattform. Eine einzige Ablesung ist genauer als jede Formel.

WICHTIG - der Versatz verschiebt niemals eine Rechnung:

Gespeichert, verglichen und gerechnet wird weiter ausschliesslich im
Index-Preisraum. Der Versatz wirkt allein dort, wo ein Preis zu Text
wird. Das ist kein Detail, sondern die Lehre aus dem Vorgaengerbau: dort
wurde an zwei Stellen umgerechnet, und die Marken zitterten um bis zu
170 Punkte, weil die beiden Rechnungen zu verschiedenen Zeitpunkten
verschiedene Kurse sahen. Eine Groesse wird an genau einer Stelle
umgerechnet, oder sie ist nicht mehr dieselbe Groesse.

Und er verfaellt. Die Basis laeuft zum Verfall hin auf null zu, also
wandert der Versatz ueber Wochen. Deshalb steht sein Alter an der
Anzeige, und ab einem Tag gilt er als nachmessenswert.
"""

import time

from .store import Store

# Ab hier gilt eine Messung als alt genug, um sie nachzuschlagen.
STALE_AFTER = 24 * 3600

# Grenze fuer eine plausible Ablesung. Ein CFD auf denselben Basiswert
# liegt nie weiter als ein Prozent daneben - was darueber liegt, ist ein
# Tippfehler oder der Kurs eines anderen Instruments.
MAX_REL = 0.01


class Space:
    """Der Anzeige-Preisraum je Markt, dauerhaft gespeichert."""

    def __init__(self, path="broker_space.json"):
        self._store = Store("broker_space", path)
        self._cache = None

    def _all(self):
        if self._cache is None:
            self._cache = self._store.load({}) or {}
        return self._cache

    def _save(self):
        self._store.save(self._cache or {})

    def get(self, market_key):
        """Beschreibt, in welchem Preisraum dieser Markt angezeigt wird."""
        rec = self._all().get(market_key)
        if not rec:
            return {"name": None, "offset": 0.0, "at": None,
                    "age": None, "stale": False}
        age = time.time() - (rec.get("at") or 0)
        return {
            "name": rec.get("name"),
            "offset": rec.get("offset", 0.0),
            "at": rec.get("at"),
            "quote": rec.get("quote"),
            "ref": rec.get("ref"),
            "age": age,
            "stale": age > STALE_AFTER,
        }

    def calibrate(self, market_key, name, quote, ref):
        """Misst den Versatz aus einer Ablesung gegen den Indexkurs.

        `quote` ist der Kurs aus der Plattform des Nutzers, `ref` der
        gleichzeitig gemessene Indexkurs. Beide muessen aus demselben
        Moment stammen - deshalb nimmt der Aufrufer `ref` aus dem
        laufenden Snapshot und nicht aus einem eigenen Abruf.
        """
        try:
            quote, ref = float(quote), float(ref)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Kurs nicht lesbar"}
        if quote <= 0 or ref <= 0:
            return {"ok": False, "error": "Kurs muss groesser als null sein"}
        if abs(quote - ref) > ref * MAX_REL:
            return {"ok": False, "error":
                    "%.0f liegt mehr als ein Prozent von %.0f entfernt - "
                    "ist das derselbe Basiswert?" % (quote, ref)}

        self._all()[market_key] = {
            "name": (name or "CFD").strip()[:12],
            "offset": quote - ref,
            "quote": quote, "ref": ref,
            "at": time.time(),
        }
        self._save()
        return {"ok": True, **self.get(market_key)}

    def remember(self, market_key, name, offset, ref, min_gap=60.0):
        """Haelt einen von der Bruecke gemessenen Versatz fest.

        Faellt die Bruecke aus, ist der Broker-Kurs unbekannt - aber der
        Versatz von eben ist immer noch die beste Schaetzung. Ohne dieses
        Gedaechtnis spraengen beim Ausfall alle angezeigten Zahlen um
        mehrere zehn Punkte in den Index-Preisraum zurueck. Wer in dem
        Moment auf den Chart schaut, liest andere Zahlen als eine Sekunde
        vorher, ohne dass sich der Markt bewegt haette - das ist
        gefaehrlicher als eine leicht veraltete Basis.

        Gedrosselt geschrieben: die Bruecke misst im Sekundentakt, aber
        die Basis wandert in Stunden. Jede Sekunde in die Datenbank zu
        schreiben waere reine Last ohne Gewinn.
        """
        rec = self._all().get(market_key) or {}
        if time.time() - (rec.get("at") or 0) < min_gap:
            return
        self._all()[market_key] = {
            "name": (name or "LIVE").strip()[:12],
            "offset": float(offset), "ref": ref,
            "quote": (ref + offset) if ref is not None else None,
            "at": time.time(), "from": "bridge",
        }
        self._save()

    def clear(self, market_key):
        """Zurueck in den Index-Preisraum."""
        self._all().pop(market_key, None)
        self._save()
        return self.get(market_key)


SPACE = Space()
