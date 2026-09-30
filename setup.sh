#!/bin/bash
# 最初に 1 回だけ実行する準備用のスクリプト
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。README.md の「準備」を見てください。"
  exit 1
fi

echo "1/3 このツール専用の Python 環境（.venv フォルダ）を作っています..."
python3 -m venv .venv

echo "2/3 Playwright（ブラウザを動かす部品）をインストールしています..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

echo "3/3 ツール専用のブラウザ（Chromium）をダウンロードしています..."
./.venv/bin/python -m playwright install chromium

chmod +x run.sh
echo ""
echo "準備が終わりました。次のコマンドで実行できます:"
echo "  ./run.sh --course 感染症学"
