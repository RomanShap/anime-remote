#!/usr/bin/env bash
# Запуск пульта на Linux / Steam Deck. Закрыть окно или Ctrl+C = выключить.
cd "$(dirname "$0")" || exit 1
exec python3 server.py "$@"
