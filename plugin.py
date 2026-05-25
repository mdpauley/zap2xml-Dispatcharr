# plugin.py
"""
Zap2XML plugin: Generates an XMLTV file from Zap2it for one or more lineups and
automatically adds/updates an EPG Source in Dispatcharr. Runs bundled zap2xml.py.

v1.9.14:
- Global default User-Agent added (Chrome/124 on Windows 10) used when the UI field is blank.
- INPUT SIMPLIFIED: Only lineupId strings are entered (one or many).
- Per-lineup detection of country (-c) from prefix (e.g., USA-DITV501-X -> -c USA).
- Per-lineup OTA postal extraction:
    * USA-OTA12345  -> -c USA -z 12345
    * CAN-OTAK1P5W1 -> -c CAN -z K1P5W1
    * USA-LOCALBROADCAST-63601 -> -c USA -z 63601
- zap2xml.py receives --lineupId and (if derived) -z; it handles OTA API normalization & device rules.
- "Apply Channel Mappings" action (maps channels to EPGData, sets logos).
- Streams verbose, unmasked child logs: -v 2 + ZAP2XML_MASK_POSTAL=0 → container logs.
"""

import os
import sys
import subprocess
import shutil
import threading
import time
import re
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, Dict

# ---- Global default UA (used if UI field empty and no env/CoreSettings UA) ----
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class Plugin:
    name = "Zap2XML"
    version = "2.2.0"
    description = "Create an XMLTV EPG from Zap2it (single or multiple lineups)."

    # Settings rendered by UI
    fields = [
        {
            "id": "lineup_ids",
            "label": "Lineup IDs",
            "type": "text",  # multiline
            "default": "",
            "placeholder": "",
            "help_text": "Only enter lineupId strings. Country and ZIP/postal are auto-derived per lineup.",
        },
        {
            "id": "merge_lineups",
            "label": "Merge multiple lineups into a single XMLTV",
            "type": "boolean",
            "default": True,
            "help_text": "If enabled, all generated lineups are merged into one XML and one EPG Source.",
        },
        {
            "id": "timespan",
            "label": "Total hours to fetch",
            "type": "number",
            "default": 72,
            "help_text": "Total hours of guide data to fetch (requested in 6-hour chunks).",
        },
        {
            "id": "delay",
            "label": "Delay between requests (sec)",
            "type": "number",
            "default": 5,
            "help_text": "Seconds to sleep between chunk requests to Zap2it.",
        },
        {
            "id": "user_agent",
            "label": "HTTP User-Agent (optional)",
            "type": "string",
            "help_text": "Override HTTP User-Agent when fetching from Zap2it. Leave blank to use the plugin's global default.",
        },
        {
            "id": "refresh_interval_hours",
            "label": "Refresh Interval (hours)",
            "type": "number",
            "default": 24,
            "help_text": "Auto-refresh interval for the EPG source (0 to disable).",
        },
        {
            "id": "auto_download_enabled",
            "label": "Auto-Download XML on Schedule",
            "type": "boolean",
            "default": True,
            "help_text": "When enabled, download a new XMLTV file on the saved interval.",
        },
        {
            "id": "epg_name",
            "label": "EPG Name",
            "type": "string",
            "default": "Zap2XML",
            "help_text": "Base name for the EPG Source(s) added to Dispatcharr.",
        },
        {
            "id": "confirm",
            "label": "Require confirmation",
            "type": "boolean",
            "default": True,
            "help_text": "Confirm before generating and importing the EPG",
        },
    ]

    actions = [
        {
            "id": "download_epg",
            "label": "Download EPG XML",
            "description": "Fetch Zap2it data and write XML under /data/epgs. Updates/creates the EPG source(s).",
            "confirm": {
                "required": True,
                "title": "Download EPG XML?",
                "message": "This fetches a new XMLTV file using the listed lineup IDs and updates the EPG source(s).",
            },
        },
        {
            "id": "apply_channel_mappings",
            "label": "Set Channels + Logos",
            "description": "Match channels by name to EPG tvg_ids and set logos.",
            "confirm": {
                "required": True,
                "title": "Set channels + logos?",
                "message": "Matches by channel name. Sets EPG and logos from channels.db or XMLTV icons.",
            },
        },
    ]

    def __init__(self):
        try:
            _ensure_scheduler_started()
        except Exception:
            pass

    def run(self, action: str, params: dict, context: dict):
        if action not in ("download_epg", "apply_channel_mappings"):
            return {"status": "error", "message": f"Unknown action: {action}"}

        settings = context.get("settings", {})
        logger = context.get("logger")

        lineup_blob = (settings.get("lineup_ids") or "").strip()
        merge_lineups = bool(settings.get("merge_lineups", True))
        timespan = int(settings.get("timespan") or 72)
        delay = int(settings.get("delay") or 5)

        refresh_hours_raw = settings.get("refresh_interval_hours")
        try:
            refresh_hours = int(refresh_hours_raw if refresh_hours_raw is not None else 24)
        except Exception:
            refresh_hours = 24
        epg_name_base = (settings.get("epg_name") or "Zap2XML").strip()

        all_lineups = self._split_lineups(lineup_blob)

        # ---- Resolve User-Agent with precedence: UI setting → env → CoreSettings → DEFAULT_UA
        user_agent = (settings.get("user_agent") or "").strip()
        if not user_agent:
            user_agent = (
                os.environ.get("USER_AGENT")
                or os.environ.get("ZAP2XML_USER_AGENT")
                or ""
            )

        if not user_agent:
            # Optional CoreSettings fallback
            try:
                from core.models import UserAgent, CoreSettings
                default_user_agent_setting = CoreSettings.objects.filter(key="default-user-agent").first()
                if default_user_agent_setting and default_user_agent_setting.value:
                    ua_obj = UserAgent.objects.filter(id=int(default_user_agent_setting.value)).first()
                    if ua_obj and ua_obj.user_agent:
                        user_agent = ua_obj.user_agent
            except Exception:
                pass

        if not user_agent:
            user_agent = DEFAULT_UA
        # ---------------------------------------------------------------------

        base_data_dir = os.environ.get("DISPATCHARR_DATA_DIR", "/data")
        out_dir = os.path.join(base_data_dir, "epgs")
        os.makedirs(out_dir, exist_ok=True)

        if action == "download_epg":
            if not all_lineups:
                return {"status": "error", "message": "Please provide at least one lineupId."}

            produced_files: List[Tuple[str, str]] = []  # (lineup, path)
            cmds: List[str] = []

            # Run each lineup with derived -c / optional -z
            for lu in all_lineups:
                info = self._derive_meta(lu)
                if not info["country"]:
                    return {"status": "error", "message": f"Unable to detect country for lineup '{lu}' (expected prefix like USA-, CAN-, etc.)"}

                # OTA must have postal; if missing, error with a hint
                if info["is_ota"] and not info["postal"]:
                    return {"status": "error", "message": f"OTA lineup '{lu}' is missing embedded postal/ZIP (e.g., USA-OTA12345 or CAN-OTAK1P5W1)."}

                ok, produced_path, msg, cmd = self._run_zap2it_script(
                    country=info["country"], postal=info["postal"], lineup_id=lu,
                    timespan=timespan, delay=delay, logger=logger, user_agent=user_agent
                )
                cmds.append(cmd)
                if not ok or not produced_path or not os.path.exists(produced_path):
                    try: self._cleanup_plugin_workspace(logger)
                    except Exception: pass
                    return {"status": "error", "message": f"zap2xml failed for lineup '{lu}': {msg}", "commands": cmds}
                produced_files.append((lu, produced_path))

            responses = []
            if merge_lineups and len(produced_files) > 1:
                merged_name = "zap2xml_merged.xml"
                merged_path = os.path.join(out_dir, merged_name)
                try:
                    self._merge_xmltv([p for _, p in produced_files], merged_path)
                except Exception as e:
                    return {"status": "error", "message": f"Failed to merge XMLTV files: {e}", "commands": cmds}
                resp = self._create_or_update_epg_source(
                    epg_name=f"{epg_name_base} (merged)",
                    xml_path=merged_path,
                    refresh_hours=refresh_hours,
                    logger=logger,
                )
                if resp.get("status") != "ok":
                    return {**resp, "commands": cmds}
                responses.append(resp)
            else:
                for lu, src in produced_files:
                    meta = self._derive_meta(lu)
                    safe_lu = self._sanitize(lu)
                    out_name = f"zap2xml_{meta['country']}_{safe_lu}.xml"
                    out_path = os.path.join(out_dir, out_name)
                    try:
                        shutil.copyfile(src, out_path)
                    except Exception as e:
                        return {"status": "error", "message": f"Unable to write EPG file: {e}", "commands": cmds}
                    resp = self._create_or_update_epg_source(
                        epg_name=f"{epg_name_base} ({lu})",
                        xml_path=out_path,
                        refresh_hours=refresh_hours,
                        logger=logger,
                    )
                    if resp.get("status") != "ok":
                        return {**resp, "commands": cmds}
                    responses.append(resp)

            try:
                self._cleanup_plugin_workspace(logger)
            except Exception:
                pass

            summary_names = [r.get("epg_name") for r in responses if r.get("epg_name")]
            return {
                "status": "queued",
                "message": f"EPG source(s) updated: {', '.join(summary_names)}",
                "commands": cmds,
                "files": [r.get("file") for r in responses if r.get("file")],
            }

        # Apply channel mappings
        return self._apply_channel_mappings(epg_name_base, context)

    # ---------- helpers ----------

    @staticmethod
    def _split_lineups(blob: str) -> List[str]:
        if not blob:
            return []
        blob = blob.replace(",", " ").replace("\r", " ").replace("\n", " ")
        toks = [t.strip() for t in blob.split(" ") if t.strip()]
        out, seen = [], set()
        for t in toks:
            if t not in seen:
                out.append(t); seen.add(t)
        return out

    @staticmethod
    def _sanitize(s: str) -> str:
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (s or ""))

    @staticmethod
    def _derive_meta(lineup: str) -> Dict[str, Optional[str]]:
        """
        Returns: {"country": "USA"/"CAN"/..., "postal": "K1P5W1"/"63601"/"", "is_ota": bool}
        Rules:
          - country: first 3 letters before first '-' (uppercase)
          - OTA: if 'OTA' in lineup OR 'LOCALBROADCAST' in lineup
             * try '...-OTA<POSTAL>' form → capture after 'OTA'
             * else try '...-LOCALBROADCAST-<POSTAL>' form
             * if none found -> postal=""
        """
        s = (lineup or "").strip()
        m = re.match(r"^([A-Za-z]{3})-", s)
        country = m.group(1).upper() if m else None
        u = s.upper()

        postal = ""
        is_ota = ("-OTA" in u) or ("LOCALBROADCAST" in u)

        # Try USA-OTA12345 / CAN-OTAK1P5W1
        m2 = re.search(r"-OTA([A-Za-z0-9\-]+)", s, flags=re.I)
        if m2:
            postal = m2.group(1).strip()

        # Try USA-LOCALBROADCAST-63601
        if not postal:
            m3 = re.search(r"-LOCALBROADCAST-([A-Za-z0-9\-]+)", s, flags=re.I)
            if m3:
                postal = m3.group(1).strip()

        return {"country": country, "postal": postal, "is_ota": is_ota}

    def _resolve_python(self):
        candidates = []
        ve = os.environ.get("VIRTUAL_ENV")
        if ve:
            candidates.extend([os.path.join(ve, "bin", "python"), os.path.join(ve, "bin", "python3")])
        candidates.extend(["python", "python3", sys.executable])
        for c in candidates:
            if not c:
                continue
            ok = os.path.exists(c) if os.path.sep in c else (shutil.which(c) is not None)
            if not ok:
                continue
            if "uwsgi" in os.path.basename(c).lower():
                continue
            return c
        return sys.executable

    def _run_zap2it_script(
        self,
        country: str,
        postal: str,
        lineup_id: str,
        timespan: int,
        delay: int,
        logger,
        user_agent: str = "",
    ):
        """
        Run zap2xml.py with verbose, unmasked logging; stream output to container logs.
        Returns (ok, produced_path, message, cmd_str).
        """
        import subprocess, sys, os
        try:
            plugin_dir = os.path.dirname(__file__)
            script_path = os.path.join(plugin_dir, "zap2xml.py")
            if not os.path.exists(script_path):
                return False, None, "zap2xml.py not found in plugin directory", ""

            produced_path = os.path.join(plugin_dir, f"xmltv_{self._sanitize(lineup_id)}.xml")
            try:
                if os.path.exists(produced_path):
                    os.remove(produced_path)
            except Exception:
                pass

            python_bin = self._resolve_python()
            cmd = [
                python_bin,
                script_path,
                "--lineupId", lineup_id,
                "-c", country,
                "--timespan", str(timespan),
                "-d", str(delay),
                "--output", produced_path,
                "-v", "2",  # max verbosity in logs
            ]
            if postal:
                # insert '-z postal' after '-c country'
                cmd[6:6] = ["-z", postal]

            env = {k:v for k,v in os.environ.items() if k not in ('USER_AGENT','HTTP_USER_AGENT','ZAP2XML_USER_AGENT')}
            # Make absolutely sure the zap2xml logger never masks postal in URLs.
            env["ZAP2XML_MASK_POSTAL"] = "0"
            if user_agent:
                env["USER_AGENT"] = user_agent
                env["ZAP2XML_USER_AGENT"] = user_agent

            cmd_str = " ".join(cmd)
            if logger:
                try:
                    logger.info("[zap2xml] Launching: %s (cwd=%s)", cmd_str, plugin_dir)
                except Exception:
                    pass
            else:
                print(f"[zap2xml] Launching: {cmd_str} (cwd={plugin_dir})", file=sys.stderr, flush=True)

            # Stream live output
            proc = subprocess.Popen(
                cmd,
                cwd=plugin_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line buffered
            )
            try:
                for line in proc.stdout:
                    line = (line or "").rstrip("\n")
                    if logger:
                        try:
                            logger.info("[zap2xml] %s", line)
                        except Exception:
                            pass
                    else:
                        print(f"[zap2xml] {line}", file=sys.stderr, flush=True)
            finally:
                ret = proc.wait()

            if ret != 0:
                return False, None, f"zap2xml.py exited with code {ret}", cmd_str

            if not os.path.exists(produced_path):
                return False, None, "zap2xml.py finished but no XML produced", cmd_str

            if logger:
                try:
                    logger.info("[zap2xml] Output ready: %s", produced_path)
                except Exception:
                    pass

            return True, produced_path, "ok", cmd_str

        except Exception as e:
            return False, None, f"zap2xml execution error: {e}", " ".join(cmd) if "cmd" in locals() else ""

    
    def _merge_xmltv(self, paths: List[str], out_path: str):
            """Merge multiple XMLTVs into one, deduplicating channels by id and ordering:
               - channels first, sorted by display name (callSign → affiliate → id)
               - programmes next, for each channel in the same channel order, sorted by start time.
            """
            if not paths:
                raise ValueError("No XML paths to merge")

            # Collect channels and programmes across all files
            chan_map = {}   # id -> channel Element (first occurrence wins)
            prog_map = {}   # id -> list[programme Elements]

            def _chan_name(ch):
                # derive a sort key similar to write_xmltv()
                names = [None, None]
                # try to read display-names if present (call sign then affiliate)
                dns = [dn.text or "" for dn in ch.findall("./display-name") if dn is not None and dn.text]
                # Heuristics: first display-name tends to be callSign; second, affiliate.
                if dns:
                    names[0] = dns[0]
                    if len(dns) > 1:
                        names[1] = dns[1]
                # fallback to id
                base = names[0] or names[1] or ch.get("id") or ""
                return str(base).casefold()

            import xml.etree.ElementTree as ET
            tv_root = ET.Element("tv")

            for p in paths:
                tree = ET.parse(p)
                root = tree.getroot()
                if root.tag != "tv":
                    continue
                # channels
                for ch in root.findall("./channel"):
                    cid = ch.get("id")
                    if not cid:
                        continue
                    if cid not in chan_map:
                        chan_map[cid] = ch
                    # ensure list exists for programmes
                    prog_map.setdefault(cid, [])
                # programmes
                for pr in root.findall("./programme"):
                    cid = pr.get("channel")
                    if not cid:
                        continue
                    prog_map.setdefault(cid, []).append(pr)

            # Sort channels by derived name
            chan_items = sorted(chan_map.items(), key=lambda kv: _chan_name(kv[1]))

            # Append channels first
            for cid, ch in chan_items:
                tv_root.append(ch)

            # Append programmes per channel in same order, time-sorted
            def _pr_start(p):
                return p.get("start") or ""
            for cid, _ in chan_items:
                progs = prog_map.get(cid, [])
                progs.sort(key=_pr_start)
                for pr in progs:
                    tv_root.append(pr)

            tree = ET.ElementTree(tv_root)
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(out_path, encoding="utf-8", xml_declaration=True)
    def _create_or_update_epg_source(self, epg_name: str, xml_path: str, refresh_hours: int, logger=None):
        """Create/update EPGSource and queue parse."""
        try:
            from apps.epg.models import EPGSource
            from core.utils import send_websocket_update
            from apps.epg.tasks import refresh_epg_data

            source, created = EPGSource.objects.get_or_create(
                name=epg_name,
                defaults={"source_type": "xmltv", "file_path": xml_path, "is_active": True},
            )
            updates = []
            if not created and source.file_path != xml_path:
                source.file_path = xml_path
                updates.append("file_path")
            if refresh_hours is not None and refresh_hours >= 0 and source.refresh_interval != refresh_hours:
                source.refresh_interval = refresh_hours
                updates.append("refresh_interval")
            if updates:
                source.save(update_fields=updates)

            try:
                refresh_epg_data.delay(source.id)
            except Exception:
                try:
                    refresh_epg_data(source.id)
                except Exception:
                    pass

            try:
                send_websocket_update(
                    "updates", "update",
                    {
                        "type": "epg_sources_changed",
                        "action": "created" if created else "updated",
                        "source_id": source.id,
                        "name": source.name,
                    },
                )
            except Exception:
                pass

            # record last_generated so scheduler can trigger correctly
            try:
                from apps.plugins.models import PluginConfig
                plugin_key = __name__.split(".")[0]
                cfg = PluginConfig.objects.filter(key=plugin_key).first()
                if cfg:
                    st = dict(cfg.settings or {})
                    st["last_generated"] = datetime.utcnow().isoformat() + "Z"
                    cfg.settings = st
                    cfg.save(update_fields=["settings", "updated_at"])
            except Exception:
                pass

            return {"status": "ok", "epg_name": epg_name, "file": xml_path}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create/update EPG source '{epg_name}': {e}"}

    def _apply_channel_mappings(self, epg_name_base, context):
        """
        Map app Channels to XMLTV stations and set logos.

        Strategy:
        - Find EPGSource(s) created by this plugin (name contains epg_name_base).
        - Parse their XML files to build a lookup:
            normalized_name -> {"id": stationId, "names": [...], "icon": url}
        - For each Channel in DB:
            * normalize channel.name and try to match
            * set channel.tvc_guide_stationid (if field exists)
            * ensure EPGData(tvg_id, epg_source) exists and assign to channel.epg_data (if field exists)
            * logo: prefer channels.db, else XML icon; create/attach Logo
        Returns a summary dict.
        """
        import sqlite3
        from pathlib import Path

        logger = context.get("logger")
        try:
            from apps.channels.models import Channel, Logo
            from apps.epg.models import EPGSource, EPGData
        except Exception as e:
            return {"status": "error", "message": f"Cannot import app models: {e}"}

        def _norm(s: str) -> str:
            import re as _re
            s = (s or "").lower()
            s = _re.sub(r"\[.*?\]", "", s)
            s = _re.sub(r"\(.*?\)", "", s)
            s = _re.sub(r"[^\w\s]", "", s)
            s = _re.sub(r"\s+", " ", s).strip()
            return s

        def _load_xml_channels(xml_path: str):
            m = {}
            if not xml_path or not Path(xml_path).exists():
                return m
            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()
                for ch_el in root.findall("./channel"):
                    sid = ch_el.get("id") or ""
                    names = [(dn.text or "").strip() for dn in ch_el.findall("./display-name") if (dn.text or "").strip()]
                    icon_el = ch_el.find("./icon")
                    icon = icon_el.get("src") if icon_el is not None else ""
                    for nm in names:
                        m[_norm(nm)] = {"id": sid, "names": names, "icon": icon}
                return m
            except Exception:
                if logger:
                    try: logger.exception("Failed parsing XMLTV for mapping: %s", xml_path)
                    except Exception: pass
                return {}

        def _find_sources():
            try:
                qs = EPGSource.objects.all()
                pri = list(qs.filter(name__icontains=epg_name_base).order_by("name"))
                rest = [s for s in qs if s not in set(pri)]
                return pri + rest
            except Exception:
                return []

        def _logo_from_channels_db(chan_name: str, callsign: str) -> str:
            db_paths = [
                "/data/channels.db",
                str(Path(__file__).with_name("channels.db")),
            ]
            for dbp in db_paths:
                con = None
                try:
                    p = Path(dbp)
                    if not p.exists():
                        continue
                    con = sqlite3.connect(dbp)
                    cur = con.cursor()
                    probes = [
                        ("channels", ["name","channel_name","display_name","callsign","call_sign"], ["logo_url","logo","icon","image_url"]),
                        ("stations", ["name","callsign","call_sign"], ["logo_url","logo","icon","image_url"]),
                        ("channel_logos", ["name","callsign","call_sign"], ["logo_url","logo","icon","image_url"]),
                    ]
                    # exact
                    for table, name_cols, url_cols in probes:
                        try:
                            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
                        except Exception:
                            continue
                        cur.execute(f"PRAGMA table_info({table})")
                        cols = {r[1].lower() for r in cur.fetchall()}
                        name_cols = [c for c in name_cols if c in cols]
                        url_cols  = [c for c in url_cols if c in cols]
                        if not name_cols or not url_cols:
                            continue
                        for ucol in url_cols:
                            for ncol in name_cols:
                                for val in [callsign, chan_name]:
                                    if not val:
                                        continue
                                    try:
                                        cur.execute(f"SELECT {ucol} FROM {table} WHERE lower({ncol})=lower(?) LIMIT 1", (val,))
                                        row = cur.fetchone()
                                        if row and row[0]:
                                            return str(row[0])
                                    except Exception:
                                        continue
                    # like
                    for table, name_cols, url_cols in probes:
                        try:
                            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
                        except Exception:
                            continue
                        cur.execute(f"PRAGMA table_info({table})")
                        cols = {r[1].lower() for r in cur.fetchall()}
                        name_cols = [c for c in name_cols if c in cols]
                        url_cols  = [c for c in url_cols if c in cols]
                        if not name_cols or not url_cols:
                            continue
                        for ucol in url_cols:
                            for ncol in name_cols:
                                for val in [callsign, chan_name]:
                                    if not val:
                                        continue
                                    try:
                                        cur.execute(f"SELECT {ucol} FROM {table} WHERE lower({ncol}) LIKE lower(?) LIMIT 1", (f"%{val}%",))
                                        row = cur.fetchone()
                                        if row and row[0]:
                                            return str(row[0])
                                    except Exception:
                                        continue
                except Exception:
                    pass
                finally:
                    try:
                        if con:
                            con.close()
                    except Exception:
                        pass
            return ""

        sources = _find_sources()
        if not sources:
            return {"status": "error", "message": "No EPG sources found. Generate EPG first."}

        # Load mappings from all sources (later sources won't override earlier matches)
        name_to_station = {}
        source_for_station = {}
        icon_for_station = {}
        for s in sources:
            xmlp = getattr(s, "file_path", None)
            m = _load_xml_channels(xmlp)
            for k, v in m.items():
                if k not in name_to_station:
                    name_to_station[k] = v
                    source_for_station[v["id"]] = s
                    if v.get("icon"):
                        icon_for_station[v["id"]] = v["icon"]

        # Iterate channels
        try:
            from apps.channels.models import Channel  # re-import to avoid stale model issues
            channels = list(Channel.objects.all())
        except Exception as e:
            return {"status": "error", "message": f"Cannot query channels: {e}"}

        updated = 0
        created_epgs = 0
        logo_set = 0
        associations = []

        for ch in channels:
            try:
                raw_name = getattr(ch, "name", "") or getattr(ch, "channel_name", "")
                callsign = getattr(ch, "callsign", "") or getattr(ch, "call_sign", "")
                nm = _norm(raw_name or callsign)
                if not nm:
                    continue
                match = name_to_station.get(nm)
                if not match:
                    match = name_to_station.get(_norm(callsign))
                if not match:
                    continue

                station_id = match["id"]
                src = source_for_station.get(station_id)

                # ensure EPGData exists
                epg_data_obj = None
                try:
                    epg_data_obj, created = EPGData.objects.get_or_create(tvg_id=str(station_id), epg_source=src)
                    if created:
                        created_epgs += 1
                except Exception:
                    pass

                changed = False

                # set Channel.tvc_guide_stationid if present
                if hasattr(ch, "tvc_guide_stationid"):
                    if getattr(ch, "tvc_guide_stationid") != str(station_id):
                        setattr(ch, "tvc_guide_stationid", str(station_id))
                        changed = True

                # attach epg_data field if present
                if epg_data_obj is not None and hasattr(ch, "epg_data"):
                    if getattr(ch, "epg_data") != epg_data_obj:
                        setattr(ch, "epg_data", epg_data_obj)
                        changed = True

                # logo: prefer channels.db; fallback to XML icon
                logo_url = _logo_from_channels_db(raw_name, callsign)
                if not logo_url:
                    logo_url = icon_for_station.get(station_id, "")

                if logo_url and hasattr(ch, "logo"):
                    try:
                        from apps.channels.models import Logo  # ensure correct model
                        logo = None
                        try:
                            logo = Logo.objects.filter(url=logo_url).first()
                        except Exception:
                            logo = None
                        if not logo:
                            logo = Logo(url=logo_url)
                            logo.save()
                        if getattr(ch, "logo", None) != logo:
                            ch.logo = logo
                            changed = True
                            logo_set += 1
                    except Exception:
                        pass

                if changed:
                    try:
                        ch.save()
                        updated += 1
                        associations.append({"channel": raw_name or callsign, "station_id": station_id, "source": getattr(src, "name", "")})
                    except Exception:
                        pass
            except Exception:
                if logger:
                    try: logger.exception("Failed mapping channel id=%s", getattr(ch, "id", None))
                    except Exception: pass
                continue

        # optional notify UI
        try:
            from core.utils import send_websocket_update
            send_websocket_update(
                "updates","update",
                {
                    "type": "channels_mapped",
                    "updated": updated,
                    "created_epg": created_epgs,
                    "logos": logo_set,
                    "associations": associations,
                },
            )
        except Exception:
            pass

        return {
            "status": "ok",
            "message": f"Mapped {updated} channel(s), created {created_epgs} EPG entries, set {logo_set} logo(s)",
            "matched": updated,
            "logos": logo_set,
            "created_epg": created_epgs,
        }

    def _cleanup_plugin_workspace(self, logger=None):
        """Remove transient artifacts from the plugin folder."""
        try:
            plugin_dir = os.path.dirname(__file__)
            for name in os.listdir(plugin_dir):
                if name.startswith("xmltv_") and name.endswith(".xml"):
                    try:
                        os.remove(os.path.join(plugin_dir, name))
                    except Exception:
                        pass
            cache_dir = os.path.join(plugin_dir, "cache")
            if os.path.isdir(cache_dir):
                for root, dirs, files in os.walk(cache_dir, topdown=False):
                    for n in files:
                        try: os.remove(os.path.join(root, n))
                        except Exception: pass
                    for n in dirs:
                        try: os.rmdir(os.path.join(root, n))
                        except Exception: pass
                try: os.rmdir(cache_dir)
                except Exception: pass
            if logger:
                try: logger.info("[zap2xml] workspace cleaned")
                except Exception: pass
        except Exception:
            pass


# Lightweight background scheduler (unchanged)
_SCHEDULER_THREAD = None
_SCHEDULER_LOCK = threading.Lock()

def _ensure_scheduler_started():
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        t = threading.Thread(target=_scheduler_loop, name="zap2xml_auto_dl", daemon=True)
        t.start()
        _SCHEDULER_THREAD = t

def _scheduler_loop():
    import logging
    logger = logging.getLogger(__name__)
    while True:
        try:
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.filter(key=_plugin_key()).first()
            if not cfg or not cfg.enabled:
                time.sleep(60); continue
            st = cfg.settings or {}
            auto_enabled = bool(st.get("auto_download_enabled", True))
            interval_h = st.get("refresh_interval_hours", 24) or 24
            try: interval_h = int(interval_h)
            except Exception: interval_h = 24
            if not auto_enabled or interval_h <= 0:
                time.sleep(60); continue
            last_iso = st.get("last_generated")
            should_run = False
            if not last_iso:
                should_run = True
            else:
                try:
                    iso = last_iso
                    if iso.endswith("Z"):
                        last_dt = datetime.fromisoformat(iso[:-1]).replace(tzinfo=timezone.utc)
                    else:
                        last_dt = datetime.fromisoformat(iso)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - last_dt >= timedelta(hours=interval_h):
                        should_run = True
                except Exception:
                    should_run = True
            if should_run:
                logger.info("[zap2xml] Auto-download tick… running download_epg")
                try:
                    p = Plugin()
                    context = {"settings": st, "logger": logger}
                    p.run("download_epg", {}, context)
                except Exception as e:
                    logger.warning(f"[zap2xml] Auto-download failed: {e}")
            time.sleep(60)
        except Exception:
            try:
                logger.exception("[zap2xml] Scheduler loop error")
            except Exception:
                pass
            time.sleep(60)

def _plugin_key():
    return 'zap2xml'

fields = Plugin.fields
actions = Plugin.actions
