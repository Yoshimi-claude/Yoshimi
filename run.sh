#!/bin/bash
# ツールを実行するためのスクリプト（例: ./run.sh --course 感染症学）
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "まだ準備ができていません。先に  bash setup.sh  を実行してください。"
  exit 1
fi
exec ./.venv/bin/python webclass_dl.py "$@"
