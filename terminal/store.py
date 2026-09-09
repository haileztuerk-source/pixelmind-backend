"""Zustandsspeicher: Datei oder Postgres, je nach Umgebung.

Der Free-Plan von Render hat keine persistente Festplatte - bei jedem
Neustart waeren Zonenbuch, Chat-Verlauf und Tagesplan weg. Damit haelt
der Pruefungszaehler ueber Tage sein Versprechen nicht: "siebter Test"
waere in Wahrheit "siebter Test seit dem letzten Neustart".

Ist `DATABASE_URL` gesetzt, liegt der Zustand stattdessen in Postgres.
Ohne die Variable bleibt alles beim Dateiverhalten - lokal entwickeln
soll keine Datenbank brauchen.

Neon faehrt seine kostenlosen Instanzen nach kurzer Ruhe herunter und
beim naechsten Zugriff wieder hoch. Deshalb wird je Vorgang frisch
verbunden und ein erster Fehlschlag stillschweigend wiederholt: der
Weckvorgang selbst darf keinen Datenverlust verursachen.
"""

import os
import json
import time
import threading

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TABLE = os.environ.get("STATE_TABLE", "terminal_state")
CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "12"))

_pg = None
if DATABASE_URL:
    try:
        import psycopg
        _pg = psycopg
    except ImportError:
        _pg = None

_init_done = False
_init_lock = threading.Lock()


def backend():
    return "postgres" if (_pg and DATABASE_URL) else "file"


def _is_local(url):
    return "@localhost" in url or "@127.0.0.1" in url or "host=localhost" in url


def _connect():
    # Neon verlangt TLS. Bei einer lokalen Datenbank wuerde derselbe
    # Parameter die Verbindung verhindern - dort also weglassen.
    url = DATABASE_URL
    if "sslmode=" not in url and not _is_local(url):
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return _pg.connect(url, connect_timeout=CONNECT_TIMEOUT)


def _ensure_table():
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        with _connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                " key TEXT PRIMARY KEY,"
                " value JSONB NOT NULL,"
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.commit()
        _init_done = True


class Store:
    """Ein benannter JSON-Zustand. Gleiche Schnittstelle fuer beide Wege."""

    def __init__(self, key, path):
        self.key = key
        self.path = path
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- lesen
    def load(self, default=None):
        default = {} if default is None else default
        if backend() == "postgres":
            for attempt in (0, 1):
                try:
                    _ensure_table()
                    with _connect() as conn:
                        row = conn.execute(
                            f"SELECT value FROM {TABLE} WHERE key = %s",
                            (self.key,)).fetchone()
                    return row[0] if row else default
                except Exception:
                    if attempt == 0:
                        time.sleep(1.5)   # Neon faehrt gerade hoch
                        continue
                    return self._load_file(default)
        return self._load_file(default)

    def _load_file(self, default):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return default

    # -------------------------------------------------------------- schreiben
    def save(self, data):
        if backend() == "postgres":
            for attempt in (0, 1):
                try:
                    _ensure_table()
                    with _connect() as conn:
                        conn.execute(
                            f"INSERT INTO {TABLE} (key, value, updated_at)"
                            " VALUES (%s, %s, now())"
                            " ON CONFLICT (key) DO UPDATE"
                            " SET value = EXCLUDED.value, updated_at = now()",
                            (self.key, json.dumps(data, ensure_ascii=False)))
                        conn.commit()
                    return True
                except Exception:
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    break
        return self._save_file(data)

    def _save_file(self, data):
        # Atomar ueber tmp + os.replace, damit ein Absturz mitten im
        # Schreiben keinen halben Zustand hinterlaesst.
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False


def health():
    """Kurzer Selbsttest fuer /health."""
    info = {"backend": backend(), "table": TABLE if backend() == "postgres" else None}
    if backend() == "postgres":
        try:
            _ensure_table()
            with _connect() as conn:
                n = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
            info["ok"] = True
            info["rows"] = n
        except Exception as exc:
            info["ok"] = False
            info["error"] = type(exc).__name__
    else:
        info["ok"] = True
        info["note"] = "Dateispeicher - ueberlebt keinen Neustart des Containers"
    return info
