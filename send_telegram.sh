#!/bin/bash
# Notification bridge: lets PicoClaw send messages through Snappi's Telegram bot.
# Usage: send_telegram.sh "message text"
# Token and chat_id from Snappi's .env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "$SCRIPT_DIR/.env" 2>/dev/null || true
set +a
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
[ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ] && exit 1
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  -d "text=$1" \
  -d "parse_mode=HTML" > /dev/null
