# Twitch247 Setup Guide

Detailed installation and configuration guide for Debian/Ubuntu servers.

## Prerequisites

### Hardware

| Component | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4 cores |
| RAM | 2 GB | 4 GB |
| Disk | 5 GB | 10 GB |
| Upload | 5 Mbps | 10+ Mbps |

### Software

The installer provisions the required packages automatically, but these are the runtime dependencies:

```bash
python3 --version    # 3.12+
ffmpeg -version
yt-dlp --version
sqlite3 --version
```

## Step-by-Step Installation

### 1. Prepare the Server

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl
```

### 2. Deploy the Application

Clone the current repository and switch into it:

```bash
git clone https://github.com/TheGalitube/twitch-247-yt-vods.git /tmp/twitch247
cd /tmp/twitch247
git branch --show-current
```

Run the installer as root:

```bash
sudo bash scripts/install.sh
```

The installer will:

- create the `twitch247` system user
- install Python, ffmpeg, sqlite3, and yt-dlp
- create `/opt/twitch247`
- copy the application files into place
- create `/opt/twitch247/config/config.env` from the example file
- create the Python virtual environment and install dependencies
- initialize the SQLite database
- install and enable the systemd services

### 3. Configure

Open the generated config file:

```bash
sudo nano /opt/twitch247/config/config.env
```

Minimum required settings:

```env
TWITCH_STREAM_KEY=live_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWITCH_CHANNEL=yourchannel
YOUTUBE_CHANNEL_URL=https://youtube.com/@yourchannel/streams
```

Important optional settings:

```env
YOUTUBE_SYNC_LIMIT=15
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DISCORD_WEBHOOK_URL=
```

### 4. Optional: Protect the Dashboard with Discord Login

If you want the dashboard behind Discord OAuth, configure these values:

```env
DASHBOARD_SECRET_KEY=replace-with-random-secret
DISCORD_CLIENT_ID=your-discord-client-id
DISCORD_CLIENT_SECRET=your-discord-client-secret
DISCORD_OAUTH_REDIRECT_URI=https://dashboard.example.com/oauth/callback
DASHBOARD_ALLOWED_DISCORD_USER_IDS=123456789012345678,234567890123456789
```

Notes:

- `DASHBOARD_SECRET_KEY` should be a long random secret.
- `DISCORD_OAUTH_REDIRECT_URI` must exactly match the redirect URI configured in your Discord application.
- `DASHBOARD_ALLOWED_DISCORD_USER_IDS` accepts comma-separated or space-separated Discord user IDs.

### 5. Test Before Starting Services

Validate that yt-dlp can read the configured channel:

```bash
yt-dlp --flat-playlist --print "%(id)s %(title)s" \
  "https://youtube.com/@yourchannel/streams" | head -5
```

Run a manual sync:

```bash
sudo -u twitch247 /opt/twitch247/scripts/sync-youtube.sh
```

### 6. Start Services

```bash
sudo systemctl start twitch247
sudo systemctl start twitch247-dashboard
sudo systemctl start twitch247-watchdog.timer
```

### 7. Verify

Check service health:

```bash
systemctl status twitch247 --no-pager
systemctl status twitch247-dashboard --no-pager
systemctl status twitch247-watchdog.timer --no-pager
```

Check the dashboard API:

```bash
curl http://127.0.0.1:8080/api/status
```

Follow the main service logs:

```bash
journalctl -u twitch247 -f
```

## Configuration Reference

Core settings:

| Variable | Default | Description |
|---|---|---|
| `TWITCH_STREAM_KEY` | — | Twitch RTMP stream key |
| `TWITCH_CHANNEL` | — | Twitch channel name |
| `YOUTUBE_CHANNEL_URL` | — | YouTube streams tab URL |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `SAVE_INTERVAL` | `15` | Playback position save interval in seconds |
| `SYNC_INTERVAL` | `3600` | YouTube sync interval in seconds |
| `YOUTUBE_SYNC_LIMIT` | `15` | Number of latest YouTube streams kept in the queue |
| `SEEK_TOLERANCE` | `5` | Seconds to seek back on resume |

Streaming settings:

| Variable | Default | Description |
|---|---|---|
| `VIDEO_BITRATE` | `6000k` | Target video bitrate |
| `MAXRATE` | `6000k` | Encoder maxrate |
| `BUFSIZE` | `12000k` | Encoder buffer size |
| `AUDIO_BITRATE` | `160k` | AAC audio bitrate |

Dashboard and notification settings:

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` if unset | Dashboard bind host |
| `DASHBOARD_PORT` | `8080` | Dashboard port |
| `DASHBOARD_SECRET_KEY` | — | Flask session secret for dashboard auth |
| `DISCORD_CLIENT_ID` | — | Discord OAuth client ID |
| `DISCORD_CLIENT_SECRET` | — | Discord OAuth client secret |
| `DISCORD_OAUTH_REDIRECT_URI` | — | Discord OAuth callback URL |
| `DASHBOARD_ALLOWED_DISCORD_USER_IDS` | — | Allowed Discord user IDs |
| `DISCORD_WEBHOOK_URL` | — | Optional Discord webhook notifications |

Path settings:

| Variable | Default | Description |
|---|---|---|
| `APP_ROOT` | `/opt/twitch247` | Application root |
| `DB_PATH` | `/opt/twitch247/database/twitch247.db` | SQLite database path |
| `LOG_DIR` | `/opt/twitch247/logs` | Log directory |

## Monitoring

### Log Files

```bash
tail -f /opt/twitch247/logs/twitch247.log
tail -f /opt/twitch247/logs/playback.log
tail -f /opt/twitch247/logs/error.log
```

### Manual YouTube Sync

```bash
sudo -u twitch247 /opt/twitch247/scripts/sync-youtube.sh
```

### Dashboard

Open the dashboard in a browser:

```text
http://your-server:8080
```

If you expose it publicly, put it behind a reverse proxy and enable Discord login.

## Updating yt-dlp

YouTube changes frequently. Refresh yt-dlp regularly:

```bash
sudo yt-dlp -U
```

## Troubleshooting

| Problem | Solution |
|---|---|
| No videos found | Verify `YOUTUBE_CHANNEL_URL` and run `sync-youtube.sh` manually |
| Stream does not start | Check `TWITCH_STREAM_KEY`, `journalctl -u twitch247 -f`, and `logs/error.log` |
| Dashboard unreachable | Check `DASHBOARD_HOST`, `DASHBOARD_PORT`, and `systemctl status twitch247-dashboard` |
| Discord login fails | Verify the exact OAuth redirect URI and Discord client credentials |
| yt-dlp breaks on YouTube changes | Run `sudo yt-dlp -U` |

## Security Notes

- `config.env` contains sensitive credentials. Keep it out of Git and restrict permissions.
- Use `chmod 640 /opt/twitch247/config/config.env`.
- Rotate any Twitch or Discord credential immediately if it was ever committed accidentally.
- If you expose the dashboard publicly, use a reverse proxy and HTTPS.
