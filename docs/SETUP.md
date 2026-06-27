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

```bash
git clone https://github.com/galitubereal/twitch247.git /tmp/twitch247
cd /tmp/twitch247
sudo bash scripts/install.sh
```

### 3. Configure

```bash
sudo nano /opt/twitch247/config/config.env
```

Required settings:

```env
TWITCH_STREAM_KEY=live_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWITCH_CHANNEL=galitubereal
YOUTUBE_CHANNEL_URL=https://youtube.com/@galitubereal/streams
```

### 4. Test Before Starting Services

```bash
yt-dlp --flat-playlist --print "%(id)s %(title)s" \
  "https://youtube.com/@galitubereal/streams" | head -5

sudo -u twitch247 /opt/twitch247/scripts/sync-youtube.sh
```

### 5. Start Services

```bash
sudo systemctl start twitch247
sudo systemctl start twitch247-dashboard
sudo systemctl start twitch247-watchdog.timer
```

### 6. Verify Stream

1. Check Twitch dashboard — stream should appear within 1–2 minutes
2. `curl http://127.0.0.1:8080/api/status`
3. `journalctl -u twitch247 -f`

## Updating yt-dlp

YouTube changes frequently — update yt-dlp weekly:

```bash
sudo yt-dlp -U
```
