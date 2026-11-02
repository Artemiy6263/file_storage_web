#!/usr/bin/env bash
# Запуск веб-интерфейса на Linux/macOS. Один раз: pip install -r requirements.txt
# Чтобы открывать сайт с других компьютеров: WEB_HOST=0.0.0.0 ./run.sh
cd "$(dirname "$0")"
export WEB_HOST="${WEB_HOST:-127.0.0.1}" WEB_PORT="${WEB_PORT:-8000}"
exec python3 app.py
