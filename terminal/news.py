"""Nachrichtenlage: Wirtschaftskalender und Schlagzeilen, beide keyfrei."""

import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

UA = "Mozilla/5.0 (compatible; ChartTerminal/1.0)"
CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = [
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

# Nur was den Index bewegt. Ohne Filter besteht der Digest aus
# Einzelaktien-Meldungen, die fuer ein Index-Terminal nichts aussagen.
KEYWORDS = (
    "fed", "fomc", "inflation", "cpi", "ppi", "jobs", "payroll", "unemployment",
    "rate", "yield", "treasury", "gdp", "recession", "tariff", "powell",
    "nasdaq", "s&p", "dow", "stocks", "market", "nvidia", "apple", "microsoft",
    "amazon", "meta", "google", "alphabet", "tesla", "ai ", "chip", "semiconductor",
)
RELEVANT_CURRENCIES = {"USD", "ALL"}

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        return hit[1] if hit else None
    with _lock:
        _cache[key] = (now, val)
    return val


def calendar(hours=12):
    """Hochgewichtete Termine der naechsten `hours` Stunden."""
    def fetch():
        r = requests.get(CALENDAR, headers={"User-Agent": UA}, timeout=20)
        return r.json() if r.ok else []

    data = _cached("calendar", 900, fetch) or []
    now = datetime.now(timezone.utc)
    out = []
    for ev in data:
        if ev.get("country") not in RELEVANT_CURRENCIES:
            continue
        if (ev.get("impact") or "").lower() not in ("high", "medium"):
            continue
        try:
            when = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except (ValueError, KeyError, TypeError):
            continue
        mins = (when - now).total_seconds() / 60.0
        if -30 <= mins <= hours * 60:
            out.append({
                "title": ev.get("title"), "impact": ev.get("impact"),
                "country": ev.get("country"), "when": when.isoformat(),
                "in_minutes": round(mins),
                "forecast": ev.get("forecast") or "", "previous": ev.get("previous") or "",
            })
    out.sort(key=lambda e: e["in_minutes"])
    return out


def headlines(limit=12):
    """Gefilterte Schlagzeilen aus den RSS-Quellen."""
    def fetch():
        items = []
        for source, url in FEEDS:
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
                if not r.ok:
                    continue
                root = ET.fromstring(r.content)
            except Exception:
                continue
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                if not any(k in title.lower() for k in KEYWORDS):
                    continue
                items.append({
                    "source": source, "title": title,
                    "link": (it.findtext("link") or "").strip(),
                    "date": (it.findtext("pubDate") or "").strip(),
                })
        return items

    items = _cached("headlines", 300, fetch) or []
    seen, out = set(), []
    for it in items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        out.append(it)
        if len(out) >= limit:
            break
    return out


def digest():
    """Ein Block fuer den Agenten: Kalender plus Schlagzeilen."""
    return {"calendar": calendar(), "headlines": headlines()}
