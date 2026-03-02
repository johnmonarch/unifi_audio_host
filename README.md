# unifi-alerts-service

Docker Compose stack for Home Assistant alert audio with:

- Audio hosting (`alerts-http`)
- Web file manager for uploads (`alerts-files`)
- Web admin for trigger/time/sound rules (`alerts-admin`)
- Multi-zone watcher runner (`watch-zones`) with as many zones as you want
- First-run onboarding UI for HA URL/IP + long-lived token

## Repo Layout

```text
unifi-alerts-service/
  docker-compose.yml
  docker-compose.cosmos.yml
  .env.example
  .gitignore
  .github/workflows/
    publish-images.yml
  admin/
    Dockerfile
    admin.py
  config/
    alerts.json.example
  watcher/
    Dockerfile
    watcher.py
    requirements.txt
  nginx/
    default.conf
  README.md
```

## Services

- `alerts-http` (`nginx:alpine`): serves shared `alerts_audio` volume on port `8125`
- `alerts-files` (`filebrowser/filebrowser`): upload/manage audio files on port `8126`
- `alerts-admin` (`python:3.12-alpine`): onboarding + rules dashboard on port `8127`
- `watch-zones` (`python:3.12-alpine`): runs one watcher process per zone listed in `WATCHER_NAMES`

## Deploy (Cosmos Cloud, No SSH)

1. Push this folder to a GitHub repo (private or public).
2. In Cosmos Cloud, create a new Compose stack from the repo.
3. Select `unifi-alerts-service/docker-compose.yml`.
4. Add env vars from `.env.example` (especially `ADMIN_PASSWORD`, entity IDs, and URL base values).
5. Deploy.

This stack uses Docker named volumes (`alerts_audio`, `alerts_config`, `filebrowser_data`), so no host bind-mount prep is required.

## Deploy (Cosmos Paste-Only Compose)

Use this when Cosmos asks you to paste raw YAML and cannot read your repo files.

1. Push repo to GitHub.
2. Publish images to GHCR using the included GitHub Action:
   - Workflow: `.github/workflows/publish-images.yml`
   - Trigger: push to `main` (or run `workflow_dispatch` manually)
3. In Cosmos, paste `docker-compose.cosmos.yml`.
4. Set these env vars in Cosmos:
   - `ALERTS_ADMIN_IMAGE=ghcr.io/<your-github-user>/unifi-alerts-admin:latest`
   - `ALERTS_WATCHER_IMAGE=ghcr.io/<your-github-user>/unifi-alerts-watcher:latest`
   - plus the normal vars from `.env.example`.
5. Deploy.

`docker-compose.cosmos.yml` is image-only: no local `build:` contexts and no local `./` file mounts.

## First-Run Onboarding

After deploy:

1. Open `http://<your-host>:8127/onboarding`
2. Enter:
   - Home Assistant IP or URL (example: `<ha-ip-or-host>` or `http://homeassistant.local:8123`)
   - Home Assistant long-lived access token
3. Save.

The onboarding form writes HA connection settings to `/config/runtime.json` (inside the shared config volume). Watchers will auto-detect and start using it.

## Web UIs

- File manager: `http://<your-host>:8126`
- Alerts admin: `http://<your-host>:8127`

## Upload Audio Files

Recommended:

- Use the file manager (`:8126`) and upload files into the root folder.

Confirm hosted file:

```bash
curl -I http://<your-host>:8125/attention.aiff
```

## Configure Triggers, Sounds, and Time Windows

Open `http://<your-host>:8127` and configure each watcher:

- Trigger sensor (`binary_sensor...`)
- Media player (`media_player...`) selected from discovered speaker devices
- Default sound file
- Default start/end time window
- Interval, debounce, volume, content type
- Optional `time_rules` JSON for different sounds by time of day

Changes are saved to `/config/alerts.json` (shared config volume). Watchers reload it during runtime.
Speaker options are pulled from Home Assistant states and filtered to UniFi Protect `media_player` entities containing `speaker`.
Use the `Add Zone` control in the dashboard to create as many zones as needed (for many cameras/speakers).

## Time Rules Example

```json
[
  {
    "name": "night",
    "start_hhmm": "2230",
    "end_hhmm": "0600",
    "audio_file": "attention.aiff",
    "interval_sec": 120,
    "volume": 0.6,
    "media_content_type": "music",
    "enabled": true
  },
  {
    "name": "day",
    "start_hhmm": "0600",
    "end_hhmm": "2230",
    "audio_file": "soft-tone.aiff",
    "interval_sec": 180,
    "volume": 0.4,
    "media_content_type": "music",
    "enabled": true
  }
]
```

Behavior:

- If `time_rules` is non-empty, the first matching rule controls playback.
- If no rule matches, playback is skipped.
- If `time_rules` is empty, default watcher window/sound are used.

## Manual HA Play Test

```bash
curl -sS -X POST "http://<ha-host>:8123/api/services/media_player/play_media" \
  -H "Authorization: Bearer <ha-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_id": "media_player.your_target_speaker",
    "media_content_id": "http://<your-host>:8125/attention.aiff",
    "media_content_type": "music"
  }'
```

## Configuration

Common:

- `TZ` (example: `America/New_York`)
- `ALERTS_ADMIN_IMAGE` (for `docker-compose.cosmos.yml`)
- `ALERTS_WATCHER_IMAGE` (for `docker-compose.cosmos.yml`)
- `WATCHER_NAMES` (comma-separated zone names; example: `zone1,zone2,zone3`)
- `HA_BASE_URL` (optional: env override for onboarding value)
- `HA_TOKEN` (optional: env override for onboarding value)
- `ALERT_URL_BASE` (example: `http://<your-host>:8125`)
- `FILE_MANAGER_URL` (example: `http://<your-host>:8126`)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- optional generic fallback defaults for any zone missing values in `/config/alerts.json`:
  - `ALERT_SENSOR`, `ALERT_PLAYER`, `ALERT_URL`
  - `ALERT_START_HHMM`, `ALERT_END_HHMM`
  - `ALERT_INTERVAL_SEC`, `ALERT_MIN_ON_SEC`, `ALERT_IDLE_POLL_SEC`
  - `ALERT_VOLUME`, `ALERT_MEDIA_CONTENT_TYPE`

Per watcher:

- Optional per-zone env defaults by prefix (example for zone name `zone3`):
  - `ZONE3_ALERT_SENSOR`
  - `ZONE3_ALERT_PLAYER`
  - `ZONE3_ALERT_URL`
  - `ZONE3_ALERT_START_HHMM`, `ZONE3_ALERT_END_HHMM`
  - `ZONE3_ALERT_INTERVAL_SEC`, `ZONE3_ALERT_MIN_ON_SEC`, `ZONE3_ALERT_IDLE_POLL_SEC`
  - `ZONE3_ALERT_VOLUME`, `ZONE3_ALERT_MEDIA_CONTENT_TYPE`

## Security Notes

- Keep `.env` out of Git.
- Use Cosmos secrets for sensitive env vars when possible.
- Restrict access to `:8126` and `:8127` (LAN/VPN/reverse proxy auth).
- Rotate HA token if exposed.

## Troubleshooting

- Onboarding not saved:
  - Check `alerts-admin` logs.
  - Confirm `alerts_config` volume is writable.
- Watchers waiting for HA config:
  - Complete `/onboarding`, or set `HA_BASE_URL` + `HA_TOKEN` env vars.
- Audio not playing:
  - Verify `ALERT_URL` is reachable from Home Assistant.
  - Confirm file exists and `curl -I http://<your-host>:8125/<file>` returns `200`.
  - Confirm the selected device appears in the speaker dropdown.
- Time window behavior:
  - `2230` to `0600` is treated as crossing midnight.
  - Start is inclusive, end is exclusive.
