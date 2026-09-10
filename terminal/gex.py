"""Gamma-Engine: aus einer Optionskette werden handelbare Level.

Alle Groessen rechnen **je Indexpunkt**, nicht je Prozent. Das ist die
Zahl, die man mit einer Kursbewegung im Kopf verrechnen kann - im
lokalen Terminal war die uneinheitliche Konvention (gex_dollar je 1 %
gegen "je Punkt" der Referenz) eine dokumentierte Fehlerquelle.

Vorzeichen: Calls positiv, Puts negativ. Das ist die uebliche Annahme
"Dealer sind long Calls, short Puts". Sie ist geraten, nicht gemessen -
und der Grund, warum das Dealer-Delta-Notional F(S) hier bewusst NICHT
als Level ausgewiesen wird: mit geratenen Vorzeichen ist F monoton und
hat kein einziges Extremum. Dafuer braeuchte es die Fluss-Klassifikation
aus zwei aufeinanderfolgenden Snapshots.
"""

import math
from collections import defaultdict

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _npdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _bs_gamma(S, K, T, sigma):
    """Black-Scholes-Gamma mit r = q = 0.

    Fuer kurzlaufende Indexoptionen ist der Zinsterm gegenueber der
    Vola vernachlaessigbar; die Vereinfachung haelt die Rechnung
    nachvollziehbar statt scheingenau.
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    return _npdf(d1) / (S * v)


def _bs_vanna_charm(S, K, T, sigma):
    """Vanna (dDelta/dVola) und Charm (dDelta/Tag), r = q = 0."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0, 0.0
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    d2 = d1 - v
    n = _npdf(d1)
    vanna = -n * d2 / sigma
    charm = -n * d2 / (2.0 * T)
    return vanna, charm


def by_strike(contracts):
    """Verdichtet die Kette auf eine Zeile je Strike."""
    rows = defaultdict(lambda: {
        "strike": 0.0, "call_oi": 0.0, "put_oi": 0.0,
        "call_vol": 0.0, "put_vol": 0.0,
        "call_gamma": 0.0, "put_gamma": 0.0, "iv": [],
    })
    for c in contracts:
        r = rows[c["strike"]]
        r["strike"] = c["strike"]
        if c["is_call"]:
            r["call_oi"] += c["oi"]
            r["call_vol"] += c["volume"]
            r["call_gamma"] += c["gamma"] * c["oi"]
        else:
            r["put_oi"] += c["oi"]
            r["put_vol"] += c["volume"]
            r["put_gamma"] += c["gamma"] * c["oi"]
        if c["iv"] > 0:
            r["iv"].append(c["iv"])
    out = []
    for r in rows.values():
        oi = r["call_oi"] + r["put_oi"]
        vol = r["call_vol"] + r["put_vol"]
        out.append({
            "strike": r["strike"],
            "call_oi": r["call_oi"], "put_oi": r["put_oi"], "oi": oi,
            "call_vol": r["call_vol"], "put_vol": r["put_vol"], "vol": vol,
            # v/OI: wo das heutige Volumen das stehende OI vielfach
            # uebersteigt, wird gerade frisch positioniert - die Waende
            # von morgen, die ein reines OI-Bild nicht zeigt.
            "voi": (vol / oi) if oi > 0 else 0.0,
            "iv": (sum(r["iv"]) / len(r["iv"])) if r["iv"] else 0.0,
            "call_gamma": r["call_gamma"], "put_gamma": r["put_gamma"],
        })
    out.sort(key=lambda r: r["strike"])
    return out


def net_gex_at(contracts, S):
    """Netto-Gamma-Exposure in $ je Indexpunkt, neu gerechnet fuer Kurs S."""
    total = 0.0
    for c in contracts:
        if c["oi"] <= 0 or c["iv"] <= 0:
            continue
        T = max(c["dte"], 0.5) / 365.0
        g = _bs_gamma(S, c["strike"], T, c["iv"])
        if g <= 0:
            continue
        e = g * c["oi"] * 100.0 * S
        total += e if c["is_call"] else -e
    return total


def zero_gamma(contracts, spot, span=0.06, steps=41):
    """Kurs, an dem das Netto-Gamma das Vorzeichen wechselt.

    Nicht der Strike mit dem groessten Gamma - der klebt per Konstruktion
    am Geld und war im Original die Ursache fuer acht Regime-Kippen pro
    Tag. Stattdessen wird die Kurve ueber ein Preisgitter neu gerechnet
    und die echte Nullstelle interpoliert.
    """
    if not spot:
        return None, []
    lo, hi = spot * (1 - span), spot * (1 + span)
    step = (hi - lo) / (steps - 1)
    curve = []
    for i in range(steps):
        S = lo + i * step
        curve.append({"s": S, "gex": net_gex_at(contracts, S)})

    flip = None
    for a, b in zip(curve, curve[1:]):
        if a["gex"] == 0:
            flip = a["s"]
            break
        if (a["gex"] < 0) != (b["gex"] < 0):
            # Lineare Interpolation zwischen den beiden Stuetzstellen
            frac = abs(a["gex"]) / (abs(a["gex"]) + abs(b["gex"]))
            flip = a["s"] + frac * (b["s"] - a["s"])
            break
    return flip, curve


def level_gamma(contracts, levels, spot, flip=None, atr=None):
    """Wieviel Dealer-Gamma haengt an jedem dieser Preise - und in welche
    Richtung wirkt es dort?

    Das ist die Frage, die ein Level erst zu einer Aussage macht. Ein
    Vortageshoch ist eine Zahl aus der Vergangenheit; ein Vortageshoch,
    an dem 2 Mio Dollar Gamma je Punkt POSITIV haengen, ist eine Kante,
    an der Dealer gegen die Bewegung handeln muessen. Dasselbe Hoch mit
    negativem Gamma ist eine Kante, an der sie mitziehen - und dann
    traegt der Ausbruch statt der Ablehnung.

    Gerechnet wird nicht "welche Strikes liegen in der Naehe", sondern
    das Netto-Gamma der GANZEN Kette, ausgewertet an diesem Preis. Der
    Unterschied ist wesentlich: ein Strike weit weg mit riesigem Bestand
    wirkt auch hier noch, ein naher mit leerem Bestand gar nicht.

    Dazu die Steigung. Sie sagt, was beim DURCHLAUFEN passiert - ob das
    Gamma jenseits der Kante zunimmt (die Bewegung laeuft sich fest) oder
    abnimmt (sie beschleunigt). Ein Level, an dem das Vorzeichen kippt,
    ist etwas anderes als eines mitten im Long-Gamma-Bereich.
    """
    out = []
    if not contracts or not levels:
        return out
    # Fensterbreite fuer die Steigung: eng genug, dass sie oertlich ist.
    # Ein erster Versuch nahm 0,4 Prozent des Kurses - bei NDX 116 Punkte.
    # Damit lag die Nullstelle innerhalb des Fensters von fuenf
    # verschiedenen Marken, und alle fuenf meldeten "das Vorzeichen kippt
    # genau hier". Es kippt aber nur an einer Stelle.
    tol = max(0.25 * (atr or 0.0), (spot or 1.0) * 0.0008)
    for lv in levels:
        p = lv.get("price")
        if not p or p <= 0:
            continue
        d = max(0.1 * (atr or 0.0), p * 0.0005)
        g = net_gex_at(contracts, p)
        g_lo = net_gex_at(contracts, p - d)
        g_hi = net_gex_at(contracts, p + d)
        out.append({
            **lv,
            "gex": g,
            # Aenderung je Indexpunkt beim Durchlaufen der Kante
            "slope": (g_hi - g_lo) / (2.0 * d),
            # Pivot heisst: die Marke IST die Nullstelle, nicht bloss in
            # ihrer Naehe. Gemessen gegen den bereits gerechneten Flip -
            # ihn aus dem Vorzeichenwechsel eines Fensters neu abzuleiten
            # war die Quelle des Fehlers.
            "is_pivot": (flip is not None
                         and abs(p - flip) <= tol),
            "side": "above" if (spot and p > spot) else "below",
        })
    # Bezug ist das Gamma AM KURS, nicht das groesste unter den Marken.
    #
    # Gegen das Maximum gemessen sah es falsch aus: in einer
    # Long-Gamma-Struktur waechst das Gamma mit dem Abstand, also gewann
    # immer die entfernteste Wand. Gemessen bekam eine Call-Wand 396
    # Punkte weiter 100 Prozent, waehrend POC und Value Area direkt am
    # Kurs mit 6 und 7 Prozent als "kaum Gamma" durchgingen - obwohl
    # dort gerade gehandelt wird.
    #
    # Am Kurs gemessen heisst die Zahl etwas: "an dieser Marke haengt
    # doppelt so viel Gamma wie jetzt gerade" ist eine Aussage ueber die
    # Kraft, gegen die der Kurs dort laufen wuerde.
    ref = abs(net_gex_at(contracts, spot)) if spot else 0.0
    ref = max(ref, 1.0)
    for x in out:
        x["gex_rel"] = abs(x["gex"]) / ref
    return out


def level_verdict(lv, spot, regime=None):
    """Aus Lage und Gamma eine Erwartung - in einem Satz.

    Bewusst zwei Groessen und nicht eine: WO das Level liegt entscheidet,
    ob eine Reaktion Widerstand oder Unterstuetzung heisst; WIEVIEL Gamma
    dort haengt entscheidet, ob ueberhaupt eine Reaktion zu erwarten ist.
    Ein Level ohne Gamma ist eine Linie im Chart und sonst nichts.
    """
    g, rel = lv.get("gex") or 0.0, lv.get("gex_rel") or 0.0
    oben = lv.get("side") == "above"
    kante = "Widerstand" if oben else "Unterstützung"

    # Der Vorzeichenwechsel steht VOR der Groesse. An einer Nullstelle
    # ist das Gamma per Definition null - die Pruefung auf "wenig Gamma"
    # zuerst liess ausgerechnet das Zero-Gamma als unbedeutend
    # durchgehen, obwohl es der Punkt ist, an dem das Verhalten des
    # Marktes kippt.
    if lv.get("is_pivot"):
        return ("hier liegt die Nullstelle des Gammas — jenseits dieser "
                "Marke verhaelt sich der Markt anders herum")
    if rel < 0.4:
        return ("wenig Gamma gegen den Kurs — die Marke traegt hier "
                "kaum, der Kurs kann durchlaufen")
    wucht = ("das %.1f-fache des Gammas am Kurs" % rel) if rel >= 1.2 else "vergleichbares Gamma"
    if g > 0:
        return (f"{kante} mit stuetzendem Gamma ({wucht}): Dealer handeln dort "
                f"gegen die Bewegung, Ablehnung ist wahrscheinlicher als Durchbruch")
    return (f"{kante} mit beschleunigendem Gamma ({wucht}): Dealer ziehen dort "
            f"mit, ein Durchbruch traegt eher als eine Ablehnung")


def walls(rows, spot, count=3, window=0.06):
    """Staerkste OI-Strikes je Seite: Hauptwand plus Leiter dahinter.

    Nur Strikes innerhalb von `window` um den Spot. Ohne diese Grenze
    gewinnen immer die tiefen Crash-Absicherungen - bei NDX stehen die
    groessten Put-Bestaende auf 20000 und 22000, rund ein Drittel unter
    dem Markt. Strukturell echt, als Tagesboden aber unbrauchbar.
    """
    lo, hi = spot * (1 - window), spot * (1 + window)
    calls = sorted([r for r in rows if spot <= r["strike"] <= hi],
                   key=lambda r: r["call_oi"], reverse=True)
    puts = sorted([r for r in rows if lo <= r["strike"] <= spot],
                  key=lambda r: r["put_oi"], reverse=True)
    return (
        [{"price": r["strike"], "oi": r["call_oi"]} for r in calls[:count] if r["call_oi"] > 0],
        [{"price": r["strike"], "oi": r["put_oi"]} for r in puts[:count] if r["put_oi"] > 0],
    )


def max_pain(rows):
    """Strike mit der geringsten Gesamtauszahlung an Optionskaeufer."""
    if not rows:
        return None
    best, best_pain = None, None
    for cand in rows:
        K = cand["strike"]
        pain = 0.0
        for r in rows:
            if r["strike"] < K:
                pain += r["call_oi"] * (K - r["strike"])
            elif r["strike"] > K:
                pain += r["put_oi"] * (r["strike"] - K)
        if best_pain is None or pain < best_pain:
            best, best_pain = K, pain
    return best


def gamma_pin(rows):
    """Strike mit dem hoechsten absoluten Gamma - der staerkste Magnet."""
    if not rows:
        return None
    r = max(rows, key=lambda r: abs(r["call_gamma"]) + abs(r["put_gamma"]))
    return r["strike"] if (abs(r["call_gamma"]) + abs(r["put_gamma"])) > 0 else None


def greeks_profile(contracts, spot):
    """Netto-Vanna und Netto-Charm ueber die ganze Kette."""
    v_tot = c_tot = 0.0
    for c in contracts:
        if c["oi"] <= 0 or c["iv"] <= 0:
            continue
        T = max(c["dte"], 0.5) / 365.0
        vanna, charm = _bs_vanna_charm(spot, c["strike"], T, c["iv"])
        sign = 1.0 if c["is_call"] else -1.0
        v_tot += sign * vanna * c["oi"] * 100.0 * spot / 100.0   # je Vola-Punkt
        c_tot += sign * charm * c["oi"] * 100.0 * spot / 365.0   # je Tag
    return {"vanna": v_tot, "charm": c_tot}


def expiry_ladder(contracts, spot, limit=8):
    """Netto-Gamma je Verfall - die Kacheln der Verfallsleiter.

    Kachel 1 zeigt 0DTE allein, ab Kachel 2 wird kumuliert (ohne 0DTE) -
    dieselbe Semantik, die im Strike-Raum-Dokument entschluesselt wurde.
    """
    per = defaultdict(float)
    for c in contracts:
        if c["oi"] <= 0 or c["iv"] <= 0:
            continue
        T = max(c["dte"], 0.5) / 365.0
        g = _bs_gamma(spot, c["strike"], T, c["iv"])
        e = g * c["oi"] * 100.0 * spot
        per[c["expiry"]] += e if c["is_call"] else -e

    days = sorted(per.keys())[:limit]
    out, cum = [], 0.0
    for i, d in enumerate(days):
        single = per[d]
        if i > 0:
            cum += single
        out.append({"expiry": d, "single": single,
                    "cumulative": single if i == 0 else cum})
    return out


def cluster_zones(levels, tolerance):
    """Fasst dicht liegende Level zu Baendern zusammen.

    Ein Gameplan braucht beides: die breite Zone als Kontext und ein
    schmales Band darin als Marke. Hier entsteht die Zone; die Kanten
    kommen aus den beitragenden Leveln selbst, nicht aus einem festen
    ATR-Vielfachen.
    """
    if not levels:
        return []
    items = sorted(levels, key=lambda x: x["price"])
    groups, cur = [], [items[0]]
    for it in items[1:]:
        if it["price"] - cur[-1]["price"] <= tolerance:
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)

    zones = []
    for g in groups:
        if len(g) < 2:
            continue
        prices = [x["price"] for x in g]
        zones.append({
            "top": max(prices), "bot": min(prices),
            "mid": sum(prices) / len(prices),
            "count": len(g),
            "sources": sorted({x["kind"] for x in g}),
            "labels": [x["label"] for x in g],
        })
    zones.sort(key=lambda z: z["count"], reverse=True)
    return zones


def profile(chain_data, spot=None):
    """Vollstaendiges Gamma-Bild aus einer Kette."""
    contracts = chain_data.get("contracts") or []
    S = spot or chain_data.get("spot")
    if not contracts or not S:
        return {"ok": False}

    rows = by_strike(contracts)
    zdte = [c for c in contracts if c["dte"] < 1.0]
    call_w, put_w = walls(rows, S)
    flip, curve = zero_gamma(contracts, S)
    net = net_gex_at(contracts, S)
    net_zdte = net_gex_at(zdte, S) if zdte else 0.0

    call_oi = sum(r["call_oi"] for r in rows)
    put_oi = sum(r["put_oi"] for r in rows)

    # Fresh-Flow: nur Strikes im relevanten Umfeld, sonst dominieren
    # weit entfernte Zeilen mit einer Handvoll Kontrakten.
    near = [r for r in rows if abs(r["strike"] - S) / S < 0.05 and r["oi"] > 50]
    fresh = sorted(near, key=lambda r: r["voi"], reverse=True)[:6]

    return {
        "ok": True,
        "spot": S,
        "net_gex": net,
        "net_gex_zdte": net_zdte,
        "regime": "short" if net < 0 else "long",
        "flip": flip,
        "curve": curve,
        "call_walls": call_w,
        "put_walls": put_w,
        "max_pain": max_pain(rows),
        "gamma_pin": gamma_pin(rows),
        "pcr": (put_oi / call_oi) if call_oi > 0 else None,
        "call_oi": call_oi, "put_oi": put_oi,
        "greeks": greeks_profile(contracts, S),
        "expiries": expiry_ladder(contracts, S),
        "fresh_flow": [{"strike": r["strike"], "voi": r["voi"], "vol": r["vol"]}
                       for r in fresh],
        "rows": [r for r in rows if abs(r["strike"] - S) / S < 0.08],
        "stale": chain_data.get("stale", False),
        "ts": chain_data.get("ts"),
    }
