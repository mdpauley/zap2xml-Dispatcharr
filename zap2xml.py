#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
zap2xml.py — Fetch Zap2it/Gracenote grid and emit XMLTV.

- Input: --lineupId (also accepts --lineup-id)
  * If lineup contains OTA/LOCALBROADCAST → API uses <COUNTRY>-lineupId-DEFAULT with headendId='lineupId'; ZIP required.
  * Else headendId is derived (e.g., USA-DITV501-X → DITV501).
- Only --timespan controls coverage (in hours). No --days.
- -d / --delay = seconds to sleep between chunk requests (default 0).
- Default sort by callSign.
- Pretty XML with fields: title, sub-title, desc, date, category, length, icon, url,
  episode-num (dd_progid, xmltv_ns, onscreen, common), previously-shown (when appropriate),
  audio, subtitles, rating.

Episode number rules:
- dd_progid: seriesId + "." + last4(tmsId) when available.
- xmltv_ns:
  * If Season & Episode → season-1.episode-1.
  * If Episode only → (year-1).(episode-1).
  * Else fallback to date → YYYY-1.MMDD-1.  (e.g., 2025-09-12 → 2024.0911.)
- onscreen and common both two-digit (S01E03) when S & E present.

v2.2.0 additions:
- Proxy support via ZAP2XML_PROXY_LIST env var (JSON array or comma-separated list)
  Supports HTTP/Squid and SOCKS5 proxies with round-robin rotation.
"""

import argparse
import datetime as _dt
import json
import os
import random
import re
import sys
import time
from pathlib import Path
# === v2.1.0 additions ===
# Simple display-name rewrite based on callSign -> station_name mapping (DB lookup by callSign only).
import sqlite3, atexit, signal, contextlib, fcntl, shutil

# Proxy support - can be set via ZAP2XML_PROXY_LIST env var
# Format: JSON array of proxy URLs, e.g., ["http://proxy1:port", "socks5://proxy2:port"]
def _get_proxies():
    proxy_list = os.environ.get('ZAP2XML_PROXY_LIST', '')
    if not proxy_list:
        return None
    try:
        # Try parsing as JSON array first
        proxies = json.loads(proxy_list)
        if isinstance(proxies, list):
            proxies = [p.strip() for p in proxies if p.strip()]
            return proxies if proxies else None
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback to comma-separated string
    proxies = [p.strip() for p in proxy_list.split(',') if p.strip()]
    return proxies if proxies else None

def _log_proxies(proxies):
    """Log all proxies being used on startup."""
    if not proxies:
        print("[zap2xml] No proxies configured", flush=True)
        return
    print(f"[zap2xml] Proxy list ({len(proxies)} proxy/ies):", flush=True)
    for i, proxy in enumerate(proxies, 1):
        # Mask credentials if present
        if '@' in proxy:
            parts = proxy.split('@')
            scheme = parts[0]
            creds = parts[1].split(':')[0] if ':' in parts[1] else 'user'
            masked = f"{scheme}://{creds}:***@{parts[1].split('@')[1]}" if len(parts) > 1 else proxy
            print(f"  [{i}] {masked}", flush=True)
        else:
            print(f"  [{i}] {proxy}", flush=True)

def _get_proxy_session(proxies=None):
    """Create a session with proxy support. Rotates through proxies if list provided."""
    sess = requests.Session()
    if proxies and len(proxies) > 0:
        # Round-robin proxy selection
        proxy_idx = getattr(_get_proxy_session, '_idx', 0)
        proxy = proxies[proxy_idx % len(proxies)]
        setattr(_get_proxy_session, '_idx', proxy_idx + 1)
        
        # Determine proxy type
        if proxy.lower().startswith('socks5'):
            sess.proxies = {
                'http': proxy,
                'https': proxy,
            }
        else:
            # HTTP/HTTPS proxy
            sess.proxies = {
                'http': proxy,
                'https': proxy,
            }
        print(f"[zap2xml] Using proxy: {proxy}", flush=True)
    return sess

def _load_callsign_to_stationname():
    lut = {}
    try:
        db_abs = Path('/data/plugins/zap2xml/channels.db')  # preferred
        db_rel = Path(__file__).parent / 'channels.db'      # fallback
        dbp = db_abs if db_abs.exists() else db_rel
        conn = sqlite3.connect(str(dbp))
        cur = conn.cursor()
        # callsign may appear in various cases; normalize to upper
        rows = cur.execute('SELECT callsign, station_name FROM channels_by_country').fetchall()
        for cs, nm in rows:
            if cs is None: 
                continue
            lut[str(cs).strip().upper()] = nm
        conn.close()
        try:
            print(f"[zap2xml] v2.1.0: loaded callSign→station_name LUT from {dbp} (rows={len(lut)})", flush=True)
        except Exception:
            pass
    except Exception as e:
        try:
            print(f"[zap2xml] v2.1.0: failed to load channels.db: {e}", flush=True)
        except Exception:
            pass
    return lut


def _rewrite_first_display_name_with_station_name(root):
    """Replace FIRST <display-name> (callSign) with station_name from DB,
    and if there is a "{callSign} {affiliate}" entry, change it to "{station_name} {affiliate}".
    The middle/affiliate-only entry is preserved unchanged.
    """
    try:
        lut = _load_callsign_to_stationname()
        changed = 0
        changed_combo = 0
        for ch in root.findall('./channel'):
            dnames = ch.findall('display-name')
            if not dnames:
                continue
            # gather text values (trimmed)
            txts = [(dn.text or '').strip() for dn in dnames]
            first_txt = txts[0] if txts else ''
            if not first_txt:
                continue
            cs_key = first_txt.upper()
            station_name = lut.get(cs_key)
            # try to obtain affiliate name (middle or any entry that isn't first and isn't empty and isn't same as first)
            # we don't want to infer too aggressively; typically the affiliate is the second entry
            affiliate = None
            if len(txts) >= 2:
                # if second is not same as first and not empty, treat as affiliate
                if txts[1] and txts[1].upper() != cs_key:
                    affiliate = txts[1]
            # Replace first if we have station_name
            if station_name and station_name != first_txt:
                dnames[0].text = station_name
                changed += 1
            # Replace "{callSign} {affiliate}" combo if present anywhere (usually third)
            if station_name and affiliate:
                target_combo_cs = f"{first_txt} {affiliate}".strip()
                target_combo_sn = f"{station_name} {affiliate}".strip()
                # search for any dn equal (case-sensitive compare first, then fallback to case-insensitive)
                for i in range(1, len(dnames)):
                    t = (dnames[i].text or '').strip()
                    if not t:
                        continue
                    if t == target_combo_cs or t.upper() == target_combo_cs.upper():
                        if t != target_combo_sn:
                            dnames[i].text = target_combo_sn
                            changed_combo += 1
                            break
        try:
            print(f"[zap2xml] v2.1.0: first display-name replacements applied: {changed}", flush=True)
            print(f"[zap2xml] v2.1.2: combo replacements applied: {changed_combo}", flush=True)
        except Exception:
            pass
    except Exception as e:
        try:
            print(f"[zap2xml] v2.1.2: rewrite step failed: {e}", flush=True)
        except Exception:
            pass

# Robust single-instance guard (directory lock + file lock fallback)
# Directory lock (atomic) + file lock (fcntl fallback)
_LOCK_DIR = "/data/plugins/zap2xml.lock.d"
_LOCK_FILE = "/data/plugins/zap2xml.run.lock"

def _release_locks_v210():
    try:
        if os.path.isdir(_LOCK_DIR):
            shutil.rmtree(_LOCK_DIR)
    except Exception:
        pass
    try:
        if os.path.exists(_LOCK_FILE):
            os.remove(_LOCK_FILE)
    except Exception:
        pass

@contextlib.contextmanager
def _single_instance_guard_v210():
    # Preferred: atomic dir lock (mkdir is atomic on Unix)
    try:
        os.makedirs('/data/plugins', exist_ok=True)
        os.mkdir(_LOCK_DIR)
        atexit.register(_release_locks_v210)
        signal.signal(signal.SIGTERM, lambda *a, **k: (_release_locks_v210(), os._exit(0)))
        signal.signal(signal.SIGINT,  lambda *a, **k: (_release_locks_v210(), os._exit(0)))
        yield
        _release_locks_v210()
        return
    except FileExistsError:
        print('[zap2xml] v2.1.0: another instance detected (dir lock); exiting.', flush=True)
        raise SystemExit(0)
    except Exception:
        pass
    
    # Fallback: file lock using fcntl
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode('utf-8'))
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass
            try:
                os.remove(_LOCK_FILE)
            except Exception:
                pass
    except BlockingIOError:
        print('[zap2xml] v2.1.0: another instance detected (file lock); exiting.', flush=True)
        raise SystemExit(0)

from typing import Any, Dict, List, Optional, Tuple

import requests
import xml.etree.ElementTree as ET

BASE_URL = "https://tvlistings.gracenote.com/api/grid"

# Top 20 user-agents from https://microlink.io/user-agents?type=user
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.5938.132 Safari/537.36",
]
COUNTRY_3 = {"US": "USA", "CA": "CAN"}  # normalize 2→3 if someone passes US/CA
VERBOSE = 0

def _ua() -> str:
    return os.environ.get('ZAP2XML_PLUGIN_UA') or random.choice(USER_AGENTS)
def _ensure_desktop_ua(headers: dict) -> None:
    ua = headers.get('User-Agent', '') or ''
    bad = ('TiviMate', 'Android', 'Mobile;', 'Dalvik/', 'VLC/')
    desk = ('Windows NT', 'Macintosh', 'X11; Linux')
    if any(t in ua for t in bad) and not any(t in ua for t in desk):
        headers['User-Agent'] = os.environ.get('ZAP2XML_PLUGIN_UA') or random.choice(USER_AGENTS)
        print(f"[zap2xml] force desktop UA (was {ua!r}) -> {headers['User-Agent']!r}", flush=True)

    return os.environ.get("USER_AGENT") or os.environ.get("ZAP2XML_USER_AGENT") or random.choice(USER_AGENTS)

def _v(level: int, msg: str):
    if VERBOSE >= level:
        print(f"[zap2xml] {msg}", file=sys.stderr, flush=True)

def _now() -> int:
    return int(time.time())

def _is_ota(lineup_id: str) -> bool:
    s = (lineup_id or "").upper()
    return "OTA" in s or "LOCALBROADCAST" in s

def _headend_from_lineup(lineup_id: str) -> str:
    if _is_ota(lineup_id):
        return "lineupId"
    m = re.match(r"^[A-Z]{3}-([^-]+)-", lineup_id or "")
    return m.group(1) if m else "lineup"

def _api_lineup_and_headend(country: str, lineup_id: str) -> Tuple[str, str]:
    c3 = COUNTRY_3.get(country.upper(), country.upper())
    if _is_ota(lineup_id):
        return f"{c3}-lineupId-DEFAULT", "lineupId"
    return lineup_id, _headend_from_lineup(lineup_id)

def _device_from_lineup(lineup_id: str) -> str:
    """
    OTA or ...-DEFAULT => '-'
    Otherwise, if lineup ends with -<single letter>, use that letter (e.g., -X or -L).
    Else '-'.
    """
    s = (lineup_id or "").upper().strip()
    if _is_ota(s) or s.endswith("-DEFAULT"):
        return "-"
    m = re.search(r"-([A-Z])$", s)
    return m.group(1) if m else "-"

def _build_url(lineup_id: str, headend_id: str, country: str, postal: Optional[str],
               time_sec: int, chunk_hours: int, *, is_ota: bool) -> str:
    """
    For non-OTA, include postalCode='-'.
    For OTA, include the real postal value.
    """
    device = _device_from_lineup(lineup_id)

    user_id = os.environ.get('ZAP2XML_USER_ID') or ('%08x' % __import__('random').getrandbits(32))

    params = [
        ("lineupId", lineup_id),
        ("timespan", str(chunk_hours)),
        ("headendId", headend_id),
        ("country", country),
        # ("timezone", ""),  # do not send empty timezone
        ("device", device),
        # postal next (see below)
        ("isOverride", "true"),
        ("time", str(time_sec)),
        ("pref", "16,128"),
        ("userId", user_id),
        ("aid", "chi"),
        ("languagecode", "en-us"),
    ]

    # Postal handling
    if is_ota:
        if postal:
            params.insert(6, ("postalCode", str(postal)))
    else:
        params.insert(6, ("postalCode", "-"))

    qs = "&".join(f"{requests.utils.quote(k)}={requests.utils.quote(v)}"
                  for k, v in params if v not in (None, ""))
    return f"{BASE_URL}?{qs}"

def _normalize_channel(ch: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "stationId": ch.get("stationId") or ch.get("channelId"),
        "channelId": ch.get("channelId"),
        "callSign": ch.get("callSign") or ch.get("name"),
        "channelNo": ch.get("channelNo") or ch.get("channel"),
        "affiliateName": ch.get("affiliateName"),
        "thumbnail": ch.get("thumbnail"),
        "events": [],
    }

def _merge_filter_tags_into_genres(ev: Dict[str, Any]) -> None:
    program = ev.get("program") or {}
    genres = set()
    for g in (program.get("genres") or []):
        if isinstance(g, dict) and g.get("name"):
            genres.add(str(g["name"]).lower())
        elif isinstance(g, str):
            genres.add(g.lower())
    for tag in (ev.get("filter") or []):
        genres.add(re.sub(r"^filter-", "", str(tag), flags=re.I).strip().lower())
    if genres:
        program["genres"] = sorted(list(genres))

def fetch_grid(
    *,
    country: str,
    lineup_id_input: str,
    postal: Optional[str],
    timespan: int = 72,
    delay_seconds: int = 0,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    Fetch grid in 6-hour chunks with retry on 429/5xx and clear errors on other 4xx.
    """
    c3 = COUNTRY_3.get(country.upper(), country.upper())
    if not lineup_id_input:
        raise ValueError("lineupId is required")
    if _is_ota(lineup_id_input) and not postal:
        raise ValueError("Postal/ZIP code is required for OTA (LocalBroadcast) lineups")

    lineup_api, headend_api = _api_lineup_and_headend(c3, lineup_id_input)

    total_hours = int(timespan)
    chunk_hours = 6

    # Get proxies from environment and log them
    proxies = _get_proxies()
    _log_proxies(proxies)
    sess = _get_proxy_session(proxies)
    try:
        print('[zap2xml] warm-up GET https://tvlistings.gracenote.com/', flush=True)
        _wr = sess.get('https://tvlistings.gracenote.com/', headers={'User-Agent': _ua()}, timeout=20)
        try:
            print(f"[zap2xml] cookies after warm-up: {set(c.name for c in sess.cookies)}", flush=True)
        except Exception:
            pass
    except Exception as _e:
        print(f"[zap2xml] warm-up failed: {_e}", flush=True)
    headers_base = {
        "User-Agent": _ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tvlistings.gracenote.com/",
        "Origin": "https://tvlistings.gracenote.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }

    channels_map: Dict[str, Dict[str, Any]] = {}
    base_time = _now()
    offsets = list(range(0, total_hours, chunk_hours))

    for idx, offset in enumerate(offsets):
        t = base_time + offset * 3600
        is_ota = _is_ota(lineup_id_input)
        url = _build_url(lineup_api, headend_api, c3, postal or "", t, chunk_hours, is_ota=is_ota)

        attempt = 0
        while True:
            attempt += 1
            headers = dict(headers_base)
            headers['User-Agent'] = _ua()
            _ensure_desktop_ua(headers)
            try:
                if 'User-Agent' in sess.headers:
                    del sess.headers['User-Agent']
            except Exception:
                pass
            # rotate UA on retries
            if attempt > 1:
                headers["User-Agent"] = _ua()

            # Unmasked GET line
            print(f"[zap2xml] GET chunk {idx+1}/{len(offsets)} attempt {attempt}/{max_retries} → {url}", flush=True)
            print(f"[zap2xml]   UA: {headers.get('User-Agent', '')}", flush=True)

            try:
                r = sess.get(url, headers=headers, timeout=30)
            except requests.RequestException as e:
                if attempt <= max_retries:
                    sleep_s = min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    _v(1, f"network error: {e} — retrying in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                raise

            sc = r.status_code
            if sc == 200:
                try:
                    data = r.json()
                except Exception:
                    body = (r.text or "")[:600]
                    raise RuntimeError(f"Zap2it returned non-JSON for chunk {idx+1}: {sc}. Body: {body}")

                for ch in data.get("channels", []) or []:
                    cid = str(ch.get("channelId"))
                    if cid not in channels_map:
                        channels_map[cid] = _normalize_channel(ch)
                    base = channels_map[cid]
                    for ev in ch.get("events", []) or []:
                        _merge_filter_tags_into_genres(ev)
                        base["events"].append(ev)
                break  # success

            # Retry on 429/5xx
            if sc == 429 or 500 <= sc < 600:
                if attempt <= max_retries:
                    sleep_s = min(60, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    _v(1, f"status {sc} — retrying in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                body = (r.text or "")[:600]
                print(f"[zap2xml] SKIP chunk {idx+1}/{len(offsets)} due to HTTP {sc} after {attempt-1}/{max_retries} retries", flush=True)
            break

            # Other client errors: show details and stop
            body = (r.text or "")[:600]
            raise RuntimeError(f"Zap2it error {sc} for chunk {idx+1}/{len(offsets)} "
                               f"(lineup={lineup_api}, headend={headend_api}); URL={url}; Body: {body}")

        if delay_seconds > 0 and idx < len(offsets) - 1:
            time.sleep(delay_seconds)

    channels = list(channels_map.values())
    channels.sort(key=lambda c: (str(c.get("callSign") or ""), str(c.get("channelNo") or "")))
    return channels

# ---------- XML helpers ----------

def _first(x):
    if isinstance(x, (list, tuple)) and x:
        return x[0]
    return x

def _zap_iso_to_dt(s):
    if not s:
        return None
    try:
        st = str(s)
        if st.endswith('Z'):
            return _dt.datetime.fromisoformat(st[:-1]).replace(tzinfo=_dt.timezone.utc)
        if re.fullmatch(r"\d{10}", st):
            return _dt.datetime.fromtimestamp(int(st), tz=_dt.timezone.utc)
        return _dt.datetime.fromisoformat(st.replace('Z',''))
    except Exception:
        return None

def _xmltv_time(dtobj):
    if not dtobj:
        return ""
    # Force +0000 for simplicity (Zap2it uses UTC epoch)
    if dtobj.tzinfo is None:
        dtobj = dtobj.replace(tzinfo=_dt.timezone.utc)
    return dtobj.strftime("%Y%m%d%H%M%S %z")

def _xmltv_date(dtobj):
    return dtobj.strftime("%Y%m%d") if dtobj else ""

def _xmltv_ns_from_date(dtobj):
    """
    Fallback encoding per request: YYYY-1.MMDD-1
    Example: 2025-09-12 -> 2024.0911.
    """
    if not dtobj:
        return None
    year_minus = dtobj.year - 1
    month_str = dtobj.strftime("%m")
    day_minus = dtobj.day - 1
    return f"{year_minus}.{month_str}{day_minus:02d}."

def _is_movie_or_sports(ev, program):
    genres = program.get("genres") or []
    genres = [g.lower() if isinstance(g, str) else str(g).lower() for g in genres]
    etype = (program.get("entityType") or program.get("type") or "").lower()
    return ("movie" in genres or etype == "movie" or
            "sports" in genres or etype == "sports")

def _ensure_asset_url(s: str) -> str:
    """Ensure full https://dshm.tmsimg.com/assets/<name>[.jpg] and strip query."""
    if not s:
        return s
    s0 = str(s).split("?", 1)[0]
    if s0.startswith("//"):
        s0 = "https:" + s0
    if not s0.startswith("http"):
        s0 = "https://dshm.tmsimg.com/assets/" + s0.lstrip("/")
    tail = s0.rsplit("/", 1)[-1]
    if "." not in tail:
        s0 += ".jpg"
    return s0

def _program_icon(program: Dict[str, Any], ev: Dict[str, Any]) -> Optional[str]:
    icon = None
    pref = program.get("preferredImage") or {}
    if isinstance(pref, dict):
        icon = pref.get("uri")
    
    icon = icon or program.get("image") or ev.get("thumbnail")
    
    if not icon:
        return None

    # Replace zap2it with dshm in the URL string
    icon_str = str(icon).replace("zap2it.tmsimg.com", "dshm.tmsimg.com")

    return _ensure_asset_url(icon_str)

# ---------- XML writer ----------

def write_xmltv(channels: List[Dict[str, Any]], out_path: Path) -> None:
    tv = ET.Element("tv")

    # Channels (sorted globally by name)
    def _chan_sort_key(ch):
        # Prefer callSign, then affiliate, then channelNo/id
        name = ch.get("callSign") or ch.get("affiliateName") or ch.get("channelNo") or ch.get("stationId") or ch.get("channelId") or ""
        return str(name).casefold()
    channels = sorted(channels, key=_chan_sort_key)
    for ch in channels:
        cid = str(ch.get("stationId") or ch.get("channelId") or "")
        ch_el = ET.SubElement(tv, "channel", {"id": cid})

        def dn(val):
            if val:
                ET.SubElement(ch_el, "display-name").text = str(val)

        call_sign = ch.get("callSign")
        affiliate = ch.get("affiliateName")
        dn(call_sign)
        dn(affiliate)
        if call_sign and affiliate:
            dn(f"{call_sign} {affiliate}")

        thumb = ch.get("thumbnail")
        if thumb:
            ET.SubElement(ch_el, "icon", {"src": _ensure_asset_url(str(thumb))})

    # Programmes
    for ch in channels:
        events = sorted(ch.get("events", []), key=lambda e: e.get("startTime") or "")
        for ev in events:
            program = ev.get("program") or {}
            start_dt = _zap_iso_to_dt(ev.get("startTime") or ev.get("start"))
            end_dt   = _zap_iso_to_dt(ev.get("endTime")   or ev.get("end"))

            prog_el = ET.SubElement(tv, "programme", {
                "start":  _xmltv_time(start_dt),
                "stop":   _xmltv_time(end_dt),
                "channel": str(ch.get("stationId") or ch.get("channelId") or ""),
            })

            title = _first(program.get("title")) or _first(ev.get("title"))
            if title:
                ET.SubElement(prog_el, "title").text = str(title)

            if program.get("episodeTitle"):
                ET.SubElement(prog_el, "sub-title").text = str(program["episodeTitle"])

            desc = (program.get("shortDesc") or program.get("longDescription") or
                    program.get("shortDescription") or ev.get("description"))
            if desc:
                ET.SubElement(prog_el, "desc").text = str(desc)

            # <date>
            if program.get("releaseYear"):
                ET.SubElement(prog_el, "date").text = str(program["releaseYear"])
            else:
                dtext = _xmltv_date(start_dt)
                if dtext:
                    ET.SubElement(prog_el, "date").text = dtext

            # <category>
            genres = program.get("genres") or []
            wrote_any_category = False
            for g in sorted(genres, key=lambda x: str(x)):
                name = g if isinstance(g, str) else (g.get("name") or str(g))
                if name:
                    wrote_any_category = True
                    ET.SubElement(prog_el, "category", {"lang": "en"}).text = str(name[0].upper() + name[1:])
            if not wrote_any_category and not _is_movie_or_sports(ev, program):
                ET.SubElement(prog_el, "category", {"lang": "en"}).text = "Series"

            # <length>
            dur = ev.get("duration") or program.get("duration")
            if dur:
                try:
                    dur = int(dur)
                except Exception:
                    pass
                ET.SubElement(prog_el, "length", {"units": "minutes"}).text = str(dur)

            # <icon>
            icon_url = _program_icon(program, ev)
            if icon_url:
                ET.SubElement(prog_el, "icon", {"src": icon_url})

            # URL + dd_progid + episode numbering
            tms_id = program.get("tmsId") or ev.get("tmsId")
            series_id = (
                program.get("seriesId")
                or program.get("rootId")
                or (tms_id[:-4] if tms_id and len(str(tms_id)) > 4 and str(tms_id)[-4:].isdigit() else None)
            )

            if series_id and tms_id:
                ET.SubElement(prog_el, "url").text = (
                    f"https://tvlistings.gracenote.com//overview.html?programSeriesId={series_id}&tmsId={tms_id}"
                )

            if series_id and tms_id and str(tms_id)[-4:].isdigit():
                dd_val = f"{series_id}.{str(tms_id)[-4:]}"
                ET.SubElement(prog_el, "episode-num", {"system": "dd_progid"}).text = dd_val
            elif tms_id:
                s = str(tms_id)
                if len(s) >= 6 and s[-4:].isdigit():
                    ET.SubElement(prog_el, "episode-num", {"system": "dd_progid"}).text = f"{s[:-4]}.{s[-4:]}"
                else:
                    ET.SubElement(prog_el, "episode-num", {"system": "dd_progid"}).text = s

            def _get_int(*keys):
                for k in keys:
                    v = program.get(k)
                    if v not in (None, ""):
                        try:
                            return int(v)
                        except Exception:
                            pass
                return None

            season_raw  = _get_int("season", "seasonNumber", "seasonNum", "seasonNo")
            episode_raw = _get_int("episode", "episodeNumber", "episodeNum", "epNum", "number")

            xmltv_ns_val = None
            onscreen_val = None
            common_val   = None

            if season_raw is not None or episode_raw is not None:
                if season_raw is not None:
                    s_ns = season_raw - 1
                else:
                    s_ns = (start_dt.year - 1) if start_dt else -1
                e_ns = (episode_raw - 1) if episode_raw is not None else -1
                xmltv_ns_val = f"{s_ns}.{e_ns}."
                if season_raw is not None and episode_raw is not None:
                    onscreen_val = f"S{season_raw:02}E{episode_raw:02}"
                    common_val   = f"S{season_raw:02}E{episode_raw:02}"
            else:
                xmltv_ns_val = _xmltv_ns_from_date(start_dt)

            if xmltv_ns_val:
                ET.SubElement(prog_el, "episode-num", {"system": "xmltv_ns"}).text = xmltv_ns_val
            if onscreen_val:
                ET.SubElement(prog_el, "episode-num", {"system": "onscreen"}).text = onscreen_val
            if common_val:
                ET.SubElement(prog_el, "episode-num", {"system": "common"}).text = common_val

            # flags: from event.flag (and program.new/live if present)
            flags_raw = (ev.get("flag") or ev.get("flags") or [])
            flags = {str(f).strip().lower() for f in flags_raw}
            is_live = ('live' in flags) or bool(program.get('live'))
            is_new = ('new' in flags) or any('premiere' in f for f in flags) or bool(program.get('new'))

            if is_live:
                ET.SubElement(prog_el, "live")
            if is_new:
                ET.SubElement(prog_el, "new")

            if not is_new and not is_live:
                ps = ET.SubElement(prog_el, "previously-shown")
                airDate = program.get("originalAirDate") or program.get("airDate")
                if airDate:
                    try:
                        d = _zap_iso_to_dt(airDate) or _zap_iso_to_dt(str(airDate) + "T00:00:00Z")
                        if d:
                            ps.set("start", d.strftime("%Y%m%d") + "000000")
                    except Exception:
                        pass

            ET.SubElement(prog_el, "audio", {"type": "stereo"})
            ET.SubElement(prog_el, "subtitles", {"type": "teletext"})

            ratings = program.get("ratings") or ev.get("ratings") or []
            if isinstance(ratings, list) and ratings:
                r0 = ratings[0]
                code = r0.get("code") or r0.get("rating")
                sysname = r0.get("system") or "MPAA"
                if code:
                    r_el = ET.SubElement(prog_el, "rating", {"system": str(sysname)})
                    ET.SubElement(r_el, "value").text = str(code)
            elif program.get("rating"):
                r_el = ET.SubElement(prog_el, "rating", {"system": "MPAA"})
                ET.SubElement(r_el, "value").text = str(program["rating"])

    tree = ET.ElementTree(tv)
    try:
        ET.indent(tree, space="  ")
    except Exception:
        pass
    out_path = str(out_path)

    # v2.1.0: normalize first display-name via DB callSign lookup

    try:

        _rewrite_first_display_name_with_station_name(tv)

    except Exception:

        pass

    tree.write(out_path, encoding="utf-8", xml_declaration=True)
# ---------- CLI ----------

def main(argv=None):
    global VERBOSE
    p = argparse.ArgumentParser(prog="zap2xml.py", description="Zap2It → XMLTV (Gracenote grid)")
    p.add_argument("--lineupId", dest="lineupId", default="", help="Full lineup ID (e.g., USA-DITV501-X or USA-OTA12345)")
    p.add_argument("--lineup-id", dest="lineupId_dash", default="", help=argparse.SUPPRESS)
    p.add_argument("-c", "--country", dest="country", required=True, help="Country (e.g., USA, CAN)")
    p.add_argument("-z", "--zip", dest="postal", default="", help="Postal/ZIP code (required for OTA)")
    p.add_argument("--timespan", dest="timespan", type=int, default=72, help="Total hours to fetch")
    p.add_argument("-d", "--delay", dest="delay", type=int, default=0, help="Delay in seconds between requests")
    p.add_argument("--output", dest="output", required=True, help="Output XMLTV file path")
    p.add_argument("-v", "--verbose", dest="verbose", type=int, default=0, choices=[0,1,2], help="Verbosity (0-2)")
    args = p.parse_args(argv)


    # Build lineup_ids from args (accept ',' or '.')
    import re as _re
    raw_lineup = args.lineupId
    if isinstance(raw_lineup, str) and '.' in raw_lineup and ',' not in raw_lineup:
        print("[zap2xml] NOTE: '.' detected in lineup list; treating it as ','", flush=True)
    _parts = _re.split(r'[.,]+', raw_lineup) if isinstance(raw_lineup, str) else [raw_lineup]
    lineup_ids = [s for s in _parts if s]
    VERBOSE = int(args.verbose or 0)

    lineup_input = args.lineupId or args.lineupId_dash
    if not lineup_input:
        p.error("Please provide --lineupId (e.g., USA-DITV501-X or USA-OTA12345).")

    country = COUNTRY_3.get(args.country.upper(), args.country.upper())

    if _is_ota(lineup_input) and not args.postal:
        p.error("Postal/ZIP code is required for OTA/LocalBroadcast lineups (e.g., USA-OTA63601).")

    _v(1, f"lineup={lineup_input} country={country} postal={'(provided)' if args.postal else '-'} timespan={args.timespan} delay={args.delay}")


    all_channels = []


    for _i, _lid in enumerate(lineup_ids, 1):


        print(f"[zap2xml] === Lineup {_i}/{len(lineup_ids)}: {_lid} ===", flush=True)


        channels = fetch_grid(country=country, lineup_id_input=_lid, postal=args.postal or None, timespan=args.timespan, delay_seconds=args.delay, max_retries=3)


        try:


            all_channels.extend(channels)


        except Exception:


            pass


    channels = all_channels

    try:

        def _nm(ch):

            name = ch.get('name') or ch.get('callSign') or (ch.get('station') or {}).get('callSign')

            if isinstance(name, list): name = name[0]

            num = ch.get('number') or ch.get('channel') or ch.get('channelNumber') or ''

            return ((name or '').casefold(), str(num))

        channels.sort(key=_nm)

        print(f"[zap2xml] Sorted merged channels by name (total={len(channels)})", flush=True)

    except Exception as _e:

        print(f"[zap2xml] channel sort skipped: {_e}", flush=True)


    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xmltv(channels, out_path)
    _v(1, f"Wrote XMLTV → {out_path}")

if __name__ == "__main__":
    with _single_instance_guard_v210():
        main()
