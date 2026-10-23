#!/bin/bash
# ダブルクリックで記事を手動更新し、GitHubへ反映する。
cd "$(dirname "$0")" || exit 1
echo "=== ミニマリスト・ニュース 手動更新 ==="
python3 collect.py || { echo "収集に失敗しました"; read -r -p "Enterで閉じる"; exit 1; }
if git diff --quiet docs/articles.json; then
  echo "新着はありませんでした。"
else
  git add docs/articles.json
  git commit -m "記事の手動更新 $(date '+%Y-%m-%d %H:%M')" && git push && echo "公開サイトに反映しました。"
fi
echo
echo "=== YouTube動画の要約（字幕から） ==="
/opt/homebrew/bin/python3 ../news_yt_summary/run.py --site ミニマリスト || echo "動画の要約に失敗しました（記事の更新は済んでいます）"
read -r -p "Enterで閉じる"
