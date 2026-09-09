"""Zonenbuch: Level einmal am Tagesanker einfrieren, danach nur fortschreiben.

Das ist die Mechanik aus `_zone_day_book()` / `gex_zone_book.json` des
lokalen Terminals - und der Grund, warum ueberhaupt von *fixen* Waenden
die Rede sein kann. Ohne sie wandert jede Wand mit jedem Snapshot, und
ein Level, das sich staendig verschiebt, laesst sich weder handeln noch
messen.

Zwei Dinge kommen dazu, die im Gameplan-Dokument als Luecke benannt sind:

  Identitaet ueber Tage  Ein Level behaelt seine ID, solange sein Strike
                         wiederkehrt. Damit laeuft der Pruefungszaehler
                         ueber Wochen weiter - die Voraussetzung fuer
                         "siebter Test" und "nie drin gewesen".

  Anti-Zappel-Regel      Ein an der Kante zappelnder Kurs erzeugt EINE
                         Beruehrungs-Episode, nicht dreissig.
"""

import os
import json
import threading
from datetime import datetime, timezone, timedelta

# Tagesanker in UTC. Im Original 6 Uhr Serverzeit; hier UTC, weil der
# ganze Dienst in UTC rechnet und Cboe seine Ketten so ausliefert.
ANCHOR_HOUR = int(os.environ.get("ANCHOR_HOUR", "6"))
BOOK_PATH = os.environ.get("BOOK_STATE", "gex_zone_book.json")

TOUCH_GAP = 600           # Sekunden zwischen zwei Episoden am selben Level
TOUCH_ATR = 0.05          # Beruehrung: so nah muss der Kurs an das Level
BREAK_ATR = 0.15          # Bruch: so weit muss er darueber hinaus
HOLD_ATR = 0.30           # Gehalten: so weit muss er zurueck

# Welche Groessen eingefroren werden. Reihenfolge bestimmt die Anzeige.
FROZEN = [
    ("call_wall", "Call-Wand"), ("call_wall_2", "Call C2"), ("call_wall_3", "Call C3"),
    ("put_wall", "Put-Wand"), ("put_wall_2", "Put P2"), ("put_wall_3", "Put P3"),
    ("flip", "Zero-Gamma"), ("max_pain", "Max Pain"), ("gamma_pin", "Gamma-Pin"),
]


def trading_day(now=None):
    """Handelstag laut Anker: vor dem Anker zaehlt noch der Vortag."""
    now = now or datetime.now(timezone.utc)
    ref = now if now.hour >= ANCHOR_HOUR else now - timedelta(days=1)
    return ref.strftime("%Y-%m-%d")


def _extract(snap):
    """Zieht die einzufrierenden Preise flach aus einem Snapshot."""
    cw = snap.get("call_walls") or []
    pw = snap.get("put_walls") or []
    return {
        "call_wall": cw[0]["price"] if len(cw) > 0 else None,
        "call_wall_2": cw[1]["price"] if len(cw) > 1 else None,
        "call_wall_3": cw[2]["price"] if len(cw) > 2 else None,
        "put_wall": pw[0]["price"] if len(pw) > 0 else None,
        "put_wall_2": pw[1]["price"] if len(pw) > 1 else None,
        "put_wall_3": pw[2]["price"] if len(pw) > 2 else None,
        "flip": snap.get("flip"),
        "max_pain": snap.get("max_pain"),
        "gamma_pin": snap.get("gamma_pin"),
    }


class DayBook:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"markets": {}}
        self._load()

    def _load(self):
        try:
            with open(BOOK_PATH, "r", encoding="utf-8") as fh:
                self.state = json.load(fh)
        except Exception:
            pass
        self.state.setdefault("markets", {})

    def _save(self):
        try:
            tmp = BOOK_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
            os.replace(tmp, BOOK_PATH)
        except Exception:
            pass

    def _market(self, key):
        m = self.state["markets"].setdefault(key, {})
        m.setdefault("day", None)
        m.setdefault("levels", [])
        m.setdefault("history", {})     # ID -> {born, exams}
        m.setdefault("anchor", {})
        return m

    # ------------------------------------------------------------ Einfrieren
    def _freeze(self, key, snap, day):
        """Legt das Buch fuer einen Handelstag an."""
        m = self._market(key)
        prices = _extract(snap)
        spot = snap.get("spot")
        levels = []

        for name, label in FROZEN:
            p = prices.get(name)
            if not p:
                continue
            # Stabile ID: Art plus Strike. Kehrt derselbe Strike morgen
            # wieder, erbt das Level seine Geschichte.
            lid = f"{name}:{round(p)}"
            hist = m["history"].setdefault(lid, {"born": day, "exams": []})
            levels.append({
                "id": lid, "kind": name, "label": label,
                "price": p,
                "side": "above" if spot and p > spot else "below",
                "state": "untested",
                "touches": 0,
                "last_touch": None,
                "born": hist["born"],
                "exams_total": len(hist["exams"]),
            })

        m["day"] = day
        m["levels"] = levels
        m["anchor"] = {
            "spot": spot, "atr": snap.get("atr"), "regime": snap.get("regime"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "net_gex": snap.get("net_gex"),
        }
        return levels

    # ------------------------------------------------------------ Fortschreiben
    def update(self, key, snap):
        """Legt das Buch an oder schreibt es fort. Gibt die Ansicht zurueck."""
        with self.lock:
            m = self._market(key)
            day = trading_day()
            spot = snap.get("spot")
            atr = snap.get("atr") or 0

            if not spot:
                return self._view(key, snap)

            # Qualitaetssperre: bei fauler Quelle das alte Buch stehen
            # lassen, statt ein neues zu wuerfeln.
            if m["day"] != day:
                if snap.get("chain_stale"):
                    m.setdefault("stale_since", day)
                else:
                    self._freeze(key, snap, day)
                    m.pop("stale_since", None)
                    self._save()

            if not m["levels"]:
                return self._view(key, snap)

            now = datetime.now(timezone.utc).timestamp()
            tol_touch = max(atr * TOUCH_ATR, spot * 0.0002)
            tol_break = atr * BREAK_ATR
            tol_hold = atr * HOLD_ATR
            changed = False

            for lv in m["levels"]:
                p, d = lv["price"], spot - lv["price"]

                # Beruehrung mit Abklingzeit gegen Zappeln
                if abs(d) <= tol_touch:
                    last = lv.get("last_touch") or 0
                    if now - last > TOUCH_GAP:
                        lv["touches"] += 1
                        lv["last_touch"] = now
                        if lv["state"] == "untested":
                            lv["state"] = "tested"
                        hist = m["history"].setdefault(
                            lv["id"], {"born": lv["born"], "exams": []})
                        hist["exams"].append({"day": day, "ts": now, "outcome": "open"})
                        lv["exams_total"] = len(hist["exams"])
                        changed = True

                # Bruch: Akzeptanz jenseits der Kante, nicht blosse Beruehrung
                elif lv["state"] in ("tested", "held") and tol_break > 0:
                    broke = (lv["side"] == "above" and d > tol_break) or \
                            (lv["side"] == "below" and -d > tol_break)
                    if broke and lv["state"] != "broken":
                        lv["state"] = "broken"
                        self._close_exam(m, lv, "broken")
                        changed = True

                # Gehalten: deutlich zurueck auf die Ausgangsseite
                if lv["state"] == "tested" and tol_hold > 0:
                    held = (lv["side"] == "above" and -d > tol_hold) or \
                           (lv["side"] == "below" and d > tol_hold)
                    if held:
                        lv["state"] = "held"
                        self._close_exam(m, lv, "held")
                        changed = True

            if changed:
                self._save()
            return self._view(key, snap)

    def _close_exam(self, m, lv, outcome):
        hist = m["history"].get(lv["id"])
        if hist and hist["exams"] and hist["exams"][-1].get("outcome") == "open":
            hist["exams"][-1]["outcome"] = outcome

    # ------------------------------------------------------------ Ansicht
    def _view(self, key, snap):
        """Fixe Level plus Abweichung gegen den laufenden Stand."""
        m = self._market(key)
        live = _extract(snap)
        atr = snap.get("atr") or 0
        out = []
        for lv in m["levels"]:
            now_p = live.get(lv["kind"])
            drift = (now_p - lv["price"]) if now_p else None
            hist = m["history"].get(lv["id"], {})
            exams = hist.get("exams", [])
            out.append({
                **lv,
                "live": now_p,
                "drift": drift,
                "drift_atr": (abs(drift) / atr) if drift and atr else None,
                "exams_total": len(exams),
                "exams_held": sum(1 for e in exams if e.get("outcome") == "held"),
                "exams_broken": sum(1 for e in exams if e.get("outcome") == "broken"),
                "never_inside": len(exams) == 0,
            })
        out.sort(key=lambda x: x["price"], reverse=True)
        return {
            "day": m.get("day"),
            "anchor": m.get("anchor", {}),
            "anchor_hour": ANCHOR_HOUR,
            "stale_since": m.get("stale_since"),
            "levels": out,
        }

    def view(self, key, snap):
        with self.lock:
            return self._view(key, snap)


BOOK = DayBook()
