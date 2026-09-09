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
    def record(self, market, walls, spot=None):
        """Haengt den aktuellen Stand an die Spuren an."""
        if not walls:
            return
        with self.lock:
            st = self._state(market)
            b = _bucket()
            cutoff = time.time() - KEEP_HOURS * 3600
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
                if pts and pts[-1]["t"] == b:
                    pts[-1].update({"p": round(w["price"], 2), "oi": w["oi"],
                                    "g": round(w["gex"], 1)})
                else:
                    pts.append({"t": b, "p": round(w["price"], 2),
                                "oi": w["oi"], "g": round(w["gex"], 1)})
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
            b = _bucket()
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
