"""Live-Kurse aus cTrader - FTMO und jeder andere cTrader-Broker.

WOZU

Die Bruecke liefert den eigenen Broker-Kurs, verlangt dafuer aber einen
laufenden PC: MetaTrader spricht nur mit dem Programm auf demselben
Rechner. OANDA braucht keinen PC, liefert aber einen FREMDEN CFD.

cTrader kann beides: es ist der Kurs des eigenen Brokers, und die Open
API spricht ueber das Netz - der Server holt sie selbst. Damit laeuft
der eigene Kurs auch dann, wenn zuhause alles aus ist.

Das gilt nicht nur fuer FTMO. Jeder Broker im cTrader-Netz geht, und
das ist bewusst so gebaut: sollte FTMO den API-Zugang fuer seine Konten
sperren - Prop-Firmen tun das mitunter -, reicht ein kostenloses
Demokonto bei einem anderen cTrader-Broker.

DER WEG IN ZWEI TEILEN

  1. Anmeldung (dieses Modul, oberer Teil). OAuth ueber openapi.ctrader.com:
     einmal im Browser zustimmen, danach haelt der Dienst einen
     Zugangsschluessel und erneuert ihn selbst.
  2. Kursstrom (unterer Teil). Eine dauerhafte WebSocket-Verbindung, die
     Kurse schickt, sobald sie sich aendern - kein Abfragen im Takt.

WAS DER NUTZER EINSTELLT

    CTRADER_CLIENT_ID      aus der App-Registrierung
    CTRADER_CLIENT_SECRET  dito
    CTRADER_ENV            demo (Vorgabe) oder live
    CTRADER_SYMBOL         Name des Nasdaq-CFD, Vorgabe "US100"

Danach einmal /api/ctrader/login im Browser oeffnen und zustimmen.
Ohne Kennung und Geheimnis bleibt das Modul still.
"""

import json
import os
import threading
import time

import requests

from .live import FEED
from .store import Store

# --- Adressen --------------------------------------------------------------
AUTH_HOST = "https://openapi.ctrader.com"
STROM = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PORT = 5036                 # JSON ueber WebSocket; 5035 waere Protobuf

# --- Nachrichtenarten der Open API ----------------------------------------
# Die Zahlen sind das Protokoll; sie hier zu benennen macht den Ablauf
# unten lesbar und haelt sie an einer Stelle korrigierbar.
APP_AUTH_REQ, APP_AUTH_RES = 2100, 2101
ACC_AUTH_REQ, ACC_AUTH_RES = 2102, 2103
SYMBOLS_REQ, SYMBOLS_RES = 2114, 2115
SUBSCRIBE_REQ, SUBSCRIBE_RES = 2127, 2128
SPOT_EVENT = 2131
TRENDBAR_REQ, TRENDBAR_RES = 2137, 2138
ACCOUNTS_REQ, ACCOUNTS_RES = 2149, 2150
FEHLER_RES = 2142
HEARTBEAT = 51

# Kurse kommen als Ganzzahl mal hunderttausend - so vermeidet das
# Protokoll Gleitkomma auf dem Draht.
SKALA = 100000.0
M1 = 1                      # ProtoOATrendbarPeriod.M1

MARKT = "NQ"                # bisher nur der Nasdaq; die anderen Maerkte
                            # haben bei jedem Broker andere Symbolnamen

TOKENS = Store("ctrader_tokens", "ctrader_tokens.json")

_lock = threading.Lock()
_status = {"stufe": "nicht eingerichtet", "fehler": None, "symbol": None}
_läuft = False


# ====================================================================== Anmeldung
def client_id():
    return os.environ.get("CTRADER_CLIENT_ID") or ""


def client_secret():
    return os.environ.get("CTRADER_CLIENT_SECRET") or ""


def umgebung():
    return "live" if (os.environ.get("CTRADER_ENV") or "demo").lower() == "live" else "demo"


def symbol_name():
    return os.environ.get("CTRADER_SYMBOL") or "US100"


def configured():
    return bool(client_id() and client_secret())


def _setz(stufe, fehler=None, **rest):
    with _lock:
        _status["stufe"] = stufe
        _status["fehler"] = fehler
        _status.update(rest)


def status():
    with _lock:
        s = dict(_status)
    s["configured"] = configured()
    s["angemeldet"] = bool((TOKENS.load() or {}).get("access_token"))
    s["env"] = umgebung()
    return s


def auth_url(redirect_uri):
    """Die Adresse, an der der Nutzer einmal zustimmt."""
    from urllib.parse import urlencode
    return AUTH_HOST + "/apps/auth?" + urlencode({
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "scope": "accounts",
    })


def _token_tausch(daten):
    """Holt oder erneuert den Zugangsschluessel und legt ihn ab.

    Abgelegt wird in der Datenbank, nicht in einer Datei: die Platte des
    Dienstes ist fluechtig, und ein Schluessel, der jeden Neustart
    verloren geht, waere kein Zugang, sondern eine taegliche Pflicht.
    """
    try:
        r = requests.post(AUTH_HOST + "/apps/token", data=daten, timeout=20)
        d = r.json() if r.content else {}
    except Exception as exc:
        _setz("Anmeldung fehlgeschlagen", "Netz: %s" % type(exc).__name__)
        return None
    if not r.ok or d.get("errorCode") or not d.get("accessToken"):
        _setz("Anmeldung fehlgeschlagen",
              str(d.get("description") or d.get("errorCode") or r.status_code))
        return None
    alt = TOKENS.load() or {}
    neu = {
        "access_token": d["accessToken"],
        "refresh_token": d.get("refreshToken") or alt.get("refresh_token"),
        # Ablauf als Zeitpunkt, nicht als Dauer: eine Dauer ist ab dem
        # Neustart des Dienstes wertlos.
        "expires_at": time.time() + float(d.get("expiresIn") or 2592000) - 300,
    }
    TOKENS.save(neu)
    _setz("angemeldet")
    return neu


def einloesen(code, redirect_uri):
    """Den Code aus der Weiterleitung gegen einen Schluessel tauschen."""
    return _token_tausch({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id(), "client_secret": client_secret(),
    })


def _schluessel():
    """Gueltiger Zugangsschluessel, notfalls erneuert."""
    t = TOKENS.load() or {}
    if not t.get("access_token"):
        return None
    if time.time() < (t.get("expires_at") or 0):
        return t["access_token"]
    if not t.get("refresh_token"):
        return None
    neu = _token_tausch({
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        "client_id": client_id(), "client_secret": client_secret(),
    })
    return (neu or {}).get("access_token")


def abmelden():
    TOKENS.save({})
    _setz("abgemeldet")


# ====================================================================== Kursstrom
class Strom:
    """Eine Sitzung am cTrader-Strom.

    Der Ablauf ist festgelegt und muss in dieser Reihenfolge geschehen:
    App anmelden, Konten holen, Konto anmelden, Symbol suchen, Kerzen
    holen, Kurse abonnieren. Jede Antwort schaltet den naechsten Schritt
    frei - deshalb ein Zustand je Sitzung statt einer geraden Folge.
    """

    def __init__(self, ws, token):
        self.ws = ws
        self.token = token
        self.konto = None
        self.symbol_id = None
        self.digits = 5
        self.zaehler = 0

    def sende(self, typ, nutzlast=None):
        self.zaehler += 1
        self.ws.send(json.dumps({
            "clientMsgId": str(self.zaehler),
            "payloadType": typ,
            "payload": nutzlast or {},
        }))

    # -- Schritt fuer Schritt ------------------------------------------
    def start(self):
        self.sende(APP_AUTH_REQ, {"clientId": client_id(),
                                  "clientSecret": client_secret()})

    def behandle(self, nachricht):
        typ = nachricht.get("payloadType")
        p = nachricht.get("payload") or {}

        if typ == FEHLER_RES:
            _setz("Fehler vom Broker",
                  str(p.get("description") or p.get("errorCode")))
            return

        if typ == APP_AUTH_RES:
            _setz("App angemeldet")
            self.sende(ACCOUNTS_REQ, {"accessToken": self.token})

        elif typ == ACCOUNTS_RES:
            kontos = p.get("ctidTraderAccount") or []
            if not kontos:
                _setz("kein Konto", "Der Schluessel gehoert zu keinem Konto")
                return
            self.konto = kontos[0].get("ctidTraderAccountId")
            _setz("Konto gefunden", None, konto=self.konto)
            self.sende(ACC_AUTH_REQ, {"ctidTraderAccountId": self.konto,
                                      "accessToken": self.token})

        elif typ == ACC_AUTH_RES:
            _setz("Konto angemeldet")
            self.sende(SYMBOLS_REQ, {"ctidTraderAccountId": self.konto,
                                     "includeArchivedSymbols": False})

        elif typ == SYMBOLS_RES:
            self._waehle_symbol(p.get("symbol") or [])

        elif typ == TRENDBAR_RES:
            self._kerzen(p)

        elif typ == SPOT_EVENT:
            self._spot(p)

    def _waehle_symbol(self, liste):
        """Das gesuchte Symbol finden - Broker benennen es verschieden.

        US100, NAS100, USTEC, NASDAQ100: dieselbe Sache, vier Namen. Der
        Nutzer stellt einen ein; findet sich der nicht wortgleich, wird
        unter den ueblichen gesucht, und erst dann aufgegeben - mit der
        Liste dessen, was es gaebe.
        """
        wunsch = symbol_name().upper()
        namen = {(s.get("symbolName") or "").upper(): s for s in liste}
        treffer = namen.get(wunsch)
        if not treffer:
            for kandidat in ("US100", "NAS100", "USTEC", "NAS100.f",
                             "NASDAQ100", "US_TECH_100", "USTECH100"):
                if kandidat.upper() in namen:
                    treffer = namen[kandidat.upper()]
                    break
        if not treffer:
            nah = [n for n in namen if "100" in n or "NAS" in n or "TEC" in n]
            _setz("Symbol nicht gefunden",
                  "%s unbekannt. Vorhanden waere: %s"
                  % (wunsch, ", ".join(sorted(nah)[:10]) or "(nichts passendes)"))
            return
        self.symbol_id = treffer.get("symbolId")
        self.digits = treffer.get("digits", 5)
        _setz("laeuft", None, symbol=treffer.get("symbolName"))

        # Erst die Historie, dann der laufende Kurs.
        jetzt = int(time.time() * 1000)
        self.sende(TRENDBAR_REQ, {
            "ctidTraderAccountId": self.konto, "symbolId": self.symbol_id,
            "period": M1, "fromTimestamp": jetzt - 8 * 3600 * 1000,
            "toTimestamp": jetzt,
        })
        self.sende(SUBSCRIBE_REQ, {"ctidTraderAccountId": self.konto,
                                   "symbolId": [self.symbol_id]})

    def _kerzen(self, p):
        balken = []
        for b in p.get("trendbar") or []:
            try:
                tief = float(b["low"]) / SKALA
                o = tief + float(b.get("deltaOpen") or 0) / SKALA
                h = tief + float(b.get("deltaHigh") or 0) / SKALA
                c = tief + float(b.get("deltaClose") or 0) / SKALA
                t = int(b["utcTimestampInMinutes"]) * 60
            except (KeyError, TypeError, ValueError):
                continue
            if min(o, h, tief, c) <= 0 or h < tief:
                continue
            balken.append({"t": t, "o": o, "h": h, "l": tief, "c": c,
                           "v": float(b.get("volume") or 0.0)})
        if balken:
            FEED.put_bars(MARKT, _status.get("symbol") or symbol_name(),
                          balken, src="ctrader")

    def _spot(self, p):
        """Kurs als Mitte zwischen Geld und Brief.

        Nicht jedes Ereignis traegt beide Seiten - cTrader schickt nur,
        was sich geaendert hat. Fehlt eine, gilt die andere allein;
        andernfalls fiele der Kurs bei jedem einseitigen Ereignis aus.
        """
        geld = p.get("bid")
        brief = p.get("ask")
        werte = [float(x) / SKALA for x in (geld, brief) if x is not None]
        if not werte:
            return
        FEED.put_tick(MARKT, _status.get("symbol") or symbol_name(),
                      sum(werte) / len(werte), src="ctrader")


def _sitzung():
    """Eine Verbindung von Anfang bis Ende. Wirft bei Abbruch."""
    import websocket          # erst hier, damit das Modul ohne Paket laedt

    token = _schluessel()
    if not token:
        _setz("nicht angemeldet",
              "Einmal /api/ctrader/login im Browser oeffnen")
        return

    ws = websocket.create_connection(
        "wss://%s:%d" % (STROM[umgebung()], PORT), timeout=30)
    sitzung = Strom(ws, token)
    sitzung.start()
    letzter_schlag = time.time()
    try:
        while True:
            # Die Bruecke hat Vorrang: laeuft sie, hier nichts schreiben.
            st = FEED.state(MARKT)
            if st.get("live") and st.get("src") == "bridge":
                time.sleep(5)
                continue

            ws.settimeout(10)
            try:
                roh = ws.recv()
                if roh:
                    sitzung.behandle(json.loads(roh))
            except Exception as exc:
                if "timed out" not in str(exc).lower():
                    raise

            # Herzschlag, sonst trennt die Gegenseite nach 30 Sekunden.
            if time.time() - letzter_schlag > 20:
                sitzung.sende(HEARTBEAT)
                letzter_schlag = time.time()
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _lauf():
    """Haelt die Verbindung. Faellt sie, wird neu aufgebaut - mit Abstand.

    Der Abstand waechst bis zu einer Minute. Ohne ihn haemmerte ein
    dauerhafter Fehler - falsches Symbol, gesperrter Zugang - im
    Sekundentakt gegen den Broker.
    """
    wartezeit = 5
    while True:
        try:
            _sitzung()
            wartezeit = 5
        except Exception as exc:
            _setz("Verbindung verloren", "%s" % type(exc).__name__)
            wartezeit = min(60, wartezeit * 2)
        time.sleep(wartezeit)


def start():
    global _läuft
    if _läuft or not configured():
        return False
    try:
        import websocket  # noqa: F401
    except ImportError:
        _setz("Paket fehlt", "websocket-client ist nicht installiert")
        return False
    _läuft = True
    _setz("verbinde")
    threading.Thread(target=_lauf, daemon=True).start()
    return True
