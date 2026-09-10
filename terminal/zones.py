"""Konfluenz-Zonen: wo mehrere unabhaengige Quellen dasselbe Band meinen.

Die Zonen sind bewusst der breite Kontext, nicht das Einstiegsband. Im
Gameplan-Dokument ist genau das nachgemessen: eine Zone von 137 Punkten
gegen ein Zielband von 32 Punkten. Ein schmales Band darin waere die
Aufgabe einer Gameplan-Engine - die hier noch nicht gebaut ist.
"""

MERGE_ATR = 0.28   # dieselbe Konstante wie im lokalen Terminal

# Gewicht je Quellenart. Eine Hauptwand wiegt mehr als eine runde Zahl -
# aber keine Quelle allein macht eine Zone, dafuer braucht es mindestens
# zwei verschiedene Arten.
WEIGHTS = {
    "call_wall": 3.0, "put_wall": 3.0, "flip": 3.0,
    "max_pain": 2.0, "gamma_pin": 2.0,
    "poc": 2.0, "vah": 1.5, "val": 1.5,
    "pdh": 1.5, "pdl": 1.5, "pwh": 1.0, "pwl": 1.0,
    "on_high": 1.0, "on_low": 1.0,
    # Vortagsprofil: derselbe Rang wie das heutige. Der POC von gestern
    # ist der Preis, an dem der Markt zuletzt am laengsten stand - dass
    # er einen Tag alt ist, macht ihn nicht schwaecher.
    "pdpoc": 2.0, "pdvah": 1.5, "pdval": 1.5,
}

LABELS = {
    "call_wall": "Call-Wand", "put_wall": "Put-Wand", "flip": "Zero-Gamma",
    "max_pain": "Max Pain", "gamma_pin": "Gamma-Pin",
    "poc": "POC", "vah": "VAH", "val": "VAL",
    "pdh": "Vortageshoch", "pdl": "Vortagestief",
    "pdpoc": "POC Vortag", "pdvah": "VAH Vortag", "pdval": "VAL Vortag",
    "pwh": "Vorwochenhoch", "pwl": "Vorwochentief",
    "on_high": "Tageshoch", "on_low": "Tagestief",
}


def collect(gexp, vp, session):
    """Sammelt alle Kandidaten mit Quellenart und Beschriftung."""
    out = []

    def add(kind, price, extra=""):
        if price and price > 0:
            out.append({"kind": kind, "price": float(price),
                        "label": (LABELS.get(kind, kind) + (" " + extra if extra else "")).strip()})

    if gexp.get("ok"):
        for i, w in enumerate(gexp.get("call_walls") or []):
            add("call_wall", w["price"], "" if i == 0 else f"C{i+1}")
        for i, w in enumerate(gexp.get("put_walls") or []):
            add("put_wall", w["price"], "" if i == 0 else f"P{i+1}")
        add("flip", gexp.get("flip"))
        add("max_pain", gexp.get("max_pain"))
        add("gamma_pin", gexp.get("gamma_pin"))

    for k in ("poc", "vah", "val"):
        add(k, (vp or {}).get(k))
    for k in ("pdh", "pdl", "pwh", "pwl", "on_high", "on_low",
              "pdpoc", "pdvah", "pdval"):
        add(k, (session or {}).get(k))
    return out


def build(gexp, vp, session, atr_value, spot, limit=5):
    """Baut die Zonen und ordnet sie nach Staerke."""
    levels = collect(gexp, vp, session)
    if not levels or not atr_value:
        return [], levels

    tol = MERGE_ATR * atr_value
    items = sorted(levels, key=lambda x: x["price"])
    groups, cur = [], [items[0]]
    for it in items[1:]:
        if it["price"] - cur[-1]["price"] <= tol:
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)

    zones = []
    for g in groups:
        kinds = {x["kind"] for x in g}
        if len(kinds) < 2:
            continue  # eine einzelne Quelle ist ein Level, keine Zone
        prices = [x["price"] for x in g]
        score = sum(WEIGHTS.get(x["kind"], 1.0) for x in g)
        top, bot = max(prices), min(prices)
        mid = (top + bot) / 2
        zones.append({
            "top": top, "bot": bot, "mid": mid,
            "width": top - bot,
            "width_atr": (top - bot) / atr_value if atr_value else 0,
            "score": score,
            "sources": sorted(kinds),
            "labels": [x["label"] for x in g],
            "role": _role(mid, spot),
            "dist": mid - spot,
        })
    zones.sort(key=lambda z: z["score"], reverse=True)
    for i, z in enumerate(zones[:limit], 1):
        z["rank"] = i
    return zones[:limit], levels


def _role(price, spot):
    """Rolle relativ zum aktuellen Kurs - die Farbe im Chart folgt daraus."""
    if not spot:
        return "pivot"
    d = (price - spot) / spot
    if d > 0.0015:
        return "resist"
    if d < -0.0015:
        return "support"
    return "pivot"
