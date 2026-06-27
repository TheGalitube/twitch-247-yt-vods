# Twitch247

Autonomous 24/7 Twitch channel system that streams VODs directly from your YouTube channel — no permanent downloads, full playback persistence across Twitch's 48-hour stream resets.

## Features

- **Automatic YouTube discovery** — syncs all streams from your channel tab
- **Direct streaming** — yt-dlp resolves URLs, ffmpeg pipes to Twitch RTMP
- **Continuous RTMP output** — one Twitch connection stays open while videos change
- **Fixed Twitch format** — every source is encoded as 1920x1080 at 60 FPS
- **Smart queue** — unplayed videos first, loops from oldest when all are done
- **48-hour restart survival** — saves position every 15s, resumes seamlessly after systemd restart
- **Auto-reconnect** — handles Twitch disconnects and network interruptions
- **SQLite state** — full playback history and position tracking
- **Web dashboard** — live status, queue, uptime, errors
- **Discord notifications** — optional webhooks for stream events
- **systemd integration** — auto-start on boot, crash recovery, watchdog timer

## Architecture

```
YouTube Channel                    Twitch
     │                                ▲
     │  yt-dlp (URL only)             │ RTMP
     ▼                                │
┌─────────────┐    ffmpeg encode   ┌──┴──┐
│  VOD Queue  │ ─────────────────► │ RTMP│
│  (SQLite)   │                    └─────┘
└─────────────┘
     ▲
     │ position save (15s)
     │
┌─────────────┐     ┌──────────────┐
│  Main App   │────►│  Dashboard   │
│  (systemd)  │     │  (Flask)     │
└─────────────┘     └──────────────┘
     ▲
     │ health check (30min)
┌─────────────┐
│  Watchdog   │
└─────────────┘
```

## Requirements

- Debian 12+ or Ubuntu 22.04+
- Python 3.12+
- ffmpeg
- yt-dlp
- SQLite3
- 2+ CPU cores, 2GB+ RAM recommended
- Stable uplink (≥5 Mbps for 720p/1080p)

## Quick Install

```bash
# Clone or copy project to your server
git clone https://github.com/galitubereal/twitch247.git
cd twitch247

# Run installer as root
sudo bash scripts/install.sh

# Configure
sudo nano /opt/twitch247/config/config.env
```

Set at minimum:

```env
TWITCH_STREAM_KEY=live_xxxxxxxxxxxx
TWITCH_CHANNEL=yourchannel
YOUTUBE_CHANNEL_URL=https://youtube.com/@galitubereal/streams
```

Start services:

```bash
sudo systemctl start twitch247
sudo systemctl start twitch247-dashboard
sudo systemctl start twitch247-watchdog.timer
```

## Project Structure

```
/opt/twitch247/
├── app/                    # Core streaming application
│   ├── main.py             # Main orchestration loop
│   ├── streamer.py         # FFmpeg + yt-dlp streaming
│   ├── youtube_sync.py     # Channel discovery
│   ├── database.py         # SQLite operations
│   ├── config.py           # Configuration loader
│   ├── notifications.py  # Discord webhooks
│   └── logging_setup.py  # Rotating logs
├── dashboard/              # Flask monitoring UI
│   └── app.py
├── database/
│   └── schema.sql          # SQLite schema
├── config/
│   └── config.env          # Runtime configuration
├── logs/                   # Application logs
├── scripts/
│   ├── install.sh          # Installation script
│   ├── watchdog.sh         # Health check + 48h restart
│   └── sync-youtube.sh     # Manual YouTube sync
├── services/               # systemd unit files
│   ├── twitch247.service
│   ├── twitch247-dashboard.service
│   ├── twitch247-watchdog.service
│   └── twitch247-watchdog.timer
└── venv/                   # Python virtual environment
```

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TWITCH_STREAM_KEY` | — | Twitch RTMP stream key (required) |
| `TWITCH_CHANNEL` | — | Twitch channel name |
| `YOUTUBE_CHANNEL_URL` | — | YouTube streams tab URL |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `SAVE_INTERVAL` | `15` | Position save interval (seconds) |
| `SYNC_INTERVAL` | `3600` | YouTube sync interval (seconds) |
| `YOUTUBE_METADATA_WORKERS` | `4` | Parallel yt-dlp metadata lookups for new/incomplete videos |
| `YOUTUBE_SYNC_LIMIT` | `15` | Number of latest streams to keep in the local queue |
| `SEEK_TOLERANCE` | `5` | Seek back on restart (HLS tolerance) |
| `VIDEO_BITRATE` | `6000k` | Video bitrate for the 1080p60 Twitch stream |
| `MAXRATE` | `6000k` | Maximum video bitrate |
| `BUFSIZE` | `12000k` | Encoder rate-control buffer |
| `AUDIO_BITRATE` | `160k` | AAC audio bitrate |
| `DISCORD_WEBHOOK_URL` | — | Optional Discord notifications |
| `DASHBOARD_PORT` | `8080` | Web dashboard port |

## Monitoring

### Web Dashboard

Open `http://your-server:8080` for live status.

JSON API: `GET /api/status`

### Logs

```bash
# Main application
tail -f /opt/twitch247/logs/twitch247.log

# Playback events
tail -f /opt/twitch247/logs/playback.log

# Errors only
tail -f /opt/twitch247/logs/error.log

# systemd journal
journalctl -u twitch247 -f
```

### Manual YouTube Sync

```bash
sudo -u twitch247 /opt/twitch247/scripts/sync-youtube.sh
```

## 48-Hour Twitch Restart

Twitch requires streams to restart every ~48 hours. Twitch247 handles this automatically:

1. **Position persistence** — current video ID and timestamp saved every 15 seconds to SQLite
2. **Watchdog timer** — runs every 30 minutes, proactively restarts at 47 hours
3. **Seamless resume** — on restart, loads saved video + position (minus 5s HLS tolerance)
4. **Viewer experience** — brief interruption (~5–10s), then continues from saved position

## Playback Queue Logic

1. Prefer videos with `played_status = unplayed`
2. Order by `upload_date` ascending (oldest first)
3. When a video finishes → mark as `played`, start next
4. When all videos are `played` → reset all to `unplayed`, loop from oldest

## Troubleshooting

| Problem | Solution |
|---|---|
| No videos found | Run `sync-youtube.sh`, check `YOUTUBE_CHANNEL_URL` |
| FFmpeg errors | Verify ffmpeg installed: `ffmpeg -version` |
| yt-dlp errors | Update: `sudo yt-dlp -U` |
| Stream key invalid | Check `TWITCH_STREAM_KEY` in config.env |
| Dashboard unreachable | `systemctl status twitch247-dashboard` |
| Permission denied | `chown -R twitch247:twitch247 /opt/twitch247` |

## Security Notes

- `config.env` contains your stream key — restrict permissions (`chmod 640`)
- Dashboard binds to `127.0.0.1` by default — use a reverse proxy for remote access
- Service runs as unprivileged `twitch247` user
- systemd units use `ProtectSystem=strict`

## License

MIT
