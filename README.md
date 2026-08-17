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

## 収集元（2026-08-12 / 2026-08-17 に全URL疎通確認）

| 種別 | 主な収集元 |
| --- | --- |
| 海外の個人ブログ | The Minimalists / No Sidebar / Be More with Less / The Simplicity Habit / Nourishing Minimalism / Minimalism Made Simple / Simple Lionheart Life / Raptitude / Carryology / A Slob Comes Clean / Balance Through Simplicity / Simply + Fiercely / Exile Lifestyle |
| 国内の個人 | 筆子ジャーナル / note の #ミニマリスト #断捨離 #持たない暮らし #少ない服で暮らす |
| 国内メディア | ROOMIE / ライフハッカー・ジャパン / MonoMax |
| ニュース | Google ニュース（日本語4本・英語3本のAND検索） |
| YouTube | 海外5チャンネル・国内14チャンネル |
| X | The Minimalists / Joshua Becker / Zen Habits / ミニマリストしぶ（nitter 経由） |

止まっている媒体は `enabled: false` と `_note` を付けて残してある（復活したら戻せる）。
Becoming Minimalist・Zen Habits・ミニマリストしぶのブログはRSSが凍結中だが、
**書き手本人のXは動いている**ので、そちら（Joshua Becker / Zen Habits / ミニマリストしぶ）で拾えている。

## 決めごと

- 情報源は個人の発信を主にする。掲示板（Reddit）は信頼できる出どころとは言えないので使わない。
- 海外の記事は「🌐 日本語で読む」で Google 翻訳（translate.goog）を通したページを開く。
  URLの組み立てはブラウザ側だけで行い、収集時にGoogleへは問い合わせない。
  Cloudflare を使っている媒体は翻訳を通せないので、原文リンクを必ず併記する。
- 載せるのは**日本語の見出し・独自要約・出典名・原文リンクだけ**。全文翻訳はしない。
  原文の抜粋は `articles.json` にも残さない（`to_public()` が落としている）。
- 英語の記事も要約の段階で日本語にする。翻訳文をそのまま載せることはしない。
- LLMが `relevant: false` と判定した記事は捨てる。実測で新着の3割前後が落ちる。
- LLMが全滅したバッチは**保存せずに捨てる**。RSSには数日分残るので次の実行で拾い直される。

## 詰まりやすいところ

- **Googleニュースの検索で `OR` を使わない。** 語がばらけて占い・ゲーム攻略・新商品の記事が
  大量に流れ込み、要約の枠（1回40件）を食い潰す。スペース区切りのAND検索にする。
- **X は公式のRSSが無い。** 使えるのは nitter だけで、しかも生きている実装は少ない
  （2026-08-17時点で xcancel は400、nitter.poast / nitter.space / lightbrd は403、
  RSSHub は404。動いたのは `nitter.net` のみ）。`slow: true` で12秒あけて順番に取る。
  取り込むときに **リンクを x.com に、画像を pbs.twimg.com に直す**（nitter のURLのままだと
  読む人を nitter に送ってしまい、画像も表示できない）。リツイートと返信は見出しの
  `RT by @…` / `R to @…` で判別して捨てる。
  なお nitter で取れるアカウントは限られる（Matt D'Avella・The Minimal Mom は404）。
- **X の投稿は間隔が空くので `intake_days` を21日にしている。** 既定の4日のままだと、
  取得はできているのに1件も載らない（実際にこれで最初は0件だった）。
  フィードごとに `intake_days` を書けば取り込み期間を変えられる。
- **YouTubeの概要欄は大半がスポンサー・SNS誘導・使用機材の一覧。** そのまま要約に渡すと
  「無料体験はこちら」のような要約ができる。`clean_youtube_description()` で行ごと落としたうえ、
  プロンプトでも「宣伝は無視する」と明示している。
- **YouTubeのサムネイルは `<media:group>` の中**にあるので、`iter()` で入れ子ごと辿る。
- **収集をやめた種別の記事は、既存分も落とす必要がある。** `articles.json` に残った記事は
  `media` が `MEDIA_KINDS` に無ければ次の実行で棚から下ろす。これが無いと、
  フィードを外しても最大45日ぶんの記事がサイトに残り続ける。
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
