"""Wachhalten - aber nur, wenn es etwas zu beobachten gibt.

Der Free-Plan von Render fährt einen Dienst nach 15 Minuten ohne
eingehende Anfrage herunter. Der Agent beobachtet dann nicht mehr.

Der übliche Trick ist ein Ping-Dienst, der rund um die Uhr anklopft.
Das geht hier NICHT auf: das Freikontingent sind 750 Instanzstunden im
Monat für den gesamten Account, ein Monat hat 744. Ein einziger
durchlaufender Dienst frisst also alles - und der Bilddienst im selben
Account bekäme nichts mehr ab.

Deshalb wird nur während der US-Handelszeit wachgehalten. Das ist
ohnehin das Fenster, in dem Zonen, Wände und Regime etwas bedeuten:

    9 Stunden × 22 Handelstage ≈ 198 Stunden im Monat

Damit bleiben über 550 Stunden für alles andere. Außerhalb des Fensters
schläft der Dienst und wacht beim ersten Aufruf von selbst auf.
"""

import os
import time
import threading
from datetime import datetime, timezone

import requests

# Render setzt RENDER_EXTERNAL_URL selbst. Sonst eigene URL hinterlegen.
URL = (os.environ.get("KEEPALIVE_URL")
       or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
ENABLED = os.environ.get("KEEPALIVE", "1") not in ("0", "false", "no")
# UTC. Die US-Kassabörse läuft 13:30-20:00 UTC; der Rand deckt
# Vorbörse und Abrechnung mit ab.
WINDOW = os.environ.get("KEEPALIVE_HOURS", "12-21")
WEEKDAYS_ONLY = os.environ.get("KEEPALIVE_WEEKDAYS", "1") not in ("0", "false", "no")
INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "600"))

try:
    _from, _to = (int(x) for x in WINDOW.split("-"))
except ValueError:
    _from, _to = 12, 21


def in_window(now=None):
    """Liegt der Zeitpunkt im Wachhalte-Fenster?"""
    now = now or datetime.now(timezone.utc)
    if WEEKDAYS_ONLY and now.weekday() > 4:
        return False
    return _from <= now.hour < _to


def status():
    return {
        "enabled": bool(ENABLED and URL),
        "url": URL or None,
        "window_utc": f"{_from:02d}:00-{_to:02d}:00",
        "weekdays_only": WEEKDAYS_ONLY,
        "in_window": in_window(),
        "budget_note": "rund 198 von 750 Instanzstunden im Monat",
    }


def _loop():
    while True:
        try:
            if in_window():
                requests.get(f"{URL}/health", timeout=20)
        except Exception:
            pass   # ein verpasster Ping ist folgenlos, der nächste kommt
        time.sleep(INTERVAL)


def start():
    """Startet den Ping-Thread, wenn eine erreichbare URL bekannt ist."""
    if not (ENABLED and URL):
        return False
    threading.Thread(target=_loop, daemon=True).start()
    return True
