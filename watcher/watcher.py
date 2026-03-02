#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def clean_env_value(value: Any) -> str:
    text = str(value or "").strip()
    if text == "$":
        return ""
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text


def env_required(name: str) -> str:
    value = clean_env_value(os.getenv(name, ""))
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    return clean_env_value(os.getenv(name, default))


def parse_int(value: Any, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def parse_float_optional(value: Any, default: Optional[float]) -> Optional[float]:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def hhmm_to_minutes(hhmm: str) -> int:
    clean = hhmm.strip()
    if len(clean) != 4 or not clean.isdigit():
        raise ValueError(f"Invalid HHMM value: {hhmm!r}")
    hour = int(clean[:2])
    minute = int(clean[2:])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HHMM value: {hhmm!r}")
    return hour * 60 + minute


def hhmm_or_default(value: str, default: str) -> str:
    text = str(value or "").strip()
    if text == "":
        return default
    try:
        hhmm_to_minutes(text)
        return text
    except ValueError:
        return default


def within_window(start_min: int, end_min: int, now_min: int) -> bool:
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def build_media_url(audio_or_url: str, url_base: str, fallback_url: str) -> str:
    raw = str(audio_or_url or "").strip()
    if raw == "":
        return fallback_url
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if not url_base:
        return raw
    parts = [urllib.parse.quote(part) for part in raw.lstrip("/").split("/") if part]
    suffix = "/".join(parts)
    if suffix == "":
        return fallback_url
    return f"{url_base.rstrip('/')}/{suffix}"


def load_runtime_ha_config(runtime_config_file: str) -> Tuple[str, str, Optional[str]]:
    if not runtime_config_file:
        return "", "", None
    try:
        with open(runtime_config_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return "", "", f"runtime-config-file-not-found file={runtime_config_file}"
    except Exception as exc:  # pylint: disable=broad-except
        return "", "", f"runtime-config-read-failed file={runtime_config_file} detail={exc}"

    if not isinstance(payload, dict):
        return "", "", f"runtime-config-invalid-root file={runtime_config_file}"

    base_url = str(payload.get("ha_base_url", "")).strip().rstrip("/")
    token = str(payload.get("ha_token", "")).strip()
    return base_url, token, None


def resolve_ha_credentials(
    env_base_url: str, env_token: str, runtime_config_file: str
) -> Tuple[str, str, Optional[str]]:
    base_url = env_base_url.strip().rstrip("/")
    token = env_token.strip()

    runtime_base_url = ""
    runtime_token = ""
    runtime_err = None
    if not base_url or not token:
        runtime_base_url, runtime_token, runtime_err = load_runtime_ha_config(runtime_config_file)

    if not base_url:
        base_url = runtime_base_url
    if not token:
        token = runtime_token
    return base_url, token, runtime_err


class HAClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload=None):
        url = f"{self.base_url}{path}"
        body = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            req = urllib.request.Request(url=url, method=method, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed = raw
                return resp.status, parsed, None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return exc.code, None, detail
        except urllib.error.URLError as exc:
            return -1, None, str(exc.reason)
        except Exception as exc:  # pylint: disable=broad-except
            return -1, None, str(exc)

    def get_entity_state(self, entity_id: str):
        quoted = urllib.parse.quote(entity_id, safe="._")
        status, data, err = self._request("GET", f"/api/states/{quoted}")
        if status != 200:
            return None, status, err
        if not isinstance(data, dict):
            return None, status, "Invalid state payload"
        state = str(data.get("state", ""))
        return state, status, None

    def set_volume(self, player_entity: str, volume: float):
        payload = {"entity_id": player_entity, "volume_level": volume}
        return self._request("POST", "/api/services/media_player/volume_set", payload)

    def play_media(self, player_entity: str, media_url: str, media_content_type: str):
        payload = {
            "entity_id": player_entity,
            "media_content_id": media_url,
            "media_content_type": media_content_type,
        }
        return self._request("POST", "/api/services/media_player/play_media", payload)


@dataclass
class TimeRule:
    name: str
    start_min: int
    end_min: int
    alert_url: str
    interval_sec: int
    volume: Optional[float]
    media_content_type: str
    enabled: bool = True


@dataclass
class RuntimeConfig:
    watcher_name: str
    sensor: str
    player: str
    alert_url: str
    start_hhmm: str
    end_hhmm: str
    start_min: int
    end_min: int
    interval_sec: int
    min_on_sec: int
    idle_poll_sec: int
    volume: Optional[float]
    media_content_type: str
    enabled: bool
    time_rules: List[TimeRule]


@dataclass
class PlaybackPlan:
    alert_url: str
    interval_sec: int
    volume: Optional[float]
    media_content_type: str
    source: str


def ts_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def mins_now() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def log(msg: str):
    print(f"{ts_now()} {msg}", flush=True)


def load_watcher_overrides(config_file: str, watcher_name: str) -> Tuple[Dict[str, Any], Optional[str]]:
    if not config_file:
        return {}, None
    try:
        with open(config_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}, f"config-file-not-found file={config_file}"
    except Exception as exc:  # pylint: disable=broad-except
        return {}, f"config-read-failed file={config_file} detail={exc}"

    if not isinstance(payload, dict):
        return {}, f"config-invalid-root file={config_file}"

    watchers = payload.get("watchers", payload)
    if not isinstance(watchers, dict):
        return {}, f"config-invalid-watchers file={config_file}"

    overrides = watchers.get(watcher_name, {})
    if not isinstance(overrides, dict):
        return {}, f"config-invalid-watcher file={config_file} watcher={watcher_name}"
    return overrides, None


def parse_time_rule(
    raw: Dict[str, Any], base_cfg: RuntimeConfig, alert_url_base: str, idx: int
) -> Optional[TimeRule]:
    if not isinstance(raw, dict):
        return None
    start_hhmm = str(raw.get("start_hhmm", "")).strip()
    end_hhmm = str(raw.get("end_hhmm", "")).strip()
    if start_hhmm == "" or end_hhmm == "":
        return None
    try:
        start_min = hhmm_to_minutes(start_hhmm)
        end_min = hhmm_to_minutes(end_hhmm)
    except ValueError:
        return None

    raw_audio = raw.get("audio_file")
    raw_url = raw.get("url")
    if raw_audio is not None and str(raw_audio).strip() != "":
        alert_url = build_media_url(str(raw_audio), alert_url_base, base_cfg.alert_url)
    elif raw_url is not None and str(raw_url).strip() != "":
        alert_url = build_media_url(str(raw_url), alert_url_base, base_cfg.alert_url)
    else:
        alert_url = base_cfg.alert_url

    interval_sec = max(1, parse_int(raw.get("interval_sec"), base_cfg.interval_sec))
    volume = parse_float_optional(raw.get("volume"), base_cfg.volume)
    media_content_type = str(raw.get("media_content_type", base_cfg.media_content_type)).strip()
    if media_content_type == "":
        media_content_type = base_cfg.media_content_type

    name = str(raw.get("name", f"rule-{idx + 1}")).strip() or f"rule-{idx + 1}"
    enabled = parse_bool(raw.get("enabled"), True)
    return TimeRule(
        name=name,
        start_min=start_min,
        end_min=end_min,
        alert_url=alert_url,
        interval_sec=interval_sec,
        volume=volume,
        media_content_type=media_content_type,
        enabled=enabled,
    )


def merge_runtime_config(
    base_cfg: RuntimeConfig, overrides: Dict[str, Any], alert_url_base: str
) -> RuntimeConfig:
    sensor = str(overrides.get("sensor", base_cfg.sensor)).strip() or base_cfg.sensor
    player = str(overrides.get("player", base_cfg.player)).strip() or base_cfg.player

    start_hhmm = str(overrides.get("start_hhmm", base_cfg.start_hhmm)).strip() or base_cfg.start_hhmm
    end_hhmm = str(overrides.get("end_hhmm", base_cfg.end_hhmm)).strip() or base_cfg.end_hhmm
    try:
        start_min = hhmm_to_minutes(start_hhmm)
        end_min = hhmm_to_minutes(end_hhmm)
    except ValueError:
        start_hhmm = base_cfg.start_hhmm
        end_hhmm = base_cfg.end_hhmm
        start_min = base_cfg.start_min
        end_min = base_cfg.end_min

    base_alert_url = base_cfg.alert_url
    override_default_audio = str(overrides.get("default_audio_file", "")).strip()
    override_url = str(overrides.get("url", "")).strip() or str(overrides.get("default_url", "")).strip()
    if override_default_audio:
        alert_url = build_media_url(override_default_audio, alert_url_base, base_alert_url)
    elif override_url:
        alert_url = build_media_url(override_url, alert_url_base, base_alert_url)
    else:
        alert_url = base_alert_url

    interval_sec = max(1, parse_int(overrides.get("interval_sec", overrides.get("default_interval_sec")), base_cfg.interval_sec))
    min_on_sec = max(0, parse_int(overrides.get("min_on_sec"), base_cfg.min_on_sec))
    idle_poll_sec = max(1, parse_int(overrides.get("idle_poll_sec"), base_cfg.idle_poll_sec))
    volume = parse_float_optional(overrides.get("volume", overrides.get("default_volume")), base_cfg.volume)

    media_content_type = str(
        overrides.get("media_content_type", overrides.get("default_media_content_type", base_cfg.media_content_type))
    ).strip()
    if media_content_type == "":
        media_content_type = base_cfg.media_content_type

    enabled = parse_bool(overrides.get("enabled"), base_cfg.enabled)

    merged = RuntimeConfig(
        watcher_name=base_cfg.watcher_name,
        sensor=sensor,
        player=player,
        alert_url=alert_url,
        start_hhmm=start_hhmm,
        end_hhmm=end_hhmm,
        start_min=start_min,
        end_min=end_min,
        interval_sec=interval_sec,
        min_on_sec=min_on_sec,
        idle_poll_sec=idle_poll_sec,
        volume=volume,
        media_content_type=media_content_type,
        enabled=enabled,
        time_rules=[],
    )

    raw_rules = overrides.get("time_rules", [])
    if isinstance(raw_rules, list):
        for idx, raw_rule in enumerate(raw_rules):
            parsed = parse_time_rule(raw_rule, merged, alert_url_base, idx)
            if parsed is not None:
                merged.time_rules.append(parsed)
    return merged


def resolve_playback_plan(cfg: RuntimeConfig, now_minute: int) -> Tuple[Optional[PlaybackPlan], str]:
    if not cfg.enabled:
        return None, "watcher_disabled"

    if cfg.time_rules:
        for rule in cfg.time_rules:
            if not rule.enabled:
                continue
            if within_window(rule.start_min, rule.end_min, now_minute):
                return (
                    PlaybackPlan(
                        alert_url=rule.alert_url,
                        interval_sec=rule.interval_sec,
                        volume=rule.volume,
                        media_content_type=rule.media_content_type,
                        source=f"time_rule:{rule.name}",
                    ),
                    "ok",
                )
        return None, "outside_all_time_rules"

    if within_window(cfg.start_min, cfg.end_min, now_minute):
        return (
            PlaybackPlan(
                alert_url=cfg.alert_url,
                interval_sec=cfg.interval_sec,
                volume=cfg.volume,
                media_content_type=cfg.media_content_type,
                source="default_window",
            ),
            "ok",
        )
    return None, "outside_default_window"


def sensor_is_on(client: HAClient, sensor: str) -> bool:
    state, status, err = client.get_entity_state(sensor)
    if state is None:
        log(f"sensor={sensor} state-read-failed status={status} detail={err}")
        return False
    return state.lower() == "on"


def wait_for_continuous_on(client: HAClient, sensor: str, min_on_sec: int, check_step: float) -> bool:
    deadline = time.monotonic() + max(0, min_on_sec)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(check_step, remaining))
        if not sensor_is_on(client, sensor):
            return False


def wait_until_boundary_or_off(
    client: HAClient, sensor: str, boundary_monotonic: float, check_step: float
) -> bool:
    while True:
        remaining = boundary_monotonic - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(check_step, remaining))
        if not sensor_is_on(client, sensor):
            return False


def main():
    try:
        tz = os.getenv("TZ", "").strip()
        if tz:
            os.environ["TZ"] = tz
            if hasattr(time, "tzset"):
                time.tzset()

        env_base_url = env_optional("HA_BASE_URL")
        env_token = env_optional("HA_TOKEN")
        runtime_config_file = env_optional("RUNTIME_CONFIG_FILE", "/config/runtime.json")
        watcher_name = env_optional("WATCHER_NAME", "default")
        config_file = env_optional("CONFIG_FILE")
        alert_url_base = env_optional("ALERT_URL_BASE").rstrip("/")

        watcher_prefix = watcher_name.upper()

        def env_for_watcher(key: str, default: str = "") -> str:
            return env_optional(f"{watcher_prefix}_{key}", env_optional(key, default))

        base_sensor = env_for_watcher("ALERT_SENSOR")
        base_player = env_for_watcher("ALERT_PLAYER")
        base_alert_url = env_for_watcher("ALERT_URL")
        if not alert_url_base:
            parsed_base_url = urllib.parse.urlparse(base_alert_url)
            if parsed_base_url.scheme and parsed_base_url.netloc:
                alert_url_base = f"{parsed_base_url.scheme}://{parsed_base_url.netloc}"
        base_start_hhmm = hhmm_or_default(env_for_watcher("ALERT_START_HHMM", "0000"), "0000")
        base_end_hhmm = hhmm_or_default(env_for_watcher("ALERT_END_HHMM", "2359"), "2359")
        base_interval_sec = max(1, parse_int(env_for_watcher("ALERT_INTERVAL_SEC", "120"), 120))
        base_min_on_sec = max(0, parse_int(env_for_watcher("ALERT_MIN_ON_SEC", "5"), 5))
        base_idle_poll_sec = max(1, parse_int(env_for_watcher("ALERT_IDLE_POLL_SEC", "5"), 5))
        base_volume = parse_float_optional(env_for_watcher("ALERT_VOLUME"), None)
        base_media_content_type = env_for_watcher("ALERT_MEDIA_CONTENT_TYPE", "music") or "music"
        base_enabled = parse_bool(env_for_watcher("ALERT_ENABLED", "true"), True)

        base_cfg = RuntimeConfig(
            watcher_name=watcher_name,
            sensor=base_sensor,
            player=base_player,
            alert_url=base_alert_url,
            start_hhmm=base_start_hhmm,
            end_hhmm=base_end_hhmm,
            start_min=hhmm_to_minutes(base_start_hhmm),
            end_min=hhmm_to_minutes(base_end_hhmm),
            interval_sec=base_interval_sec,
            min_on_sec=base_min_on_sec,
            idle_poll_sec=base_idle_poll_sec,
            volume=base_volume,
            media_content_type=base_media_content_type,
            enabled=base_enabled,
            time_rules=[],
        )

        client: Optional[HAClient] = None
        client_signature = ""
        last_config_log = ""
        last_config_error = ""
        last_runtime_error = ""
        last_missing_ha_log = False
        last_missing_target_log = False

        log(
            "watcher-started "
            f"name={watcher_name} sensor={base_cfg.sensor} player={base_cfg.player} "
            f"default_window={base_cfg.start_hhmm}-{base_cfg.end_hhmm} "
            f"default_interval={base_cfg.interval_sec}s default_min_on={base_cfg.min_on_sec}s "
            f"default_idle_poll={base_cfg.idle_poll_sec}s config_file={config_file or 'none'} "
            f"runtime_config_file={runtime_config_file or 'none'}"
        )

        while True:
            base_url, token, runtime_err = resolve_ha_credentials(env_base_url, env_token, runtime_config_file)
            if runtime_err and runtime_err != last_runtime_error:
                log(f"watcher={watcher_name} runtime-config-warning detail={runtime_err}")
                last_runtime_error = runtime_err
            if not runtime_err:
                last_runtime_error = ""

            if not base_url or not token:
                if not last_missing_ha_log:
                    log(
                        f"watcher={watcher_name} waiting-for-onboarding "
                        "reason=missing_ha_base_url_or_token"
                    )
                    last_missing_ha_log = True
                time.sleep(base_cfg.idle_poll_sec)
                continue
            if last_missing_ha_log:
                log(f"watcher={watcher_name} onboarding-config-detected")
                last_missing_ha_log = False

            next_signature = f"{base_url}|{token}"
            if client is None or next_signature != client_signature:
                client = HAClient(base_url, token)
                client_signature = next_signature
                log(f"watcher={watcher_name} ha-client-updated base_url={base_url}")

            overrides, config_err = load_watcher_overrides(config_file, watcher_name)
            if config_err and config_err != last_config_error:
                log(f"watcher={watcher_name} config-warning detail={config_err}")
                last_config_error = config_err
            if not config_err:
                last_config_error = ""

            cfg = merge_runtime_config(base_cfg, overrides, alert_url_base)
            config_log_line = (
                f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} enabled={cfg.enabled} "
                f"default_window={cfg.start_hhmm}-{cfg.end_hhmm} default_url={cfg.alert_url} "
                f"default_interval={cfg.interval_sec}s min_on={cfg.min_on_sec}s idle_poll={cfg.idle_poll_sec}s "
                f"time_rules={len(cfg.time_rules)}"
            )
            if config_log_line != last_config_log:
                log(f"config-updated {config_log_line}")
                last_config_log = config_log_line

            if not cfg.enabled:
                time.sleep(cfg.idle_poll_sec)
                continue

            if not cfg.sensor or not cfg.player or not cfg.alert_url:
                if not last_missing_target_log:
                    log(
                        f"watcher={watcher_name} waiting-for-zone-config "
                        f"sensor={cfg.sensor or 'missing'} player={cfg.player or 'missing'} "
                        f"alert_url={cfg.alert_url or 'missing'}"
                    )
                    last_missing_target_log = True
                time.sleep(cfg.idle_poll_sec)
                continue
            if last_missing_target_log:
                log(f"watcher={watcher_name} zone-config-detected")
                last_missing_target_log = False

            check_step = max(0.5, min(float(cfg.idle_poll_sec), 5.0))

            if not sensor_is_on(client, cfg.sensor):
                time.sleep(cfg.idle_poll_sec)
                continue

            if not wait_for_continuous_on(client, cfg.sensor, cfg.min_on_sec, check_step):
                time.sleep(cfg.idle_poll_sec)
                continue

            plan, reason = resolve_playback_plan(cfg, mins_now())
            if plan is not None:
                if plan.volume is not None:
                    status, _, err = client.set_volume(cfg.player, plan.volume)
                    if status != 200:
                        log(
                            f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                            f"action=volume_set status={status} detail={err}"
                        )
                status, _, err = client.play_media(cfg.player, plan.alert_url, plan.media_content_type)
                log(
                    f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                    f"action=play_media source={plan.source} status={status} "
                    f"url={plan.alert_url} detail={err}"
                )
                next_interval = max(1, plan.interval_sec)
            else:
                log(
                    f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                    f"action=play_media status=skipped reason={reason}"
                )
                next_interval = max(1, cfg.interval_sec)

            next_boundary = time.monotonic() + next_interval
            while True:
                still_on = wait_until_boundary_or_off(client, cfg.sensor, next_boundary, check_step)
                if not still_on:
                    break

                base_url, token, _ = resolve_ha_credentials(env_base_url, env_token, runtime_config_file)
                next_signature = f"{base_url}|{token}"
                if base_url and token and next_signature != client_signature:
                    client = HAClient(base_url, token)
                    client_signature = next_signature
                    log(f"watcher={watcher_name} ha-client-updated base_url={base_url}")

                overrides, _ = load_watcher_overrides(config_file, watcher_name)
                cfg = merge_runtime_config(base_cfg, overrides, alert_url_base)
                check_step = max(0.5, min(float(cfg.idle_poll_sec), 5.0))

                plan, reason = resolve_playback_plan(cfg, mins_now())
                if plan is not None:
                    if plan.volume is not None:
                        status, _, err = client.set_volume(cfg.player, plan.volume)
                        if status != 200:
                            log(
                                f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                                f"action=volume_set status={status} detail={err}"
                            )
                    status, _, err = client.play_media(cfg.player, plan.alert_url, plan.media_content_type)
                    log(
                        f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                        f"action=play_media source={plan.source} status={status} "
                        f"url={plan.alert_url} detail={err}"
                    )
                    next_interval = max(1, plan.interval_sec)
                else:
                    log(
                        f"watcher={watcher_name} sensor={cfg.sensor} player={cfg.player} "
                        f"action=play_media status=skipped reason={reason}"
                    )
                    next_interval = max(1, cfg.interval_sec)
                next_boundary = time.monotonic() + next_interval
    except Exception as exc:  # pylint: disable=broad-except
        log(f"fatal-error detail={exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
