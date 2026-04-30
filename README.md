# xaubot

An AI-powered XAUUSD (gold/USD) signal bot. It fetches live candle data
from Twelve Data, computes technical indicators (RSI, EMA, MACD, ATR)
on the 15-minute and 1-hour timeframes, asks Claude to act as a
disciplined analyst and produce a structured BUY/SELL/WAIT signal, runs
session/confidence/duplicate filters over the result, and pushes
approved signals to a Telegram chat. APScheduler drives the cycle every
15 minutes and the process is designed to run 24/7 as a systemd service
on an Oracle Cloud Free Tier Ubuntu 22.04 ARM VM.

## Prerequisites

- Ubuntu 22.04 (ARM or x86_64) with Python 3.11
- A Twelve Data API key (free tier is fine for 15-min cadence)
- An Anthropic API key with access to `claude-opus-4-5`
- A Telegram bot token (from @BotFather) and a chat ID to receive signals
- `git`, `python3-venv`, and `python3-pip` installed:
  `sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip git`

## Setup

1. **Copy the project to the VM**

   ```bash
   mkdir -p ~/xaubot
   # rsync / scp / git clone the files in this repo into ~/xaubot/
   cd ~/xaubot
   ```

2. **Create a virtualenv and install dependencies**

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   ```bash
   cp .env.example .env
   nano .env   # fill in real keys
   ```

4. **Test run**

   ```bash
   source .venv/bin/activate
   python main.py
   ```

   You should see a Telegram startup alert immediately and the bot will
   run an initial cycle, then idle until the next quarter-hour.

5. **Deploy as a systemd service**

   Create `/etc/systemd/system/xaubot.service`:

   ```ini
   [Unit]
   Description=xaubot XAUUSD Claude signal bot
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/xaubot
   EnvironmentFile=/home/ubuntu/xaubot/.env
   ExecStart=/home/ubuntu/xaubot/.venv/bin/python /home/ubuntu/xaubot/main.py
   Restart=on-failure
   RestartSec=10
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

   Then enable and start it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now xaubot
   sudo systemctl status xaubot
   ```

## Weekly shadow-mode report

`weekly_report.py` reads the last 7 days of `signals.log`, summarises
distribution by direction / confidence bucket / filter outcome, and
posts the result to Telegram. Schedule it as a separate systemd timer:

`/etc/systemd/system/xaubot-weekly.service`:

```ini
[Unit]
Description=xaubot weekly shadow-mode report
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/xaubot
EnvironmentFile=/home/ubuntu/xaubot/.env
ExecStart=/home/ubuntu/xaubot/.venv/bin/python /home/ubuntu/xaubot/weekly_report.py
```

`/etc/systemd/system/xaubot-weekly.timer`:

```ini
[Unit]
Description=Run xaubot weekly report every Sunday 21:00 UTC

[Timer]
OnCalendar=Sun 21:00:00 UTC
Persistent=true
Unit=xaubot-weekly.service

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xaubot-weekly.timer
systemctl list-timers xaubot-weekly.timer
```

## Viewing logs

```bash
journalctl -u xaubot -f          # live tail of stdout/stderr
tail -f ~/xaubot/xaubot.log      # rotating file log
```

## Troubleshooting

- **`FATAL: required environment variable '...' is missing`** — you
  forgot to copy `.env.example` to `.env` or left a key blank.
- **Twelve Data 429 rate limit** — free tier allows 8 req/min; the bot
  paces itself but if you run it concurrently with other tools the
  cycle will be skipped. Wait a minute and check the next run.
- **`Anthropic API call failed`** — check that the API key is valid and
  the account has access to `claude-opus-4-5`. Network issues on the
  VM also surface here.
- **No Telegram messages arriving** — confirm the chat ID by sending a
  message to your bot and visiting
  `https://api.telegram.org/bot<TOKEN>/getUpdates`. The chat ID may be
  negative for groups.
- **`pandas-ta` import errors on ARM** — install build deps:
  `sudo apt install -y build-essential` and reinstall.
- **Bot keeps emitting WAIT** — that's normal during low-volatility
  periods or when M15/H1 disagree. Check `xaubot.log` for the analyst
  reasoning.
