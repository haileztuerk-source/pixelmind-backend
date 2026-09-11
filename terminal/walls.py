"""Leading Walls ueber mehrere Optionsketten.

Ein Index hat zwei Ketten, die dieselbe Sache meinen: die Index-Kette
selbst (NDX, SPX) und die des ETF darauf (QQQ, SPY). Beide tragen echte
Bestaende, aber auf verschiedenen Rastern - NDX in 25er-Schritten, QQQ in
Dollar. Der ETF-Strike wird deshalb ueber das Verhaeltnis der beiden
Spotkurse in den Index-Preisraum uebersetzt.

Wichtig, und aus dem Strike-Raum-Dokument uebernommen: **die Herkunft
wird bis ins Level-Dict mitgefuehrt.** Eine QQQ-Sprosse und eine
NDX-Sprosse koennen denselben Preis meinen und trotzdem verschieden viel
wert sein - der QQQ-Strike-Schritt entspricht rund 41 NDX-Punkten und ist
damit der Genauigkeitsboden jeder daraus abgeleiteten Marke.

Rangfolge nach **Open Interest**, nicht nach Gamma. Gamma ist per
Konstruktion am Geld maximal; danach zu ranken erzeugt genau die
ATM-Artefakte, an denen die Zonen-Engine des Originals gelitten hat.
"""

from . import cboe, gex

# Je Markt: die Index-Kette zuerst (definiert), dann die ETF-Kette
# (bestaetigt). Reihenfolge ist bedeutungstragend.
CHAINS = {
    "NQ":  [("_NDX", "Index"), ("QQQ", "ETF")],
    "ES":  [("_SPX", "Index"), ("SPY", "ETF")],
    "YM":  [("_DJX", "Index"), ("DIA", "ETF")],
    "RTY": [("_RUT", "Index"), ("IWM", "ETF")],
    "GC":  [("GLD", "ETF")],
}

WINDOW = 0.06        # nur Strikes in diesem Band um den Spot
PER_SIDE = 4         # so viele Waende je Seite und Kette


def _side_walls(rows, spot, side, count, window):
    """Staerkste OI-Strikes einer Seite innerhalb des Fensters."""
    lo, hi = spot * (1 - window), spot * (1 + window)
    key = "call_oi" if side == "call" else "put_oi"
    if side == "call":
        cand = [r for r in rows if spot <= r["strike"] <= hi and r[key] > 0]
    else:
        cand = [r for r in rows if lo <= r["strike"] <= spot and r[key] > 0]
    cand.sort(key=lambda r: r[key], reverse=True)
    return cand[:count]


def leading(market_key, per_side=PER_SIDE, window=WINDOW):
    """Alle Leading Walls eines Marktes, im Preisraum des Index.

    Gibt zusaetzlich das verwendete Uebersetzungsverhaeltnis zurueck,
    damit im Frontend nachvollziehbar bleibt, wie eine ETF-Marke auf
    ihren Indexpreis gekommen ist.
    """
    chains = CHAINS.get(market_key) or CHAINS["NQ"]
    base = cboe.chain(chains[0][0], ttl=120)
    base_spot = base.get("spot")
    if not base_spot:
        return {"walls": [], "ratios": {}, "spot": None}

    out, ratios = [], {}

    for sym, kind in chains:
        data = base if sym == chains[0][0] else cboe.chain(sym, ttl=120)
        spot = data.get("spot")
        if not spot or not data.get("contracts"):
            continue

        # Verhaeltnis aus zwei zeitgleich geholten Spotkursen derselben
        # Quelle - keine Futures-Basis, keine Put-Call-Paritaet noetig.
        ratio = base_spot / spot if sym != chains[0][0] else 1.0
        ratios[sym] = ratio

        rows = gex.by_strike(data["contracts"])
        for side in ("call", "put"):
            for rank, r in enumerate(_side_walls(rows, spot, side, per_side, window), 1):
                oi = r["call_oi"] if side == "call" else r["put_oi"]
                gam = r["call_gamma"] if side == "call" else r["put_gamma"]
                out.append({
                    "price": r["strike"] * ratio,
                    "strike": r["strike"],
                    "chain": sym,
                    "chain_kind": kind,
                    "side": side,
                    "rank": rank,
                    "oi": oi,
                    "vol": r["call_vol"] if side == "call" else r["put_vol"],
                    "voi": r["voi"],
                    # Dollar-Gamma je Indexpunkt an diesem Strike
                    "gex": abs(gam) * 100.0 * spot * ratio,
                    # Aufloesungsgrenze dieser Kette in Indexpunkten
                    "grid": _grid(rows) * ratio,
                })

    out.sort(key=lambda w: w["price"], reverse=True)
    return {"walls": out, "ratios": ratios, "spot": base_spot}


def _grid(rows):
    """Medianer Strike-Abstand einer Kette - ihr Genauigkeitsboden."""
    ks = sorted({r["strike"] for r in rows})
    gaps = [b - a for a, b in zip(ks, ks[1:]) if b - a > 0]
    if not gaps:
        return 0.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def confluence(walls, tolerance):
    """Waende verschiedener Ketten, die dasselbe Band meinen.

    Liegt eine NDX-Wand auf einer QQQ-Wand, ist das die staerkste
    Aussage, die diese Daten hergeben: zwei unabhaengige Bestaende auf
    demselben Fleck. Genau das meint "fortress" in der Namensgrammatik.
    """
    if not walls:
        return []
    items = sorted(walls, key=lambda w: w["price"])
    groups, cur = [], [items[0]]
    for w in items[1:]:
        if w["price"] - cur[-1]["price"] <= tolerance:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    groups.append(cur)

    out = []
    for g in groups:
        if len({w["chain"] for w in g}) < 2:
            continue      # eine Kette allein ist Bestaetigung von nichts
        prices = [w["price"] for w in g]
        out.append({
            "top": max(prices), "bot": min(prices),
            "mid": sum(prices) / len(prices),
            "chains": sorted({w["chain"] for w in g}),
            "sides": sorted({w["side"] for w in g}),
            "oi": sum(w["oi"] for w in g),
            "members": len(g),
        })
    out.sort(key=lambda z: z["oi"], reverse=True)
    return out
