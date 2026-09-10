"""Wand-Verlauf: was eine Wand ueber den Tag hinweg getan hat.

Eine Wand als Linie zu zeichnen sagt, wo sie liegt. Sie als Kette von
Punkten entlang der Zeitachse zu zeichnen sagt zusaetzlich, ob sie
waechst oder zerfaellt - und das ist die Information, die ueber
Halten oder Brechen entscheidet.

Cboe liefert immer nur den aktuellen Bestand, keine Historie. Der
Verlauf muss also selbst mitgeschrieben werden. Genau dafuer ist die
Datenbank da: ohne sie waere die Kette bei jedem Neustart wieder leer.

Der Kommentar an `_wall_trail()` im Original beschreibt dasselbe
Prinzip: jede Zeile traegt ihren eigenen Zeitstempel, und was zwischen
zwei Stuetzstellen geschah, wissen wir schlicht nicht.
"""

import os
import time
import threading
from datetime import datetime, timezone

from .store import Store

BUCKET = int(os.environ.get("TRAIL_BUCKET", "120"))     # Sekunden je Stuetzstelle
KEEP_HOURS = int(os.environ.get("TRAIL_KEEP_HOURS", "12"))
MAX_TRACKS = 24


def _bucket(ts=None):
    ts = ts or time.time()
    return int(ts - (ts % BUCKET))


def track_id(w):
    """Eine Spur ist ein Strike einer Kette auf einer Seite."""
    return f"{w['chain']}|{w['side']}|{w['strike']:.2f}"


class WallTrail:
    def __init__(self):
        self.lock = threading.Lock()
        self._stores = {}
        self._cache = {}

    def _store(self, market):
        if market not in self._stores:
            self._stores[market] = Store(f"wall_trail:{market}",
                                         f"wall_trail_{market}.json")
        return self._stores[market]

    def _state(self, market):
        if market not in self._cache:
            self._cache[market] = self._store(market).load({"tracks": {}}) or {"tracks": {}}
            self._cache[market].setdefault("tracks", {})
        return self._cache[market]

    # ------------------------------------------------------------ schreiben
    def record(self, market, walls, spot=None, ts=None):
        """Haengt den aktuellen Stand an die Spuren an.

        `ts` ist die Uhr des Charts, nicht die Wanduhr. Der Cboe-Feed
        haengt gemessene 15 Minuten zurueck; wuerde hier die reale
        Uhrzeit gestempelt, laegen die Orb-Ketten eine Viertelstunde
        rechts neben den Kerzen, zu denen sie gehoeren.
        """
        if not walls:
            return
        with self.lock:
            st = self._state(market)
            b = _bucket(ts)
            cutoff = (ts or time.time()) - KEEP_HOURS * 3600
            changed = False

            for w in walls:
                tid = track_id(w)
                tr = st["tracks"].setdefault(tid, {
                    "chain": w["chain"], "side": w["side"],
                    "strike": w["strike"], "kind": w.get("chain_kind"),
                    "points": [],
                })
                pts = tr["points"]
                # Je Bucket eine Stuetzstelle; ein zweiter Lauf im selben
                # Fenster aktualisiert sie, statt eine Dublette zu legen.
                # Was je Stuetzstelle festgehalten wird - und warum:
                #
                #   oi  steht waehrend der Sitzung still. Die OCC rechnet
                #       ihn ueber Nacht; gemessen an einem 0DTE-Call mit
                #       731 Kontrakten Tagesumsatz und Open Interest null
                #       ist das kein Zweifelsfall. Er beschreibt das
                #       Fundament der Wand, nicht ihr Leben.
                #   g   Dollar-Gamma. Laeuft live mit, weil Spot und
                #       implizite Vola laufen.
                #   v   gehandeltes Volumen am Strike, kumuliert ueber den
                #       Tag. Erst die Differenz zweier Stuetzstellen sagt,
                #       was in diesen zwei Minuten wirklich geschah - und
                #       das ist die einzige Groesse hier, die einen
                #       Zeitpunkt beschreibt statt eines Bestands.
                snapshot = {"p": round(w["price"], 2), "oi": w["oi"],
                            "g": round(w["gex"], 1),
                            "v": round(w.get("vol") or 0.0)}
                if pts and pts[-1]["t"] == b:
                    pts[-1].update(snapshot)
                else:
                    pts.append({"t": b, **snapshot})
                    changed = True
                tr["points"] = [q for q in pts if q["t"] >= cutoff]
                tr["last"] = b

            # Verwaiste Spuren fallen lassen, sonst waechst der Datensatz
            # mit jedem Strike, der einmal kurz in den Rang gerutscht ist.
            st["tracks"] = {k: v for k, v in st["tracks"].items() if v["points"]}
            if len(st["tracks"]) > MAX_TRACKS:
                ranked = sorted(st["tracks"].items(),
                                key=lambda kv: max(q["oi"] for q in kv[1]["points"]),
                                reverse=True)
                st["tracks"] = dict(ranked[:MAX_TRACKS])

            if changed:
                self._store(market).save(st)

    # -------------------------------------------------------------- lesen
    def series(self, market, active_only=True):
        """Spuren fuer die Darstellung: je Spur Punkte mit Zeit und Staerke."""
        with self.lock:
            st = self._state(market)
            # Frische gegen die juengste aufgezeichnete Stuetzstelle messen,
            # nicht gegen die Wanduhr - sonst gilt der gesamte Verlauf als
            # veraltet, sobald der Feed nachhinkt.
            b = max((tr.get("last", 0) for tr in st["tracks"].values()), default=0)
            out = []
            peak = 0.0
            for tid, tr in st["tracks"].items():
                if not tr["points"]:
                    continue
                # Nur Spuren, die im aktuellen Fenster noch leben
                if active_only and b - tr.get("last", 0) > BUCKET * 6:
                    continue
                peak = max(peak, max(q["oi"] for q in tr["points"]))
                out.append({
                    "id": tid, "chain": tr["chain"], "side": tr["side"],
                    "strike": tr["strike"], "kind": tr.get("kind"),
                    "points": tr["points"],
                    "now": tr["points"][-1],
                    "span": tr["points"][-1]["t"] - tr["points"][0]["t"],
                })
            out.sort(key=lambda t: t["now"]["oi"], reverse=True)
            return {"tracks": out, "peak_oi": peak, "bucket": BUCKET}


TRAIL = WallTrail()
