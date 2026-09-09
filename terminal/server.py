"""ChartTerminal Cloud - Flask-Server.

Liefert das Frontend und alle Datenendpunkte. Ein Hintergrund-Thread
haelt die Snapshots frisch und laesst den Agenten beobachten; er arbeitet
nur fuer Maerkte, die zuletzt wirklich abgerufen wurden, damit eine
schlafende Instanz keine Daten zieht.
"""

import os
import time
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from . import market, cboe, gex, zones, news
from .agent import AGENT
from .daybook import BOOK
from . import keepalive

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "20"))
ACTIVE_TTL = 600          # so lange gilt ein Markt nach dem letzten Abruf als aktiv

app = Flask(__name__, static_folder=None)
CORS(app)

_snapshots = {}
_active = {}
_lock = threading.Lock()


def _touch(key):
    with _lock:
        _active[key] = time.time()


def _active_markets():
    now = time.time()
    with _lock:
        return [k for k, t in _active.items() if now - t < ACTIVE_TTL]


def build_snapshot(key, interval="15m"):
    """Rechnet das vollstaendige Bild fuer einen Markt."""
    conf = market.MARKETS.get(key) or market.MARKETS[market.DEFAULT_MARKET]

    chain = cboe.chain(conf["chain"], ttl=120)
    gexp = gex.profile(chain)
    bars, src = market.bars(key, interval)

    # ATR bevorzugt aus Tagesbars; wenn Yahoo drosselt, aus der
    # Intraday-Spanne hochgerechnet statt auf null zu fallen.
    daily, _ = market.bars(key, "1d")
    if len(daily) > 15:
        atr_v = market.atr(daily)
        session = market.session_levels(daily)
    else:
        atr_v = market.atr(bars) * 4 if bars else 0.0
        session = {}
    session.update(market.overnight_range(bars))

    vp = market.volume_profile(bars) if bars else {}
    spot = (gexp.get("spot") if gexp.get("ok") else None) or (bars[-1]["c"] if bars else None)

    zone_list, levels = zones.build(gexp, vp, session, atr_v, spot) if spot else ([], [])
    digest = news.digest()

    snap = {
        "market": key,
        "market_name": conf["name"],
        "interval": interval,
        "spot": spot,
        "atr": atr_v,
        "bars": bars,
        "bar_source": src,
        "vp": vp,
        "session": session,
        "zones": zone_list,
        "levels": levels,
        "calendar": digest["calendar"],
        "headlines": digest["headlines"],
        "ts": datetime.now(timezone.utc).isoformat(),
        "delayed_minutes": 15,
    }
    # Session-Marken flach mitfuehren, damit Agent und Frontend sie ohne
    # Umweg ueber das verschachtelte Dict lesen koennen.
    snap.update({k: session.get(k) for k in ("on_high", "on_low", "on_day",
                                             "pdh", "pdl", "pwh", "pwl")})

    if gexp.get("ok"):
        snap.update({
            "regime": gexp["regime"],
            "net_gex": gexp["net_gex"],
            "net_gex_zdte": gexp["net_gex_zdte"],
            "flip": gexp["flip"],
            "curve": gexp["curve"],
            "call_wall": gexp["call_walls"][0]["price"] if gexp["call_walls"] else None,
            "put_wall": gexp["put_walls"][0]["price"] if gexp["put_walls"] else None,
            "call_walls": gexp["call_walls"],
            "put_walls": gexp["put_walls"],
            "max_pain": gexp["max_pain"],
            "gamma_pin": gexp["gamma_pin"],
            "pcr": gexp["pcr"],
            "greeks": gexp["greeks"],
            "expiries": gexp["expiries"],
            "fresh_flow": gexp["fresh_flow"],
            "rows": gexp["rows"],
            "chain_ts": gexp["ts"],
            "chain_stale": gexp["stale"],
        })
    else:
        snap["regime"] = None
        snap["chain_stale"] = True

    # Zonenbuch zuletzt: es friert die Gamma-Felder ein, die erst oben
    # gesetzt wurden. Erst dadurch sind die Waende ueber den Tag hinweg
    # dieselben Zahlen - ohne das wandert jede Marke mit jedem Snapshot.
    snap["fixed"] = BOOK.update(key, snap)
    return snap


def snapshot(key, interval="15m", max_age=25):
    """Snapshot aus dem Cache oder frisch gerechnet."""
    _touch(key)
    with _lock:
        hit = _snapshots.get((key, interval))
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    snap = build_snapshot(key, interval)
    with _lock:
        _snapshots[(key, interval)] = (time.time(), snap)
    return snap


def _scan_loop():
    """Beobachtungstakt: lokal vergleichen, nur bei Anlass melden."""
    while True:
        try:
            for key in _active_markets() or []:
                snap = snapshot(key, "15m", max_age=SCAN_INTERVAL)
                AGENT.observe(key, snap)
                AGENT.maybe_update_plan(key, snap)
                if datetime.now().hour >= 8:
                    AGENT.plan(key, snap)
        except Exception:
            pass  # ein Fehler im Takt darf den Takt nicht beenden
        time.sleep(SCAN_INTERVAL)


threading.Thread(target=_scan_loop, daemon=True).start()
keepalive.start()


# ------------------------------------------------------------------ Routen
@app.route("/health")
def health():
    return jsonify({"ok": True, "active": _active_markets(),
                    "budget": AGENT.budget(),
                    "keepalive": keepalive.status()})


@app.route("/api/markets")
def markets():
    return jsonify([{"key": k, "name": v["name"], "chain": v["chain"]}
                    for k, v in market.MARKETS.items()])


@app.route("/api/state")
def state():
    key = request.args.get("market", market.DEFAULT_MARKET)
    if key not in market.MARKETS:
        return jsonify({"error": "unbekannter Markt"}), 400
    interval = request.args.get("tf", "15m")
    if interval not in market.INTERVALS:
        return jsonify({"error": "unbekanntes Intervall"}), 400
    snap = dict(snapshot(key, interval))
    if request.args.get("light") == "1":
        snap.pop("bars", None)
        snap.pop("rows", None)
        snap.pop("curve", None)
    return jsonify(snap)


@app.route("/api/candles")
def candles():
    key = request.args.get("market", market.DEFAULT_MARKET)
    interval = request.args.get("tf", "15m")
    bars, src = market.bars(key, interval)
    return jsonify({"bars": bars, "source": src})


@app.route("/api/overview")
def overview():
    """Kurs und Tagesveraenderung aller Maerkte - fuer die Marktleiste.

    Nutzt bewusst nur den Intraday-Endpunkt (rund 100 KB je Markt) statt
    der vollen Ketten (6 MB je Markt). Regime und Waende gibt es erst,
    wenn ein Markt wirklich geoeffnet wird.
    """
    out = []
    for key, conf in market.MARKETS.items():
        bars, _ = cboe.intraday(conf["chain"], ttl=90)
        last = bars[-1]["c"] if bars else None
        first = bars[0]["o"] if bars else None
        out.append({
            "key": key, "name": conf["name"], "chain": conf["chain"],
            "spot": last,
            "change": (last - first) if (last and first) else None,
            "change_pct": ((last / first - 1) * 100) if (last and first) else None,
        })
    return jsonify(out)


@app.route("/api/book")
def book():
    key = request.args.get("market", market.DEFAULT_MARKET)
    return jsonify(BOOK.view(key, snapshot(key)))


@app.route("/api/agent/messages")
def agent_messages():
    key = request.args.get("market", market.DEFAULT_MARKET)
    _touch(key)
    return jsonify({"messages": AGENT.messages(key), "budget": AGENT.budget()})


@app.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    data = request.get_json(silent=True) or {}
    key = data.get("market", market.DEFAULT_MARKET)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "leere Frage"}), 400
    snap = snapshot(key)
    return jsonify({"message": AGENT.ask(key, text, snap)})


@app.route("/api/agent/plan")
def agent_plan():
    key = request.args.get("market", market.DEFAULT_MARKET)
    plan = AGENT.get_plan(key)
    if not plan or request.args.get("force") == "1":
        plan = AGENT.plan(key, snapshot(key), force=True)
    return jsonify(plan or {})


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC, path)


def main():
    port = int(os.environ.get("PORT", "8770"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
