# ミニマリスト・ニュース

世界と日本のミニマリストの発信を6時間ごとに集め、日本語の見出しと要約を付けて並べる静的サイト。

集めるのは2つの軸。

- **🍃 減らし方** — どうやって物を減らしているか。手放す基準、捨て方、考え方、失敗談。
- **🎒 持ち物** — 減らしたうえで何を持っているか。実際に使っている道具、買い直した物、持ち物リスト。

## 構成

```
collect.py          RSS収集 → LLMで日本語化・要約・分類 → docs/articles.json
feeds.json          収集元の一覧（enabled で個別にON/OFF）
llm_providers.py    Gemini → Groq → Claude → OpenAI の順に落ちるフォールバック
docs/index.html     依存ライブラリなしの1ファイル。PWA対応（iPhoneのホーム画面に追加できる）
docs/articles.json  収集結果。GitHub Actions が差分をコミットする
更新.command        ダブルクリックで手動更新して push する
```

`.github/workflows/update.yml` が cron `0 */6 * * *`（日本時間 3/9/15/21時）で実行する。
手動で回すときは `更新.command` か、Actions の workflow_dispatch。

## 収集元（2026-08-12 に全URL疎通確認）

| 種別 | 主な収集元 |
| --- | --- |
| 海外ブログ | The Minimalists / No Sidebar / Be More with Less / The Simplicity Habit / Nourishing Minimalism / Minimalism Made Simple / Simple Lionheart Life / Raptitude / Carryology |
| 国内ブログ | 筆子ジャーナル / ROOMIE / ライフハッカー・ジャパン / MonoMax |
| ニュース | Google ニュース（日本語4本・英語3本のAND検索） |
| YouTube | 海外5チャンネル・国内14チャンネル |
| Reddit | r/minimalism / r/simpleliving / r/BuyItForLife / r/onebag / r/declutter / r/Anticonsumption |

止まっている媒体は `enabled: false` と `_note` を付けて残してある（復活したら戻せる）。
Becoming Minimalist・Zen Habits・ミニマリストしぶのブログはRSSが凍結中。

## 決めごと

- 載せるのは**日本語の見出し・独自要約・出典名・原文リンクだけ**。全文翻訳はしない。
  原文の抜粋は `articles.json` にも残さない（`to_public()` が落としている）。
- 英語の記事も要約の段階で日本語にする。翻訳文をそのまま載せることはしない。
- LLMが `relevant: false` と判定した記事は捨てる。実測で新着の3割前後が落ちる。
- LLMが全滅したバッチは**保存せずに捨てる**。RSSには数日分残るので次の実行で拾い直される。

## 詰まりやすいところ

- **Googleニュースの検索で `OR` を使わない。** 語がばらけて占い・ゲーム攻略・新商品の記事が
  大量に流れ込み、要約の枠（1回40件）を食い潰す。スペース区切りのAND検索にする。
- **Redditは連続で叩くと429を返す。** `slow: true` を付けたフィードは12秒あけて順番に取り、
  失敗しても次の実行に回す（実測で毎回2〜4本は落ちる）。GitHub Actions からは
  さらに通りにくいので、Redditの記事が数日出てこなくても異常ではない。
- **YouTubeの概要欄は大半がスポンサー・SNS誘導・使用機材の一覧。** そのまま要約に渡すと
  「無料体験はこちら」のような要約ができる。`clean_youtube_description()` で行ごと落としたうえ、
  プロンプトでも「宣伝は無視する」と明示している。
- **YouTubeのサムネイルは `<media:group>` の中**にあるので、`iter()` で入れ子ごと辿る。
- **Redditの画像投稿は `<img>` を持たない。** 本文の `[link]` のリンク先が i.redd.it などの
  画像URLになっているので、そこから拾う（`reddit_image()`）。
- **世の中の発信は「減らす話」に偏っている。** 放っておくと持ち物タブが埋まらないので、
  `OWN_QUOTA` で1回40件のうち12件を持ち物寄りの収集元に先取りさせている。

## APIキー

`.env`（ローカル）と GitHub Secrets（Actions）に置く。1つでも通れば動く。

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```
