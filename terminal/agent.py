"""Terminal Agent: beobachtet lokal, meldet sich bei echten Ereignissen.

Uebernimmt die Architektur des Originals - zwei Taktraten, strikt
getrennt. Der Vergleich alle 20 Sekunden kostet nichts; erst ein echter
Auslöser fuehrt zu einer Meldung.

Der wichtigste Grundsatz stammt aus der Forensik vom 29.07.: der
statische Morgenplan traf 4 von 4, die 13 Live-Fortschreibungen
kassierten ihn an genau den Wendepunkten. Deshalb gilt hier:
**die Engine rechnet, friert ein und zeichnet - Text begruendet nur
bereits feststehende Zahlen.** Ohne Modell-Key entstehen die Meldungen
aus Vorlagen, was diesen Grundsatz sogar strenger einhaelt: es kann
keine Zahl erfunden werden, die nicht aus der Engine kommt.
"""

import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta

import requests

from .store import Store

# Auslöser mit Abklingzeit in Sekunden. Die vier als "major" markierten
# ueberleben auch die Budget-Drosselung.
TRIGGERS = {
    "zone_touch":  {"cooldown": 600,  "major": True},
    "zone_break":  {"cooldown": 600,  "major": True},
    "regime_flip": {"cooldown": 1800, "major": True},
    "calendar":    {"cooldown": 1800, "major": True},
    "zone_new":    {"cooldown": 900,  "major": False},
    "zone_gone":   {"cooldown": 900,  "major": False},
    "wall_move":   {"cooldown": 1800, "major": False},
    "news":        {"cooldown": 300,  "major": False},
}

WALL_MOVE_PCT = 0.003     # 0,3 % - Schwelle aus dem Original
PLAN_HOUR = 8             # lokale Zeit
DAY_BUDGET = int(os.environ.get("AGENT_DAY_BUDGET", "1400"))

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK = os.environ.get("GEMINI_FALLBACK", "gemini-3.1-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

STATE_PATH = os.environ.get("AGENT_STATE", "terminal_agent_state.json")


def _fmt(v, digits=0):
    """Eine Zahl. Fuer ABSTAENDE - ATR, "42 Punkte entfernt", Netto-GEX."""
    if v is None:
        return "—"
    return f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _px(v, snap, digits=0):
    """Ein PREIS, im Preisraum der Anzeige.

    Der Agent schreibt Text, und Text wird nicht mehr umgerechnet - er
    liegt fertig im Verlauf. Also muss der Versatz schon hier hinein,
    sonst nennt der Agent andere Zahlen als der Chart daneben. Genau das
    war der Fall: der Agent schrieb "Zero-Gamma liegt bei 29.134",
    waehrend der Chart 29.203 zeigte - die 59 Punkte Versatz.

    Getrennt von _fmt() aus demselben Grund wie px() von fmt() im
    Frontend: in einem Abstand kuerzt sich der Versatz heraus. "Der Kurs
    steht 20 Punkte ueber dem Zero-Gamma" bleibt wahr, egal in welchem
    Preisraum gemessen wird.

    Der Versatz des Augenblicks wird mit eingefroren, und das ist
    richtig: die Nachricht ist eine Aussage ueber einen Zeitpunkt, und
    zu dem gehoerte dieser Versatz.
    """
    if v is None:
        return "—"
    off = ((snap or {}).get("space") or {}).get("offset") or 0
    return _fmt(v + off, digits)


class Agent:
    """Haelt Verlauf, Snapshot und Tagesplan je Markt."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"markets": {}, "budget": {"day": None, "used": 0}}
        self._store = Store("agent_state", STATE_PATH)
        self._load()

    # ---------------------------------------------------------------- Zustand
    def _load(self):
        self.state = self._store.load({}) or {}
        self.state.setdefault("markets", {})
        self.state.setdefault("budget", {"day": None, "used": 0})

    def _save(self):
        self._store.save(self.state)

    def _market(self, key):
        m = self.state["markets"].setdefault(key, {})
        m.setdefault("messages", [])
        m.setdefault("cooldowns", {})
        m.setdefault("snapshot", None)
        m.setdefault("plan", None)
        return m

    # ---------------------------------------------------------------- Budget
    def _budget_ok(self, major):
        today = datetime.now().strftime("%Y-%m-%d")
        b = self.state["budget"]
        if b.get("day") != today:
            b["day"], b["used"] = today, 0
        if b["used"] >= DAY_BUDGET:
            return False
        # Ab 80 % kommen nur noch die grossen Auslöser durch.
        if b["used"] >= DAY_BUDGET * 0.8 and not major:
            return False
        return True

    def _spend(self, n=1):
        self.state["budget"]["used"] = self.state["budget"].get("used", 0) + n

    # ---------------------------------------------------------------- Auslöser
    def detect(self, key, snap):
        """Vergleicht den neuen Zustand mit dem letzten und liefert Auslöser."""
        m = self._market(key)
        prev = m.get("snapshot")
        m["snapshot"] = snap
        if not prev:
            return []

        events = []
        spot, prev_spot = snap.get("spot"), prev.get("spot")
        if not spot or not prev_spot:
            return []

        # Regimewechsel relativ zum Zero-Gamma-Level
        if prev.get("regime") and snap.get("regime") != prev["regime"]:
            events.append({
                "type": "regime_flip",
                "why": (f"Regime kippt auf {snap['regime'].upper()}-Gamma. "
                        f"Zero-Gamma {_px(snap.get('flip'), snap)}, Kurs {_px(spot, snap)}."),
            })

        # Zonen: berührt, verlassen, neu, weggefallen
        def inside(z, p):
            return z["bot"] <= p <= z["top"]

        prev_zones = {round(z["mid"]): z for z in prev.get("zones", [])}
        now_zones = {round(z["mid"]): z for z in snap.get("zones", [])}

        for mid, z in now_zones.items():
            was = prev_zones.get(mid)
            if not was:
                events.append({
                    "type": "zone_new",
                    "why": (f"Neue Zone {_px(z['bot'], snap)}–{_px(z['top'], snap)} "
                            f"({', '.join(z['labels'])})."),
                })
                continue
            if inside(z, spot) and not inside(was, prev_spot):
                events.append({
                    "type": "zone_touch",
                    "why": (f"Kurs läuft in Zone #{z.get('rank','?')} "
                            f"{_px(z['bot'], snap)}–{_px(z['top'], snap)} hinein "
                            f"({', '.join(z['labels'])})."),
                })
            elif not inside(z, spot) and inside(was, prev_spot):
                events.append({
                    "type": "zone_break",
                    "why": (f"Kurs verlässt Zone #{z.get('rank','?')} "
                            f"{_px(z['bot'], snap)}–{_px(z['top'], snap)} — jetzt {_px(spot, snap)}."),
                })
        for mid, z in prev_zones.items():
            if mid not in now_zones:
                events.append({
                    "type": "zone_gone",
                    "why": f"Zone {_px(z['bot'], snap)}–{_px(z['top'], snap)} ist weggefallen.",
                })

        # Wandbewegung ueber der Toleranz
        for name, label in (("call_wall", "Call-Wand"), ("put_wall", "Put-Wand"),
                            ("flip", "Zero-Gamma")):
            a, b = prev.get(name), snap.get(name)
            if a and b and abs(b - a) / a > WALL_MOVE_PCT:
                events.append({
                    "type": "wall_move",
                    "why": f"{label} verschoben: {_px(a, snap)} → {_px(b, snap)}.",
                })

        # Kalender: hochgewichteter Termin rueckt heran
        for ev in snap.get("calendar", [])[:3]:
            if 0 <= ev.get("in_minutes", 999) <= 30 and ev.get("impact") == "High":
                events.append({
                    "type": "calendar",
                    "why": (f"{ev['title']} in {ev['in_minutes']} Minuten "
                            f"({ev['country']}, {ev['impact']})."),
                })

        # Neue Schlagzeile
        prev_titles = {h["title"] for h in prev.get("headlines", [])}
        for h in snap.get("headlines", [])[:5]:
            if h["title"] not in prev_titles:
                events.append({"type": "news", "why": f"{h['source']}: {h['title']}"})
                break
        return events

    def _cooldown_ok(self, key, ttype):
        m = self._market(key)
        last = m["cooldowns"].get(ttype, 0)
        cd = TRIGGERS.get(ttype, {}).get("cooldown", 600)
        if time.time() - last < cd:
            return False
        m["cooldowns"][ttype] = time.time()
        return True

    # ---------------------------------------------------------------- Meldung
    def observe(self, key, snap):
        """Ein Beobachtungszyklus. Gibt die erzeugten Meldungen zurueck."""
        with self.lock:
            events = self.detect(key, snap)
            posted = []
            for ev in events:
                cfg = TRIGGERS.get(ev["type"], {})
                if not self._cooldown_ok(key, ev["type"]):
                    continue
                if not self._budget_ok(cfg.get("major", False)):
                    continue
                text = self._compose(snap, ev)
                if GEMINI_KEY:
                    self._spend()
                posted.append(self._post(key, "proactive", text, ev))
            if posted:
                self._save()
            return posted

    def _post(self, key, role, text, ev=None):
        m = self._market(key)
        msg = {
            "role": role, "text": text,
            "ts": datetime.now(timezone.utc).isoformat(),
            "trigger": ev["type"] if ev else None,
            "why": ev["why"] if ev else None,
        }
        m["messages"].append(msg)
        m["messages"] = m["messages"][-120:]
        return msg

    def _compose(self, snap, ev):
        """Text zum Auslöser. Mit Key vom Modell, ohne Key aus Vorlagen."""
        if GEMINI_KEY:
            text = self._ask_model(self._context(snap), ev["why"])
            if text:
                return text
        return self._template(snap, ev)

    def _template(self, snap, ev):
        """Regelbasierte Einordnung - jede Zahl stammt aus der Engine."""
        regime = snap.get("regime")
        parts = [ev["why"]]
        if regime == "short":
            parts.append("Im Short-Gamma verstärken Dealer die Bewegung: "
                         "an den Kanten eher scharfe Ausbrüche als sanftes Abprallen.")
        elif regime == "long":
            parts.append("Im Long-Gamma dämpfen Dealer: Rücksetzer an den Kanten "
                         "werden eher gekauft, Ausbrüche laufen schwerer.")
        flip, spot = snap.get("flip"), snap.get("spot")
        if flip and spot:
            d = spot - flip
            parts.append(f"Zero-Gamma liegt bei {_px(flip, snap)}, der Kurs {_fmt(abs(d))} Punkte "
                         f"{'darüber' if d > 0 else 'darunter'} — "
                         f"{'ein Rutsch darunter kippt das Regime' if d > 0 else 'eine Rückeroberung kippt es zurück'}.")
        cw, pw = snap.get("call_wall"), snap.get("put_wall")
        if cw and pw:
            parts.append(f"Rahmen des Tages: Put-Wand {_px(pw, snap)}, Call-Wand {_px(cw, snap)}.")
        return " ".join(parts)

    def _context(self, snap):
        """Datenblock fuer das Modell - kompakt, aber vollstaendig."""
        lines = [
            f"Markt: {snap.get('market_name')} ({snap.get('market')})",
            f"Kurs: {_px(snap.get('spot'), snap)}",
            f"Gamma-Regime: {snap.get('regime')} (Netto-GEX {_fmt((snap.get('net_gex') or 0)/1e6)} Mio $/Punkt)",
            f"Zero-Gamma: {_px(snap.get('flip'), snap)}",
            f"Call-Wand: {_px(snap.get('call_wall'), snap)}  Put-Wand: {_px(snap.get('put_wall'), snap)}",
            f"Max Pain: {_px(snap.get('max_pain'), snap)}  Gamma-Pin: {_px(snap.get('gamma_pin'), snap)}",
            f"PCR: {snap.get('pcr')}  ATR: {_fmt(snap.get('atr'))}",
        ]
        for z in snap.get("zones", [])[:4]:
            lines.append(f"Zone #{z.get('rank')}: {_px(z['bot'], snap)}–{_px(z['top'], snap)} "
                         f"({', '.join(z['labels'])})")
        for e in snap.get("calendar", [])[:3]:
            lines.append(f"Termin in {e['in_minutes']} Min: {e['title']} ({e['impact']})")
        for h in snap.get("headlines", [])[:4]:
            lines.append(f"News: {h['title']}")
        return "\n".join(lines)

    def _ask_model(self, context, reason, max_sentences=4):
        prompt = (
            "Du bist der eingebaute Analyst eines Chart-Terminals. Ordne den "
            "Auslöser in die Marktlage ein. Verwende ausschließlich Zahlen aus "
            "dem Datenblock, erfinde keine. Kein Markdown, keine Aufzählung, "
            f"höchstens {max_sentences} Sätze, Deutsch.\n\n"
            f"DATENBLOCK\n{context}\n\nAUSLÖSER\n{reason}"
        )
        for model in (GEMINI_MODEL, GEMINI_FALLBACK):
            try:
                r = requests.post(
                    GEMINI_URL.format(m=model),
                    params={"key": GEMINI_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=30,
                )
                if r.status_code == 429:
                    continue   # Kontingent erschöpft, naechstes Modell
                if not r.ok:
                    continue
                cand = r.json().get("candidates") or []
                if cand:
                    txt = cand[0]["content"]["parts"][0]["text"].strip()
                    return txt.replace("**", "").replace("*", "")
            except Exception:
                continue
        return None

    # ---------------------------------------------------------------- Chat
    def ask(self, key, question, snap):
        """Direkte Frage. Vom Budget-Schutz ausgenommen."""
        with self.lock:
            self._post(key, "user", question)
            if GEMINI_KEY:
                answer = self._ask_model(self._context(snap),
                                         f"Frage des Traders: {question}", 6)
                if not answer:
                    answer = ("Das Modell antwortet gerade nicht — Kontingent erschöpft "
                              "oder Dienst nicht erreichbar. Die Zahlen im Terminal "
                              "bleiben davon unberührt.")
            else:
                answer = self._answer_offline(question, snap)
            msg = self._post(key, "agent", answer)
            self._save()
            return msg

    def _answer_offline(self, question, snap):
        """Antwort ohne Modell: liest die Lage aus der Engine vor."""
        q = (question or "").lower()
        if any(w in q for w in ("wand", "wall", "widerstand", "support", "boden")):
            return (f"Put-Wand {_px(snap.get('put_wall'), snap)}, Call-Wand {_px(snap.get('call_wall'), snap)}, "
                    f"Zero-Gamma {_px(snap.get('flip'), snap)}. Max Pain liegt bei "
                    f"{_px(snap.get('max_pain'), snap)}, der Gamma-Pin bei {_px(snap.get('gamma_pin'), snap)}.")
        if any(w in q for w in ("zone", "level", "marke")):
            zs = snap.get("zones", [])[:3]
            if not zs:
                return "Aktuell trägt keine Zone genug Konfluenz, um ausgewiesen zu werden."
            return " ".join(f"Zone #{z.get('rank')}: {_px(z['bot'], snap)}–{_px(z['top'], snap)} "
                            f"({', '.join(z['labels'])})." for z in zs)
        if any(w in q for w in ("regime", "gamma", "bias", "richtung")):
            return self._template(snap, {"why": "Lage auf Nachfrage:"})
        return (f"Ohne Modell-Key antworte ich aus der Engine: Kurs {_px(snap.get('spot'), snap)}, "
                f"{snap.get('regime','?').upper()}-Gamma, Zero-Gamma {_px(snap.get('flip'), snap)}, "
                f"Rahmen {_px(snap.get('put_wall'), snap)} bis {_px(snap.get('call_wall'), snap)}. "
                f"Frag nach Zonen, Wänden oder Regime für mehr Detail.")

    # ---------------------------------------------------------------- Tagesplan
    def plan(self, key, snap, force=False):
        """Tagesplan: einmal morgens erstellt, danach nur fortgeschrieben."""
        with self.lock:
            m = self._market(key)
            today = datetime.now().strftime("%Y-%m-%d")
            cur = m.get("plan")
            if cur and cur.get("day") == today and not force:
                return cur
            if not force and datetime.now().hour < PLAN_HOUR and cur:
                return cur

            body = self._plan_text(snap)
            plan = {"day": today, "created": datetime.now(timezone.utc).isoformat(),
                    "market": key, "body": body, "updates": [],
                    "anchor": {k: snap.get(k) for k in
                               ("spot", "flip", "call_wall", "put_wall", "regime")}}
            m["plan"] = plan
            self._save()
            return plan

    def _plan_text(self, snap):
        """Struktur des Morgenplans - dieselben fuenf Abschnitte wie im Original."""
        spot = snap.get("spot")
        flip = snap.get("flip")
        regime = snap.get("regime")
        atr_v = snap.get("atr")
        zs = snap.get("zones", [])

        struktur = []
        struktur.append(
            f"Gamma-Regime: {(regime or '?').upper()}-GAMMA "
            f"(Netto-GEX {_fmt((snap.get('net_gex') or 0)/1e6)} Mio $ je Punkt, "
            f"PCR {snap.get('pcr') and round(snap['pcr'],2)}). "
            + ("Dealer verstärken Bewegungen — Reaktionen kommen scharf an den Kanten."
               if regime == "short" else
               "Dealer dämpfen Bewegungen — Rücksetzer laufen eher aus als durch."))
        if flip:
            struktur.append(f"Pivot / Zero-Gamma: {_px(flip, snap)}. "
                            f"Kurs steht {_fmt(abs((spot or 0) - flip))} Punkte "
                            f"{'darüber' if (spot or 0) > flip else 'darunter'}.")
        if snap.get("call_wall"):
            struktur.append(f"Haupt-Widerstand: Call-Wand {_px(snap['call_wall'], snap)}.")
        if snap.get("put_wall"):
            struktur.append(f"Haupt-Unterstützung: Put-Wand {_px(snap['put_wall'], snap)}.")
        for z in zs[:3]:
            struktur.append(f"Zone #{z.get('rank')}: {_px(z['bot'], snap)}–{_px(z['top'], snap)} "
                            f"({', '.join(z['labels'])}).")

        if regime == "short":
            bias = ("Erhöhte Bewegungsbereitschaft. Im Short-Gamma trägt Trendfortsetzung "
                    "eher als Mean Reversion.")
        else:
            bias = ("Gedämpfte Bewegung. Im Long-Gamma trägt Mean Reversion zwischen den "
                    "Wänden eher als der Ausbruch.")
        if flip and spot:
            bias += (f" Kipp-Punkt: ein nachhaltiges Etablieren "
                     f"{'unter' if spot > flip else 'über'} {_px(flip, snap)} dreht das Bild.")

        watch = []
        for z in zs[:3]:
            watch.append(f"Reaktion an Zone #{z.get('rank')} {_px(z['bot'], snap)}–{_px(z['top'], snap)}: "
                         f"hält die Kante oder bricht sie?")
        if snap.get("max_pain") and spot:
            d = snap["max_pain"] - spot
            watch.append(f"Max Pain {_px(snap['max_pain'], snap)} liegt {_fmt(abs(d))} Punkte "
                         f"{'über' if d > 0 else 'unter'} dem Kurs — Sog in den Verfall.")
        for e in snap.get("calendar", [])[:2]:
            watch.append(f"{e['title']} in {e['in_minutes']} Minuten ({e['impact']}).")

        nachrichten = [h["title"] for h in snap.get("headlines", [])[:4]] or \
                      ["Keine index-relevante Schlagzeile im Filter."]

        return {
            "rueckblick": (
                f"ATR {_fmt(atr_v)} Punkte. Spanne der Sitzung "
                f"{snap.get('on_day') or ''}: {_px(snap.get('on_low'), snap)}–{_px(snap.get('on_high'), snap)}"
                + (f" ({(snap['on_high'] - snap['on_low']) / atr_v:.2f} ATR)."
                   if atr_v and snap.get('on_high') and snap.get('on_low') else ".")),
            "nachrichten": nachrichten,
            "struktur": struktur,
            "bias": bias,
            "watch": watch,
        }

    def maybe_update_plan(self, key, snap):
        """Fortschreibung nur bei struktureller Änderung."""
        with self.lock:
            m = self._market(key)
            plan = m.get("plan")
            if not plan:
                return None
            a = plan.get("anchor") or {}
            reasons = []
            if a.get("regime") and snap.get("regime") != a["regime"]:
                reasons.append(f"Regime gekippt auf {snap.get('regime','?').upper()}-Gamma")
            atr_v = snap.get("atr") or 0
            for name, label in (("flip", "Zero-Gamma"), ("call_wall", "Call-Wand"),
                                ("put_wall", "Put-Wand")):
                old, new = a.get(name), snap.get(name)
                if old and new and abs(new - old) > max(0.15 * atr_v, old * WALL_MOVE_PCT):
                    reasons.append(f"{label} {_px(old, snap)} → {_px(new, snap)}")
            if not reasons:
                return None
            note = {"ts": datetime.now(timezone.utc).isoformat(),
                    "why": "; ".join(reasons),
                    "text": self._template(snap, {"why": "; ".join(reasons) + "."})}
            plan["updates"].append(note)
            plan["anchor"] = {k: snap.get(k) for k in
                              ("spot", "flip", "call_wall", "put_wall", "regime")}
            self._save()
            return note

    # ---------------------------------------------------------------- Lesen
    def messages(self, key, limit=60):
        with self.lock:
            return list(self._market(key)["messages"][-limit:])

    def get_plan(self, key):
        with self.lock:
            return self._market(key).get("plan")

    def budget(self):
        with self.lock:
            b = dict(self.state["budget"])
            b["limit"] = DAY_BUDGET
            b["model"] = GEMINI_MODEL if GEMINI_KEY else None
            return b


AGENT = Agent()
