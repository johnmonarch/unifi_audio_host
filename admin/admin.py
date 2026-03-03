#!/usr/bin/env python3
import base64
import html
import json
import os
import re
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple


def clean_env_value(value: Any) -> str:
    text = str(value or "").strip()
    if text == "$":
        return ""
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text


def env_optional(name: str, default: str = "") -> str:
    return clean_env_value(os.getenv(name, default))


CONFIG_FILE = env_optional("CONFIG_FILE", "/config/alerts.json")
RUNTIME_CONFIG_FILE = env_optional("RUNTIME_CONFIG_FILE", "/config/runtime.json")
AUDIO_DIR = env_optional("AUDIO_DIR", "/audio")
WATCHER_NAMES = [name.strip() for name in env_optional("WATCHER_NAMES", "zone1,zone2").split(",") if name.strip()]
if not WATCHER_NAMES:
    WATCHER_NAMES = ["zone1", "zone2"]
ALERT_URL_BASE = env_optional("ALERT_URL_BASE", "").rstrip("/")
ADMIN_USERNAME = env_optional("ADMIN_USERNAME", "")
ADMIN_PASSWORD = env_optional("ADMIN_PASSWORD", "")
FILE_MANAGER_URL = env_optional("FILE_MANAGER_URL", "http://localhost:8126")
HOST = env_optional("ADMIN_BIND_HOST", "0.0.0.0") or "0.0.0.0"
PORT = int(os.getenv("ADMIN_BIND_PORT", "8127"))
HA_API_TIMEOUT_SEC = 7


def parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float_optional(value: Any, default):
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def hhmm_valid(value: str) -> bool:
    text = value.strip()
    if len(text) != 4 or not text.isdigit():
        return False
    hour = int(text[:2])
    minute = int(text[2:])
    return 0 <= hour <= 23 and 0 <= minute <= 59


def zone_name_valid(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value.strip().lower()) is not None


def normalize_ha_base_url(value: str) -> str:
    raw = value.strip()
    if raw == "":
        raise ValueError("Home Assistant URL/IP is required")
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"http://{raw}"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Home Assistant URL must use http or https")
    if not parsed.netloc:
        raise ValueError("Home Assistant URL/IP is invalid")

    host = parsed.hostname
    if not host:
        raise ValueError("Home Assistant URL/IP is invalid")

    netloc = parsed.netloc
    if parsed.port is None and ":" not in netloc:
        netloc = f"{host}:8123"
    return urllib.parse.urlunparse((parsed.scheme, netloc, "", "", "", "")).rstrip("/")


def write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    directory = os.path.dirname(path)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=directory) as temp:
        json.dump(payload, temp, indent=2)
        temp.write("\n")
        temp_name = temp.name
    os.replace(temp_name, path)


def read_runtime_raw() -> Tuple[Dict[str, Any], str]:
    if not os.path.exists(RUNTIME_CONFIG_FILE):
        return {}, ""
    try:
        with open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # pylint: disable=broad-except
        return {}, f"Could not read runtime config: {exc}"
    if not isinstance(payload, dict):
        return {}, "Runtime config is not a JSON object."
    return payload, ""


def read_runtime_settings() -> Tuple[Dict[str, Any], str]:
    env_base_url = env_optional("HA_BASE_URL", "").rstrip("/")
    env_token = env_optional("HA_TOKEN", "")

    runtime_payload, runtime_warning = read_runtime_raw()
    runtime_base_url = str(runtime_payload.get("ha_base_url", "")).strip().rstrip("/")
    runtime_token = str(runtime_payload.get("ha_token", "")).strip()

    effective_base_url = env_base_url or runtime_base_url
    effective_token = env_token or runtime_token
    source = "env" if env_base_url and env_token else "runtime_config" if runtime_base_url and runtime_token else "missing"

    settings = {
        "configured": bool(effective_base_url and effective_token),
        "base_url": effective_base_url,
        "source": source,
        "runtime_base_url": runtime_base_url,
        "runtime_has_token": bool(runtime_token),
        "env_has_base_url": bool(env_base_url),
        "env_has_token": bool(env_token),
    }
    return settings, runtime_warning


def ha_get_states(ha_base_url: str, ha_token: str) -> Tuple[List[Dict[str, Any]], str]:
    if not ha_base_url or not ha_token:
        return [], "Home Assistant is not configured yet."

    url = f"{ha_base_url.rstrip('/')}/api/states"
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Authorization": f"Bearer {ha_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HA_API_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return [], f"HA API error: HTTP {exc.code} {detail}"
    except urllib.error.URLError as exc:
        return [], f"HA API connection error: {exc.reason}"
    except Exception as exc:  # pylint: disable=broad-except
        return [], f"HA API read error: {exc}"

    if not isinstance(payload, list):
        return [], "HA API returned invalid states payload."

    out: List[Dict[str, Any]] = []
    for row in payload:
        if isinstance(row, dict):
            out.append(row)
    return out, ""


def discover_speaker_players(runtime_settings: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    runtime_payload, _ = read_runtime_raw()
    base_url = str(runtime_settings.get("base_url", "")).strip().rstrip("/")
    token = env_optional("HA_TOKEN", "") or str(runtime_payload.get("ha_token", "")).strip()
    states, err = ha_get_states(base_url, token)
    if err:
        return [], err

    unifi_candidates: List[Dict[str, str]] = []
    seen = set()
    for row in states:
        entity_id = str(row.get("entity_id", "")).strip()
        if not entity_id.startswith("media_player."):
            continue
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}

        friendly_name = str(attrs.get("friendly_name", "")).strip()
        attribution = str(attrs.get("attribution", "")).strip()
        manufacturer = str(attrs.get("manufacturer", "")).strip()

        haystack = " ".join([entity_id, friendly_name, attribution, manufacturer]).lower()
        if "speaker" not in haystack:
            continue
        if entity_id in seen:
            continue
        seen.add(entity_id)

        label = friendly_name or entity_id
        candidate = {"entity_id": entity_id, "label": f"{label} ({entity_id})"}

        unifi_haystack = " ".join([entity_id, friendly_name, attribution, manufacturer]).lower()
        if "unifi" in unifi_haystack:
            unifi_candidates.append(candidate)

    unifi_candidates.sort(key=lambda item: item["label"].lower())

    if not unifi_candidates:
        return [], "No UniFi Protect speaker media_player devices were discovered in Home Assistant."
    return unifi_candidates, ""


def infer_audio_file(url: str) -> str:
    raw_url = (url or "").strip()
    if raw_url == "":
        return ""
    if ALERT_URL_BASE and raw_url.startswith(ALERT_URL_BASE + "/"):
        quoted = raw_url[len(ALERT_URL_BASE) + 1 :]
        return urllib.parse.unquote(quoted)
    return ""


def list_audio_files() -> List[str]:
    if not os.path.isdir(AUDIO_DIR):
        return []
    files: List[str] = []
    for root, _, names in os.walk(AUDIO_DIR):
        rel_root = os.path.relpath(root, AUDIO_DIR)
        for name in names:
            if name.startswith("."):
                continue
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            files.append(rel.replace("\\", "/"))
    files.sort()
    return files


def default_watcher(name: str) -> Dict[str, Any]:
    prefix = name.upper()
    url = env_optional(f"{prefix}_ALERT_URL", "")
    return {
        "enabled": True,
        "sensor": env_optional(f"{prefix}_ALERT_SENSOR", ""),
        "player": env_optional(f"{prefix}_ALERT_PLAYER", ""),
        "start_hhmm": env_optional(f"{prefix}_ALERT_START_HHMM", "2230"),
        "end_hhmm": env_optional(f"{prefix}_ALERT_END_HHMM", "0600"),
        "interval_sec": parse_int(env_optional(f"{prefix}_ALERT_INTERVAL_SEC", "120"), 120),
        "min_on_sec": parse_int(env_optional(f"{prefix}_ALERT_MIN_ON_SEC", "5"), 5),
        "idle_poll_sec": parse_int(env_optional(f"{prefix}_ALERT_IDLE_POLL_SEC", "5"), 5),
        "default_volume": parse_float_optional(env_optional(f"{prefix}_ALERT_VOLUME", ""), None),
        "default_media_content_type": env_optional(f"{prefix}_ALERT_MEDIA_CONTENT_TYPE", "music") or "music",
        "default_audio_file": infer_audio_file(url),
        "url": url,
        "time_rules": [],
    }


def read_alerts_config() -> Tuple[Dict[str, Any], str]:
    if not os.path.exists(CONFIG_FILE):
        return {"watchers": {}}, ""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # pylint: disable=broad-except
        return {"watchers": {}}, f"Could not read config: {exc}"

    if not isinstance(payload, dict):
        return {"watchers": {}}, "Config root is not a JSON object."
    watchers = payload.get("watchers", {})
    if not isinstance(watchers, dict):
        return {"watchers": {}}, "Config 'watchers' value is invalid."
    return {"watchers": watchers}, ""


def write_alerts_config(payload: Dict[str, Any]) -> None:
    write_json_atomic(CONFIG_FILE, payload)


def merged_alerts_config() -> Tuple[Dict[str, Dict[str, Any]], str]:
    payload, warning = read_alerts_config()
    watchers_raw = payload.get("watchers", {})
    names: List[str] = []
    for watcher_name in WATCHER_NAMES:
        clean = watcher_name.strip().lower()
        if clean and clean not in names:
            names.append(clean)
    if isinstance(watchers_raw, dict):
        for watcher_name in sorted(watchers_raw.keys()):
            clean = str(watcher_name).strip().lower()
            if clean and clean not in names:
                names.append(clean)

    watchers: Dict[str, Dict[str, Any]] = {}
    for watcher_name in names:
        merged = default_watcher(watcher_name)
        override = watchers_raw.get(watcher_name, {})
        if isinstance(override, dict):
            merged.update(override)
        if not isinstance(merged.get("time_rules"), list):
            merged["time_rules"] = []
        watchers[watcher_name] = merged
    return watchers, warning


def auth_ok(auth_header: str) -> bool:
    if ADMIN_USERNAME == "" and ADMIN_PASSWORD == "":
        return True
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    encoded = auth_header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:  # pylint: disable=broad-except
        return False
    provided_user, _, provided_pass = decoded.partition(":")
    return provided_user == ADMIN_USERNAME and provided_pass == ADMIN_PASSWORD


def rules_text(rules: Any) -> str:
    if not isinstance(rules, list):
        return "[]"
    return json.dumps(rules, indent=2)


def html_input(name: str, value: Any, input_type: str = "text", step: str = "") -> str:
    escaped = html.escape("" if value is None else str(value), quote=True)
    step_part = f' step="{step}"' if step else ""
    return f'<input type="{input_type}" name="{name}" value="{escaped}"{step_part} />'


def html_speaker_select(name: str, selected: str, options: List[Dict[str, str]]) -> str:
    escaped_name = html.escape(name, quote=True)
    selected_value = (selected or "").strip()
    rows: List[str] = [f'<select name="{escaped_name}">']
    rows.append('<option value="">-- Select a speaker device --</option>')

    for opt in options:
        entity_id = opt.get("entity_id", "")
        label = opt.get("label", entity_id)
        if not entity_id:
            continue
        selected_attr = " selected" if entity_id == selected_value else ""
        rows.append(
            f'<option value="{html.escape(entity_id, quote=True)}"{selected_attr}>{html.escape(label)}</option>'
        )

    if selected_value and all(opt.get("entity_id") != selected_value for opt in options):
        rows.append(
            f'<option value="{html.escape(selected_value, quote=True)}" selected>'
            f"{html.escape(selected_value)} (current value)</option>"
        )
    rows.append("</select>")
    return "\n".join(rows)


def page_style() -> str:
    return """
  <style>
    :root {
      --bg: #eef2f4;
      --panel: #ffffff;
      --ink: #1b2733;
      --muted: #607080;
      --accent: #1f6fb2;
      --line: #d5dee6;
    }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 15% 10%, #dce8f2 0%, transparent 40%),
        radial-gradient(circle at 85% 0%, #e4f0ec 0%, transparent 35%),
        var(--bg);
    }
    .wrap {
      max-width: 1080px;
      margin: 0 auto;
      padding: 20px;
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .top a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }
    .notice {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      background: #ecf5ff;
      border: 1px solid #c6def5;
      color: #24455f;
    }
    form {
      margin-top: 14px;
    }
    .grid {
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      display: grid;
      gap: 8px;
      box-shadow: 0 4px 14px rgba(25, 36, 46, 0.06);
    }
    h1 {
      margin: 0;
      font-size: 1.4rem;
    }
    h2 {
      margin: 0;
      font-size: 1.1rem;
    }
    .muted {
      color: var(--muted);
      margin: 0;
    }
    label {
      font-size: 0.9rem;
      font-weight: 600;
    }
    input, textarea {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
      color: inherit;
      background: #f9fbfc;
    }
    textarea {
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      resize: vertical;
    }
    .help {
      margin-top: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
    }
    .actions {
      margin-top: 14px;
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }
    code {
      background: #f0f5f8;
      border: 1px solid #dae5ec;
      border-radius: 5px;
      padding: 2px 5px;
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      font-size: 0.86rem;
    }
  </style>
"""


def render_onboarding(message: str = "", warning: str = "") -> str:
    runtime_settings, runtime_warn = read_runtime_settings()
    notice = message or warning or runtime_warn
    escaped_notice = html.escape(notice)
    base_value = html.escape(runtime_settings.get("runtime_base_url", "") or runtime_settings.get("base_url", ""), quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Unifi Alerts Onboarding</title>
  {page_style()}
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>Unifi Alerts Onboarding</h1>
      <a href="/">Back to Dashboard</a>
    </div>
    {f'<div class="notice">{escaped_notice}</div>' if escaped_notice else ''}
    <section class="card" style="max-width: 700px; margin-top: 14px;">
      <p class="muted">First, connect this stack to Home Assistant.</p>
      <form method="post" action="/onboarding/save">
        <label>Home Assistant IP or URL</label>
        <input type="text" name="ha_base_url" value="{base_value}" placeholder="<ha-ip-or-host> or http://homeassistant.local:8123" />
        <label>Home Assistant Long-Lived Access Token</label>
        <input type="password" name="ha_token" value="" placeholder="Paste token here" />
        <div class="actions">
          <button type="submit">Save HA Connection</button>
        </div>
      </form>
      <p class="muted">If you enter only an IP/host, this page assumes <code>http://</code> and port <code>8123</code>.</p>
    </section>
  </div>
</body>
</html>
"""


def render_dashboard(message: str = "", warning: str = "") -> str:
    watchers, load_warning = merged_alerts_config()
    runtime_settings, runtime_warn = read_runtime_settings()
    audio_files = list_audio_files()
    speaker_options, speaker_error = discover_speaker_players(runtime_settings)

    notices: List[str] = []
    for val in [message, warning, load_warning, runtime_warn, speaker_error]:
        if val:
            notices.append(val)
    notice = " | ".join(notices)
    escaped_notice = html.escape(notice)

    datalist_options = "\n".join(
        f'<option value="{html.escape(path, quote=True)}"></option>' for path in audio_files
    )

    cards: List[str] = []
    for watcher_name, cfg in watchers.items():
        checked = "checked" if parse_bool(cfg.get("enabled"), True) else ""
        time_rules_value = html.escape(rules_text(cfg.get("time_rules")), quote=False)
        card = f"""
        <section class="card">
          <h2>{html.escape(watcher_name)}</h2>
          <p class="muted">Trigger + player mapping for this watcher.</p>
          <label><input type="checkbox" name="{watcher_name}__enabled" {checked}/> Enabled</label>
          <label>Trigger Sensor Entity ID</label>
          {html_input(f"{watcher_name}__sensor", cfg.get("sensor", ""))}
          <label>Media Player Entity ID</label>
          {html_speaker_select(f"{watcher_name}__player", str(cfg.get("player", "")), speaker_options)}
          <label>Default Alert Window Start (HHMM)</label>
          {html_input(f"{watcher_name}__start_hhmm", cfg.get("start_hhmm", "2230"))}
          <label>Default Alert Window End (HHMM)</label>
          {html_input(f"{watcher_name}__end_hhmm", cfg.get("end_hhmm", "0600"))}
          <label>Default Audio File (or full URL)</label>
          <input type="text" name="{watcher_name}__default_audio_file" value="{html.escape(str(cfg.get("default_audio_file", "")), quote=True)}" list="audio-files" />
          <label>Default Interval Sec</label>
          {html_input(f"{watcher_name}__interval_sec", cfg.get("interval_sec", 120), "number")}
          <label>Min ON Sec</label>
          {html_input(f"{watcher_name}__min_on_sec", cfg.get("min_on_sec", 5), "number")}
          <label>Idle Poll Sec</label>
          {html_input(f"{watcher_name}__idle_poll_sec", cfg.get("idle_poll_sec", 5), "number")}
          <label>Default Volume (blank disables volume set)</label>
          {html_input(f"{watcher_name}__default_volume", cfg.get("default_volume", ""), "number", "0.01")}
          <label>Default Media Content Type</label>
          {html_input(f"{watcher_name}__default_media_content_type", cfg.get("default_media_content_type", "music"))}
          <label>Time Rules JSON (optional, for time-of-day sounds)</label>
          <textarea name="{watcher_name}__time_rules_json" rows="12">{time_rules_value}</textarea>
        </section>
        """
        cards.append(card)

    cards_html = "\n".join(cards)
    current_ha = html.escape(runtime_settings.get("base_url", ""), quote=True)
    source = html.escape(runtime_settings.get("source", "missing"), quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Unifi Alerts Admin</title>
  {page_style()}
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>Unifi Alerts Admin</h1>
      <div>
        <a href="{html.escape(FILE_MANAGER_URL, quote=True)}" target="_blank" rel="noopener">Open File Manager</a>
        <span> | </span>
        <a href="/onboarding">HA Onboarding</a>
        <span> | </span>
        <a href="/api/config" target="_blank" rel="noopener">View JSON</a>
      </div>
    </div>
    <div class="notice">HA connection: <code>{current_ha or "not configured"}</code> (source: <code>{source}</code>)</div>
    {f'<div class="notice">{escaped_notice}</div>' if escaped_notice else ''}
    <form method="post" action="/add-zone">
      <div class="actions">
        <input type="text" name="new_zone_name" value="" placeholder="new zone name (example: zone3)" />
        <button type="submit">Add Zone</button>
      </div>
    </form>
    <form method="post" action="/save">
      <datalist id="audio-files">
        {datalist_options}
      </datalist>
      <div class="grid">
        {cards_html}
      </div>
      <div class="actions">
        <button type="submit">Save Settings</button>
      </div>
    </form>
    <section class="help">
      <p class="muted"><strong>Time Rules JSON format</strong></p>
      <p class="muted">If rules exist, alerts only play when one rule matches current time. Each rule can set a different sound for time-of-day.</p>
      <pre><code>[
  {{
    "name": "night",
    "start_hhmm": "2230",
    "end_hhmm": "0600",
    "audio_file": "attention.aiff",
    "interval_sec": 120,
    "volume": 0.6,
    "media_content_type": "music"
  }},
  {{
    "name": "day",
    "start_hhmm": "0600",
    "end_hhmm": "2230",
    "audio_file": "soft-tone.aiff",
    "interval_sec": 180,
    "volume": 0.4,
    "media_content_type": "music"
  }}
]</code></pre>
    </section>
  </div>
</body>
</html>
"""


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "unifi-alerts-admin/1.1"

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="unifi-alerts-admin"')
        self.end_headers()
        self.wfile.write(b"Authentication required.\n")

    def _require_auth(self) -> bool:
        if auth_ok(self.headers.get("Authorization", "")):
            return True
        self._unauthorized()
        return False

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _send_html(self, content: str, status: int = 200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Dict[str, Any], status: int = 200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            if not self._require_auth():
                return
            parsed = urllib.parse.urlparse(self.path)
            runtime_settings, runtime_warning = read_runtime_settings()

            if parsed.path == "/health":
                self._send_json({"ok": True, "time": int(time.time())})
                return
            if parsed.path == "/api/runtime":
                payload = {
                    "configured": runtime_settings.get("configured", False),
                    "base_url": runtime_settings.get("base_url", ""),
                    "source": runtime_settings.get("source", "missing"),
                    "runtime_config_file": RUNTIME_CONFIG_FILE,
                    "warning": runtime_warning,
                }
                self._send_json(payload)
                return
            if parsed.path == "/api/config":
                watchers, warning = merged_alerts_config()
                speaker_players, speaker_error = discover_speaker_players(runtime_settings)
                payload = {
                    "watchers": watchers,
                    "audio_files": list_audio_files(),
                    "speaker_players": speaker_players,
                    "alert_url_base": ALERT_URL_BASE,
                    "warning": warning or speaker_error,
                }
                self._send_json(payload)
                return
            if parsed.path == "/onboarding":
                query = urllib.parse.parse_qs(parsed.query)
                message = query.get("msg", [""])[0]
                warning = query.get("warn", [""])[0]
                self._send_html(render_onboarding(message=message, warning=warning))
                return
            if parsed.path != "/":
                self.send_error(404, "Not Found")
                return

            if not runtime_settings.get("configured"):
                self._redirect("/onboarding?warn=Home+Assistant+is+not+configured")
                return

            query = urllib.parse.parse_qs(parsed.query)
            message = query.get("msg", [""])[0]
            warning = query.get("warn", [""])[0]
            self._send_html(render_dashboard(message=message, warning=warning))
        except Exception as exc:  # pylint: disable=broad-except
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} admin-error method=GET path={self.path} detail={exc}", flush=True)
            print(traceback.format_exc(), flush=True)
            try:
                self._send_html(f"<h1>Internal Server Error</h1><p>{html.escape(str(exc))}</p>", status=500)
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        try:
            if not self._require_auth():
                return
            parsed = urllib.parse.urlparse(self.path)
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")
            form = urllib.parse.parse_qs(body, keep_blank_values=True)

            if parsed.path == "/onboarding/save":
                raw_base_url = form.get("ha_base_url", [""])[0].strip()
                raw_token = form.get("ha_token", [""])[0].strip()
                try:
                    normalized_base_url = normalize_ha_base_url(raw_base_url)
                except ValueError as exc:
                    self._send_html(render_onboarding(message=str(exc)), status=400)
                    return
                if raw_token == "":
                    self._send_html(render_onboarding(message="Home Assistant token is required"), status=400)
                    return

                payload = {
                    "ha_base_url": normalized_base_url,
                    "ha_token": raw_token,
                    "saved_at_unix": int(time.time()),
                }
                try:
                    write_json_atomic(RUNTIME_CONFIG_FILE, payload)
                except Exception as exc:  # pylint: disable=broad-except
                    self._send_html(render_onboarding(message=f"Failed to save runtime config: {exc}"), status=500)
                    return
                self._redirect("/?msg=Home+Assistant+onboarding+saved")
                return

            if parsed.path == "/add-zone":
                new_zone_name = form.get("new_zone_name", [""])[0].strip().lower()
                if not zone_name_valid(new_zone_name):
                    self._send_html(
                        render_dashboard(
                            message="Zone name must match [a-z0-9][a-z0-9_-]{0,31} (examples: zone3, side_gate)"
                        ),
                        status=400,
                    )
                    return

                current_payload, _ = read_alerts_config()
                watchers = current_payload.get("watchers", {})
                if not isinstance(watchers, dict):
                    watchers = {}

                if new_zone_name in watchers:
                    self._redirect(f"/?warn=Zone+already+exists:+{urllib.parse.quote(new_zone_name)}")
                    return

                watchers[new_zone_name] = default_watcher(new_zone_name)
                try:
                    write_alerts_config({"watchers": watchers})
                except Exception as exc:  # pylint: disable=broad-except
                    self._send_html(render_dashboard(message=f"Failed to add zone: {exc}"), status=500)
                    return
                self._redirect(f"/?msg=Added+zone:+{urllib.parse.quote(new_zone_name)}")
                return

            if parsed.path != "/save":
                self.send_error(404, "Not Found")
                return

            runtime_settings, _ = read_runtime_settings()
            if not runtime_settings.get("configured"):
                self._redirect("/onboarding?warn=Please+complete+Home+Assistant+onboarding+first")
                return
            speaker_options, _ = discover_speaker_players(runtime_settings)
            speaker_entities = {item.get("entity_id", "") for item in speaker_options if item.get("entity_id")}

            current_payload, _ = read_alerts_config()
            existing_watchers = current_payload.get("watchers", {})
            if not isinstance(existing_watchers, dict):
                existing_watchers = {}

            merged_watchers, _ = merged_alerts_config()
            watcher_names = list(merged_watchers.keys())

            watchers_out: Dict[str, Any] = dict(existing_watchers)
            parse_errors: List[str] = []
            for watcher_name in watcher_names:
                defaults = default_watcher(watcher_name)

                def field(name: str, default: str = "") -> str:
                    return form.get(f"{watcher_name}__{name}", [default])[0].strip()

                enabled = f"{watcher_name}__enabled" in form
                sensor = field("sensor", str(defaults.get("sensor", "")))
                player = field("player", str(defaults.get("player", "")))
                if player and "speaker" not in player.lower():
                    parse_errors.append(f"{watcher_name}: selected player must contain 'speaker'")
                if speaker_entities and player and player not in speaker_entities:
                    parse_errors.append(f"{watcher_name}: selected player is not in current speaker device list")

                start_hhmm = field("start_hhmm", str(defaults.get("start_hhmm", "2230")))
                end_hhmm = field("end_hhmm", str(defaults.get("end_hhmm", "0600")))
                if not hhmm_valid(start_hhmm):
                    parse_errors.append(f"{watcher_name}: invalid start_hhmm '{start_hhmm}'")
                if not hhmm_valid(end_hhmm):
                    parse_errors.append(f"{watcher_name}: invalid end_hhmm '{end_hhmm}'")

                interval_sec = max(1, parse_int(field("interval_sec", str(defaults.get("interval_sec", 120))), 120))
                min_on_sec = max(0, parse_int(field("min_on_sec", str(defaults.get("min_on_sec", 5))), 5))
                idle_poll_sec = max(1, parse_int(field("idle_poll_sec", str(defaults.get("idle_poll_sec", 5))), 5))
                default_media_content_type = field(
                    "default_media_content_type", str(defaults.get("default_media_content_type", "music"))
                ) or "music"
                default_volume = parse_float_optional(
                    field("default_volume", "" if defaults.get("default_volume") is None else str(defaults["default_volume"])),
                    defaults.get("default_volume"),
                )

                default_audio_file = field("default_audio_file", str(defaults.get("default_audio_file", "")))
                if default_audio_file.startswith("http://") or default_audio_file.startswith("https://"):
                    url = default_audio_file
                elif default_audio_file and ALERT_URL_BASE:
                    encoded = "/".join(
                        urllib.parse.quote(part) for part in default_audio_file.lstrip("/").split("/") if part
                    )
                    url = f"{ALERT_URL_BASE}/{encoded}" if encoded else str(defaults.get("url", ""))
                elif default_audio_file:
                    url = default_audio_file
                else:
                    url = str(defaults.get("url", ""))

                raw_time_rules = field("time_rules_json", "[]")
                try:
                    time_rules = json.loads(raw_time_rules) if raw_time_rules else []
                    if not isinstance(time_rules, list):
                        raise ValueError("time_rules_json must be a JSON array")
                except Exception as exc:  # pylint: disable=broad-except
                    parse_errors.append(f"{watcher_name}: invalid time_rules_json ({exc})")
                    time_rules = []

                normalized_rules: List[Dict[str, Any]] = []
                for idx, rule in enumerate(time_rules):
                    if not isinstance(rule, dict):
                        parse_errors.append(f"{watcher_name}: rule {idx + 1} is not an object")
                        continue
                    rs = str(rule.get("start_hhmm", "")).strip()
                    re = str(rule.get("end_hhmm", "")).strip()
                    if not (hhmm_valid(rs) and hhmm_valid(re)):
                        parse_errors.append(f"{watcher_name}: rule {idx + 1} has invalid HHMM window")
                        continue
                    normalized_rules.append(
                        {
                            "name": str(rule.get("name", f"rule-{idx + 1}")).strip() or f"rule-{idx + 1}",
                            "start_hhmm": rs,
                            "end_hhmm": re,
                            "audio_file": str(rule.get("audio_file", "")).strip(),
                            "interval_sec": max(1, parse_int(rule.get("interval_sec"), interval_sec)),
                            "volume": parse_float_optional(rule.get("volume"), default_volume),
                            "media_content_type": str(rule.get("media_content_type", default_media_content_type)).strip()
                            or default_media_content_type,
                            "enabled": parse_bool(rule.get("enabled"), True),
                        }
                    )

                watchers_out[watcher_name] = {
                    "enabled": enabled,
                    "sensor": sensor,
                    "player": player,
                    "start_hhmm": start_hhmm,
                    "end_hhmm": end_hhmm,
                    "interval_sec": interval_sec,
                    "min_on_sec": min_on_sec,
                    "idle_poll_sec": idle_poll_sec,
                    "default_volume": default_volume,
                    "default_media_content_type": default_media_content_type,
                    "default_audio_file": default_audio_file,
                    "url": url,
                    "time_rules": normalized_rules,
                }

            if parse_errors:
                message = " | ".join(parse_errors)
                self._send_html(render_dashboard(message=message), status=400)
                return

            try:
                write_alerts_config({"watchers": watchers_out})
            except Exception as exc:  # pylint: disable=broad-except
                self._send_html(render_dashboard(message=f"Failed to save config: {exc}"), status=500)
                return

            self._redirect("/?msg=Saved")
        except Exception as exc:  # pylint: disable=broad-except
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} admin-error method=POST path={self.path} detail={exc}", flush=True)
            print(traceback.format_exc(), flush=True)
            try:
                self._send_html(f"<h1>Internal Server Error</h1><p>{html.escape(str(exc))}</p>", status=500)
            except Exception:
                pass


def main():
    tz = os.getenv("TZ", "").strip()
    if tz:
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()
    server = ThreadingHTTPServer((HOST, PORT), AdminHandler)
    print(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} admin-started "
        f"bind={HOST}:{PORT} config_file={CONFIG_FILE} runtime_config_file={RUNTIME_CONFIG_FILE} "
        f"audio_dir={AUDIO_DIR} watchers={','.join(WATCHER_NAMES)}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
