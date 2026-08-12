#!/usr/bin/env python3
"""ミニマリズムの話題を海外・国内から集め、日本語の見出しと要約を付けて docs/articles.json に書き出す。

集めるのは2つの軸。
  reduce = どうやって物を減らしているか（手放し方・考え方）
  own    = 減らしたうえで何を持っているか（実際に使っている道具）

原文の本文はそのまま載せない。独自の短い要約・出典名・原文へのリンクだけを持たせる
（引用の範囲に収めるため）。英語の記事は要約の段階で日本語にする。

GitHub Actions から6時間ごとに実行され、差分がコミットされると
GitHub Pages 側のサイトが更新される。ローカルでも同じスクリプトが動く。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import llm_providers  # noqa: E402  （.env 読み込みより前でよい。キーは呼び出し時に参照される）

FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "articles.json")

USER_AGENT = "minimalist-news/1.0 (+https://github.com/mifune39428)"
FETCH_TIMEOUT = 25

# 新しく取り込む記事の対象期間。ブログは毎日は更新されないので少し広めに取る。
INTAKE_DAYS = 4
# サイトに残す期間と件数の上限。ニュースと違って古びにくい話題なので長めに残す。
KEEP_DAYS = 45
KEEP_MAX = 400
# 1回のLLM呼び出しでまとめて処理する記事数。
# Groqの無料枠は分あたりトークン数（TPM 6000）が厳しいので、大きくし過ぎない。
BATCH_SIZE = 5
# 1回の実行で要約する上限。無料枠の1日あたり回数を使い切らないための蓋。
# 溢れた分は次の実行（6時間後）に回る。
MAX_NEW_PER_RUN = 40
# そのうち「持ち物」寄りの収集元のために空けておく枠。
# 世の中の発信は減らす話に偏っていて、放っておくと「持ち物」タブが埋まらない。
OWN_QUOTA = 12
# 1回の実行で、過去の記事のサムネイルを取りに行く件数の上限。
BACKFILL_PER_RUN = 30
# Reddit は連続で叩くと 429 を返す。slow 指定のフィードはこの間隔をあけて順番に取る。
SLOW_FEED_INTERVAL = 12
SLOW_FEED_RETRIES = 2

# 記事の2軸。サイト上部のタブに対応する。
AXES = ["reduce", "own"]

CATEGORIES = [
    "手放す・捨て方",
    "部屋・収納",
    "服・ファッション",
    "道具・ガジェット",
    "キッチン・食",
    "お金・買い方",
    "旅・持ち歩き",
    "心・習慣",
    "暮らしの実例",
    "その他",
]

MEDIA_KINDS = ["blog", "news", "youtube", "reddit"]
REGIONS = ["国内", "海外"]

# Googleニュース経由で紛れ込む、この話題と縁のない出典。部分一致で落とす。
# ゲーム攻略と占いは「持ち物」「捨てる」という語に反応して繰り返し湧いてくる。
BLOCK_SOURCES = ["PR TIMES", "prtimes", "GameWith", "Game8", "アルテマ", "神ゲー攻略"]

# Googleニュースの <source> がドメインのまま入ってくる媒体を、読める名前に直す。
DOMAIN_NAMES = {
    "news.yahoo.co.jp": "Yahoo!ニュース",
    "www3.nhk.or.jp": "NHK",
    "www.nhk.or.jp": "NHK",
    "news.ntv.co.jp": "日テレNEWS",
}

JST = dt.timezone(dt.timedelta(hours=9))

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
RSS10 = "{http://purl.org/rss/1.0/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


# --------------------------------------------------------------------------
# 下ごしらえ
# --------------------------------------------------------------------------

def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入るので不要）。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """トラッキング用のクエリを落として、同じ記事が別URLに見えないようにする。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "at_"))
    ]
    # Google ニュースと YouTube はクエリに記事・動画のIDが載るので触らない。
    if parts.netloc in ("news.google.com", "www.youtube.com", "youtube.com"):
        query = urllib.parse.parse_qsl(parts.query)
    cleaned = parts._replace(query=urllib.parse.urlencode(query), fragment="")
    return urllib.parse.urlunsplit(cleaned).rstrip("/")


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def parse_date(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# RSS / Atom / RDF の取得
# --------------------------------------------------------------------------

def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None:
            if found.text:
                return found.text
            # Atom の <link href="..."> のように属性側に入っている場合。
            href = found.get("href")
            if href:
                return href
    return ""


# 記事のサムネイルとして使わない画像（配信計測用の透明画像やアイコンなど）。
IMAGE_BLOCKLIST = ("feedburner", "gravatar", "/pixel", "1x1", "blank.gif", "spacer",
                   "doubleclick", "/award_", "redditstatic")
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']'
    r'[^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
    re.I,
)


def usable_image(url: str, base: str) -> str:
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    url = urllib.parse.urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return ""
    if any(word in url.lower() for word in IMAGE_BLOCKLIST):
        return ""
    return url


def image_from_entry(entry, base: str) -> str:
    """RSSの中に入っている画像を探す。媒体ごとに置き場所が違うので順に当たる。"""
    # YouTube はサムネイルを <media:group> の中に入れるので、入れ子ごと辿る。
    for node in list(entry.iter(f"{MEDIA}thumbnail")) + list(entry.iter(f"{MEDIA}content")):
        medium = (node.get("medium") or node.get("type") or "").lower()
        if medium and "image" not in medium:
            continue
        found = usable_image(node.get("url", ""), base)
        if found:
            return found

    for node in entry.findall("enclosure") + entry.findall(f"{ATOM}link"):
        if "image" in (node.get("type") or "").lower():
            found = usable_image(node.get("url") or node.get("href") or "", base)
            if found:
                return found

    # 本文HTMLの最初の <img>。多くの媒体はここにアイキャッチが入っている。
    raw_body = " ".join(
        node.text or ""
        for tag in ("description", f"{CONTENT}encoded", f"{RSS10}description",
                    f"{ATOM}summary", f"{ATOM}content")
        for node in entry.findall(tag)
    )
    for candidate in IMG_TAG_RE.findall(raw_body):
        found = usable_image(candidate, base)
        if found:
            return found
    return ""


# --------------------------------------------------------------------------
# Google ニュースのリンクを元媒体のURLに戻す
# --------------------------------------------------------------------------

GOOGLE_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')


def resolve_google_url(url: str) -> str:
    """news.google.com の転送URLから、元媒体の記事URLを取り出す。

    転送ページはJavaScriptで飛ぶ作りなので、HTTPを追うだけでは元URLが分からない。
    ページに埋まっている署名（sg）と時刻（ts）を Google の batchexecute に投げると
    元URLが返る。取れなければ転送URLのまま使う（リンクとしては機能する）。
    """
    if "news.google.com" not in url:
        return url
    try:
        gid = url.split("/articles/")[1].split("?")[0]
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            # 署名はページのかなり後ろに入っているので、途中で切らずに全部読む。
            page = response.read().decode("utf-8", errors="ignore")
        signature, timestamp = SIGNATURE_RE.search(page), TIMESTAMP_RE.search(page)
        if not signature or not timestamp:
            return url

        payload = [[
            "Fbv4je",
            json.dumps([
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                  None, None, None, None, None, 0, 1],
                 "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                gid, int(timestamp.group(1)), signature.group(1),
            ]),
            None, "1",
        ]]
        data = urllib.parse.urlencode({"f.req": json.dumps([payload])}).encode()
        request = urllib.request.Request(
            GOOGLE_BATCH_URL,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001  取れなくても転送URLで記事は読める
        return url
    return parse_garturlres(body) or url


def parse_garturlres(body: str) -> str:
    """batchexecute の返事から元URLを取り出す。

    返事は `[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://…\\",1]",…]]` の形で、
    URLは二重にJSONエスケープされている。素直に2段階で読む。
    """
    for line in body.splitlines():
        if "garturlres" not in line:
            continue
        try:
            for part in json.loads(line):
                if isinstance(part, list) and len(part) > 2 and part[0] == "wrb.fr":
                    inner = json.loads(part[2])
                    if len(inner) > 1 and str(inner[1]).startswith("http"):
                        return canonical_url(inner[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return ""


def resolve_google_urls(items: list[dict]) -> None:
    targets = [item for item in items if "news.google.com" in item["url"]]
    if not targets:
        return
    print(f"  Googleニュースのリンク {len(targets)}件を元媒体のURLに変換中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, resolved in zip(targets, pool.map(lambda i: resolve_google_url(i["url"]), targets)):
            item["url"] = resolved
    remaining = sum(1 for item in targets if "news.google.com" in item["url"])
    print(f"  変換できたもの {len(targets) - remaining}件")


def fetch_og_image(url: str) -> str:
    """RSSに画像が無い記事は、元ページの og:image を見に行く。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=12) as response:
            head = response.read(200_000).decode("utf-8", errors="ignore")
            final_url = response.geturl()
    except Exception:  # noqa: BLE001  取れなくても記事自体は載せる
        return ""
    match = OG_IMAGE_RE.search(head)
    if not match:
        return ""
    return usable_image(match.group(1) or match.group(2) or "", final_url)


def fill_missing_images(items: list[dict], limit: int = 0) -> None:
    """画像がまだ無い記事について、元ページの og:image を取りに行く。

    limit を渡すと1回に取りに行く件数を抑える（既存記事の穴埋め用）。
    """
    targets = [item for item in items if not item.get("image")]
    if limit:
        targets = targets[:limit]
    if not targets:
        return
    print(f"  サムネイル未取得 {len(targets)}件をページから取得中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, image in zip(targets, pool.map(lambda i: fetch_og_image(i["url"]), targets)):
            item["image"] = image
    print(f"  取得できたもの {sum(1 for item in targets if item['image'])}件")


# YouTubeの概要欄で、動画の中身ではなく宣伝・定型文が書かれている行の目印。
# 行ごと落とす（Matt D'Avella のように1行目がスポンサー読み上げの動画があるため）。
PROMO_MARKERS = (
    # 英語圏
    "sponsor", "free trial", "discount", "% off", "promo code", "coupon", "$",
    "affiliate", "commission", "subscribe", "my gear", "gear i use", "follow me",
    "instagram", "twitter", "tiktok", "patreon", "newsletter", "join the",
    "shop ", "use code", "check out", "link below", "watch more", "click here",
    "membership", "/month", "sign up", "i'd love to help", "free guide",
    "download", "my course", "my book", "0:00", "chapters", "@",
    # 日本語圏
    "チャンネル登録", "使用機材", "お仕事", "ご依頼", "お問い合わせ", "楽天",
    "アフィリエイト", "提供", "案件", "bgm", "音源", "素材", "サブチャンネル",
    "メンバーシップ", "インスタ", "目次", "こちら", "リンク", "キャンペーン",
    "無料", "限定", "クーポン", "ダウンロード", "公式サイト", "円off", "プレゼント",
    "詳細は", "応募", "当たる",
)


def clean_youtube_description(raw: str) -> str:
    """YouTubeの概要欄から、動画の中身を語っている行だけを残す。

    概要欄はスポンサー読み上げ・SNS誘導・使用機材・目次で大半が埋まる。
    そのまま要約に渡すと「無料トライアルはこちら」のような要約が出来上がるので、
    行単位で落としてから渡す。
    """
    text = html.unescape(re.sub(r"(?s)<br\s*/?>", "\n", raw or ""))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    kept = []
    for line in text.splitlines():
        line = re.sub(r"https?://\S+", " ", line)
        line = re.sub(r"\s+", " ", line).strip(" 　-–—=*・▼▶︎▶#")
        if len(line) < 8:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in PROMO_MARKERS):
            continue
        # 「00:35 導入」のような目次行。
        if re.match(r"^\d{1,2}:\d{2}", line):
            continue
        # 記号や絵文字ばかりの飾り行。
        if len(re.sub(r"[\wぁ-んァ-ヶ一-龥]", "", line)) > len(line) * 0.5:
            continue
        kept.append(line)
        if sum(len(k) for k in kept) > 400:
            break
    return " ".join(kept).strip()


# Redditの本文末に必ず付く定型（投稿者名とリンク）。要約の材料にならないので落とす。
REDDIT_BOILERPLATE_RE = re.compile(
    r"\s*submitted by\s*/u/\S+\s*(\[link\])?\s*(\[comments\])?\s*$", re.I)
# Redditの画像投稿は、本文に <img> ではなく [link] のリンク先として画像URLが入る。
REDDIT_IMAGE_RE = re.compile(
    r'href="(https://(?:i|preview)\.redd\.it/[^"]+|https://i\.imgur\.com/[^"]+)"', re.I)


def entry_body(entry, media: str) -> str:
    """要約の材料になる本文を取り出す。"""
    if media == "youtube":
        # YouTube の説明文は <media:group> の中にある。
        node = entry.find(f"{MEDIA}group/{MEDIA}description")
        if node is not None and node.text:
            return clean_youtube_description(node.text)

    raw = _text(
        entry,
        "description",
        f"{CONTENT}encoded",
        f"{RSS10}description",
        f"{ATOM}summary",
        f"{ATOM}content",
    )
    body = strip_html(raw)
    if media == "reddit":
        body = REDDIT_BOILERPLATE_RE.sub("", body).strip()
    return body


def reddit_image(entry) -> str:
    """Redditの投稿から画像を拾う。本文の [link] が画像を指していればそれを使う。"""
    raw = " ".join(
        node.text or "" for node in entry.findall(f"{ATOM}content") + entry.findall(f"{ATOM}summary")
    )
    match = REDDIT_IMAGE_RE.search(html.unescape(raw))
    return usable_image(match.group(1), "https://www.reddit.com/") if match else ""


def fetch_feed(feed: dict) -> list[dict]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()

    root = ET.fromstring(raw)
    entries = (
        root.findall(".//item")
        or root.findall(f".//{RSS10}item")
        or root.findall(f".//{ATOM}entry")
    )

    media = feed.get("media", "blog")
    items = []
    for entry in entries:
        title = strip_html(_text(entry, "title", f"{ATOM}title", f"{RSS10}title"))
        link = _text(entry, "link", f"{RSS10}link", f"{ATOM}link").strip()
        if not link:
            # Atom は複数の <link> を持つので rel="alternate" を拾う。
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break
        if not title or not link:
            continue

        source = feed["name"]
        if feed.get("google_news"):
            # Google ニュースの見出しは「本文の見出し - 媒体名」の形。
            # 媒体名は <source> にも入っているので、そちらを出典として使う。
            actual = strip_html(_text(entry, "source"))
            if actual:
                source = DOMAIN_NAMES.get(actual, actual)
                if title.endswith(f" - {actual}"):
                    title = title[: -len(actual) - 3].strip()
            else:
                title = re.sub(r"\s+-\s+[^-]{2,30}$", "", title).strip()
        if any(blocked.lower() in source.lower() for blocked in BLOCK_SOURCES):
            continue

        published = parse_date(
            _text(entry, "pubDate", f"{DC}date", f"{ATOM}published", f"{ATOM}updated", "date")
        )
        body = entry_body(entry, media)
        # 見出しがハッシュタグ並びで説明も無い動画はショート。中身を知る手がかりが無く、
        # 「暮らしを整える様子を紹介」のような当たり障りのない要約にしかならないので取らない。
        if media == "youtube" and not body and len(re.findall(r"#\S+", title)) >= 2:
            continue
        # Google ニュースの description は他媒体へのリンク集なので要約の材料にならない。
        if feed.get("google_news"):
            body = ""

        image = image_from_entry(entry, link)
        if not image and media == "reddit":
            image = reddit_image(entry)

        items.append(
            {
                "id": article_id(link),
                "url": canonical_url(link),
                "title_original": title,
                "excerpt": body[:800],
                "source": source,
                "image": image,
                "media": media if media in MEDIA_KINDS else "blog",
                "region": feed.get("region", "海外"),
                "hint": feed.get("hint", ""),
                "published": (published or dt.datetime.now(dt.timezone.utc)).isoformat(),
            }
        )

    limit = int(feed.get("max_items", 0) or 0)
    if limit:
        items.sort(key=lambda item: item["published"], reverse=True)
        items = items[:limit]
    return items


def collect_feed_items(feeds: list[dict]) -> list[dict]:
    """通常のフィードは並列で、slow 指定のものは間隔をあけて順番に取る。"""
    collected: list[dict] = []
    quick = [feed for feed in feeds if not feed.get("slow")]
    slow = [feed for feed in feeds if feed.get("slow")]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, feed): feed for feed in quick}
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                items = future.result()
            except Exception as exc:  # 1本落ちても全体は続ける
                print(f"  × {feed['name']}: {type(exc).__name__}: {exc}")
                continue
            print(f"  ○ {feed['name']}: {len(items)}件")
            collected.extend(items)

    if slow:
        print(f"  Reddit など {len(slow)}本は{SLOW_FEED_INTERVAL}秒あけて順番に取得します"
              f"（レート制限のため一部は落ちます）")
    for index, feed in enumerate(slow):
        if index:
            time.sleep(SLOW_FEED_INTERVAL)
        for attempt in range(SLOW_FEED_RETRIES + 1):
            try:
                items = fetch_feed(feed)
            except Exception as exc:  # noqa: BLE001
                if attempt < SLOW_FEED_RETRIES:
                    time.sleep(SLOW_FEED_INTERVAL)
                    continue
                # 429 は日常茶飯事。落ちた分は次の実行で拾い直せばよい。
                print(f"  × {feed['name']}: {type(exc).__name__}: {exc}")
                break
            print(f"  ○ {feed['name']}: {len(items)}件")
            collected.extend(items)
            break
    return collected


# --------------------------------------------------------------------------
# 重複の除去
# --------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = re.sub(r"[\s　]+", "", title.lower())
    return re.sub(r"[!-/:-@\[-`{-~、。「」・…—–\-]", "", title)


TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[0-9]+|[ァ-ヶー]{2,}|[一-龥]{2,}")


def title_tokens(title: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(title)}


def same_story(a: dict, b: dict) -> bool:
    """日本語化した見出しで、同じ話題かどうかを見る。

    海外記事と国内記事は原題が別物なので、日本語の見出しになって初めて重なりが分かる。
    """
    published_a, published_b = parse_date(a["published"]), parse_date(b["published"])
    if published_a and published_b and abs((published_a - published_b).total_seconds()) > 36 * 3600:
        return False

    left, right = normalize_title(a["title_ja"]), normalize_title(b["title_ja"])
    if SequenceMatcher(None, left, right).ratio() >= 0.78:
        return True

    tokens_a, tokens_b = title_tokens(a["title_ja"]), title_tokens(b["title_ja"])
    if len(tokens_a) >= 3 and len(tokens_b) >= 3:
        if len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= 0.72:
            return True
    return False


def dedupe_stories(
    new_items: list[dict], existing_items: list[dict]
) -> tuple[list[dict], set[str]]:
    """同じ話題は1本に絞る。Googleニュース経由の転載より、元媒体の記事を優先する。

    掲載する新着と、入れ替えで取り下げる既存記事のIDを返す。
    """
    recent = existing_items[:120]
    ordered = sorted(new_items, key=lambda item: 1 if item["media"] == "news" else 0)
    kept: list[dict] = []
    replaced: set[str] = set()
    for item in ordered:
        older = next(
            (o for o in recent if o["id"] not in replaced and same_story(item, o)), None
        )
        if older is not None:
            # 集約サイト経由の短報を先に載せたあとに元媒体の記事が届いたら、そちらへ差し替える。
            if item["media"] != "news" and older.get("media") == "news":
                print(f"  ・元媒体に差し替え: {item['title_ja']}（{item['source']}）")
                replaced.add(older["id"])
                kept.append(item)
            else:
                print(f"  ・既出のため除外: {item['title_ja']}（{item['source']}）")
            continue
        if any(same_story(item, other) for other in kept):
            print(f"  ・重複のため除外: {item['title_ja']}（{item['source']}）")
            continue
        kept.append(item)
    return kept, replaced


def is_duplicate(title: str, known_titles: list[str]) -> bool:
    target = normalize_title(title)
    if not target:
        return False
    for known in known_titles:
        if not known:
            continue
        if target == known:
            return True
        if abs(len(target) - len(known)) <= max(6, len(target) * 0.3):
            if SequenceMatcher(None, target, known).ratio() >= 0.86:
                return True
    return False


def interleave_by_source(items: list[dict], limit: int) -> list[dict]:
    """出典ごとに1件ずつ順番に取って上限まで詰める。

    YouTube のように毎日出る媒体が枠を独占すると、週1更新のブログが
    いつまでも載らない。出典を回しながら取ることで、どの媒体も必ず1件は入る。
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["source"], []).append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: item["published"], reverse=True)

    picked: list[dict] = []
    while len(picked) < limit:
        added = False
        for bucket in buckets.values():
            if not bucket:
                continue
            picked.append(bucket.pop(0))
            added = True
            if len(picked) >= limit:
                break
        if not added:
            break
    return picked


# --------------------------------------------------------------------------
# 翻訳・要約・分類
# --------------------------------------------------------------------------

PROMPT_TEMPLATE = """あなたは「ミニマリスト」をテーマにした日本語のニュースサイトの編集者です。
海外・国内のブログ、ニュース、YouTube、Reddit から集めた記事を渡します。
日本語の読者向けに「短い見出し」と「要約」を作り、分類してください。

このサイトが読者に届けたいのは次の2つです。
  reduce（減らし方）… どうやって物を減らしたか。手放す基準、捨て方、考え方、失敗談。
  own（持ち物）    … 減らしたうえで何を持っているか。実際に使っている道具、買い直した物、持ち物リスト。

厳守すること:
- 原文をそのまま写さない。翻訳文をそのまま載せるのでもない。事実を踏まえて自分の言葉で短くまとめる。
- 英語の記事も、見出し・要約は必ず日本語で書く。
- 事実を足さない。抜粋に書かれていない数字・値段・商品名を創作しない。
  抜粋が無く見出しだけの場合は、見出しから確実に言えることだけを書く。
- YouTubeの抜粋（概要欄）には、動画の中身と関係のない宣伝が混ざっていることがある。
  スポンサー、割引、会員募集、SNSの案内、使用機材の一覧は無視して、
  動画の中身を語っている部分だけを使う。使える部分が無ければ見出しだけから書く。
  宣伝を要約してはいけない（「無料体験はこちら」のような要約は誤り）。
- 見出し(title_ja)は日本語で40文字以内。煽らず、内容が分かる形にする。
  「〜すべき」より「〜した人の話」のように、何が読めるのかが分かる書き方にする。
- 要約(summary_ja)は日本語で80〜140文字。1〜3文。
  できるだけ具体を残す（何を手放したか、何個にしたか、何を使っているか）。
- relevant: ミニマリズム・持ち物・暮らしを簡素にすることに関係する内容なら true。
  無関係な一般ニュース、広告・通販・セール告知・求人、まとめサイトのランキング、
  「ミニマル」を意匠の意味だけで使った記事（音楽・デザイン論など）は false。
- axis: reduce か own のどちらか1つ。
  持ち物の紹介・愛用品・買った物・ルームツアーでの所有物が中心なら own。
  手放し方・片づけ・考え方・暮らしの整え方が中心なら reduce。
  どちらとも取れる場合は、読者が「何を持っているか」を知れる度合いが高いほうを own にする。
- things: 記事に出てくる具体的な持ち物・製品・道具の名前を最大4つ、日本語の配列で。
  一般名詞でよい（例: 「洗濯機」「iPhone」「無印のトート」）。
  抜粋に出てこないなら空配列 []。憶測で商品名を作らない。
- category は次から必ず1つ選ぶ: {categories}
- importance は1〜5の整数。5=考え方が変わるような濃い内容、3=普通、1=軽い小ネタ。
- 出力はJSON配列のみ。前置き・説明・コードフェンスを付けない。

出力形式（要素数は入力と同じ{count}件、iは入力の番号）:
[{{"i":1,"relevant":true,"axis":"own","title_ja":"...","summary_ja":"...","things":["..."],"category":"道具・ガジェット","importance":3}}]

入力記事:
{articles}
"""

MEDIA_LABELS = {
    "blog": "ブログ記事",
    "news": "ニュース記事",
    "youtube": "YouTube動画（見出しは動画タイトル、抜粋は概要欄）",
    "reddit": "Redditの投稿（見出しは投稿タイトル、抜粋は本文）",
}


def build_prompt(batch: list[dict]) -> str:
    lines = []
    for index, item in enumerate(batch, start=1):
        lines.append(
            f"[{index}] 種別: {MEDIA_LABELS.get(item['media'], 'ブログ記事')} / "
            f"出典: {item['source']}（{item['region']}）\n"
            f"見出し: {item['title_original']}\n"
            f"抜粋: {item['excerpt'][:600] or '(抜粋なし)'}\n"
        )
    return PROMPT_TEMPLATE.format(
        categories=" / ".join(CATEGORIES),
        count=len(batch),
        articles="\n".join(lines),
    )


def parse_llm_json(text: str, expected: int) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise llm_providers.ResponseInvalid("空の配列です")
    if len(data) != expected:
        raise llm_providers.ResponseInvalid(f"{expected}件のはずが{len(data)}件です")
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("title_ja") or not entry.get("summary_ja"):
            raise llm_providers.ResponseInvalid("title_ja / summary_ja が欠けています")
    return data


def clean_things(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    things = []
    for value in raw:
        name = re.sub(r"\s+", " ", str(value)).strip(" 　・,、")
        if name and len(name) <= 24 and name not in things:
            things.append(name)
    return things[:4]


def enrich(items: list[dict]) -> list[dict]:
    """LLMで日本語の見出し・要約・分類を付ける。失敗した分は捨てて次回に回す。"""
    results: list[dict] = []
    for offset in range(0, len(items), BATCH_SIZE):
        batch = items[offset : offset + BATCH_SIZE]
        print(f"  要約 {offset + 1}〜{offset + len(batch)}件目 …")
        try:
            text = llm_providers.generate_text(
                build_prompt(batch),
                validate=lambda t, n=len(batch): parse_llm_json(t, n),
            )
            entries = parse_llm_json(text, len(batch))
        except llm_providers.LLMError as exc:
            # 生煮えの記事をサイトに出すより、今回は見送って次の実行で拾い直す。
            # RSSには数日分残っているので、枠が空けば自然に再挑戦される。
            print(f"  × 要約に失敗（この{len(batch)}件は次回に回します）: {exc}")
            continue

        by_index = {}
        for entry in entries:
            try:
                by_index[int(entry.get("i", 0))] = entry
            except (TypeError, ValueError):
                continue

        for index, item in enumerate(batch, start=1):
            entry = by_index.get(index) or entries[index - 1]
            if entry.get("relevant") is False:
                print(f"  ・ミニマリズムと無関係のため除外: {item['title_original'][:40]}")
                continue
            category = str(entry.get("category", "")).strip()
            axis = str(entry.get("axis", "")).strip()
            item["title_ja"] = str(entry["title_ja"]).strip()
            item["summary_ja"] = str(entry["summary_ja"]).strip()
            item["category"] = category if category in CATEGORIES else "その他"
            # フィードに hint がある媒体は、LLMが逆を言わない限りその軸に寄せる。
            if axis not in AXES:
                axis = item["hint"] if item["hint"] in AXES else "reduce"
            item["axis"] = axis
            item["things"] = clean_things(entry.get("things"))
            try:
                item["importance"] = max(1, min(5, int(entry.get("importance", 3))))
            except (TypeError, ValueError):
                item["importance"] = 3
            results.append(item)
    return results


def to_public(item: dict) -> dict:
    """サイトに出す形に整える。原文の抜粋は公開データに残さない。"""
    return {key: value for key, value in item.items() if key not in ("excerpt", "hint")}


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

def load_existing() -> dict:
    if not os.path.exists(OUTPUT_PATH):
        return {"updated_at": None, "items": []}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "items": []}
    data.setdefault("items", [])
    return data


def main() -> int:
    load_env()

    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = [feed for feed in json.load(f)["feeds"] if feed.get("enabled", True)]

    print(f"■ フィード取得（{len(feeds)}本）")
    fetched = collect_feed_items(feeds)
    print(f"  合計 {len(fetched)}件")

    existing = load_existing()
    existing_items = existing["items"]
    known_ids = {item["id"] for item in existing_items}
    known_urls = {canonical_url(item["url"]) for item in existing_items}
    known_titles = [normalize_title(item.get("title_original", "")) for item in existing_items]

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=INTAKE_DAYS)

    # 元媒体を先に見ることで、同じ記事がGoogleニュース経由でも流れてきたときに
    # 元の媒体のURLのほうを残す。
    fetched.sort(key=lambda item: item["published"], reverse=True)
    fetched.sort(key=lambda item: 1 if item["media"] == "news" else 0)  # 安定ソート

    new_items: list[dict] = []
    for item in fetched:
        published = parse_date(item["published"])
        if published is None or published < cutoff or published > now + dt.timedelta(hours=12):
            continue
        if item["id"] in known_ids or item["url"] in known_urls:
            continue
        if is_duplicate(item["title_original"], known_titles):
            continue
        known_ids.add(item["id"])
        known_urls.add(item["url"])
        known_titles.append(normalize_title(item["title_original"]))
        new_items.append(item)

    new_items.sort(key=lambda item: item["published"], reverse=True)

    print(f"■ 新着 {len(new_items)}件（重複と期間外を除外）")
    if len(new_items) > MAX_NEW_PER_RUN:
        print(f"  うち{MAX_NEW_PER_RUN}件を今回処理（残りは次回）")
        # 「持ち物」寄りの収集元の枠を先に確保してから、残りを出典を回して埋める。
        owned = interleave_by_source(
            [item for item in new_items if item["hint"] == "own"], OWN_QUOTA
        )
        taken = {item["id"] for item in owned}
        rest = interleave_by_source(
            [item for item in new_items if item["id"] not in taken],
            MAX_NEW_PER_RUN - len(owned),
        )
        new_items = owned + rest
        new_items.sort(key=lambda item: item["published"], reverse=True)

    enriched: list[dict] = []
    replaced: set[str] = set()
    if new_items:
        enriched = enrich(new_items)
        print(f"  要約 {len(enriched)}件（ミニマリズムと無関係と判定された分は除外）")
        enriched, replaced = dedupe_stories(enriched, existing_items)
        print(f"  掲載対象 {len(enriched)}件")
        # 実際に載せる記事だけ元ページを見に行く（無駄なアクセスを増やさないため）。
        resolve_google_urls(enriched)
        fill_missing_images(enriched)

    # 既に載っている記事にも、あとから足した出典名の変換とブロックを後追いで効かせる。
    kept_existing = [
        {**item, "source": DOMAIN_NAMES.get(item["source"], item["source"])}
        for item in existing_items
        if item["id"] not in replaced
        and item.get("category") in CATEGORIES
        and item.get("axis") in AXES
        and not any(blocked.lower() in item["source"].lower() for blocked in BLOCK_SOURCES)
    ]

    merged = enriched + kept_existing
    merged = [
        item
        for item in merged
        if (parse_date(item.get("published", "")) or now) >= now - dt.timedelta(days=KEEP_DAYS)
    ]
    merged.sort(key=lambda item: item["published"], reverse=True)
    merged = merged[:KEEP_MAX]

    # 以前の実行で画像が付かなかった記事を、少しずつ埋めていく。
    stale = [item for item in merged if not item.get("image")][:BACKFILL_PER_RUN]
    if stale:
        print("■ 既存記事のサムネイル補完")
        resolve_google_urls(stale)
        fill_missing_images(stale)

    merged = [to_public(item) for item in merged]

    # 「持ち物」として挙がった名前を数えて、よく出てくる順に並べる。
    counts: dict[str, int] = {}
    for item in merged:
        for name in item.get("things", []):
            counts[name] = counts.get(name, 0) + 1
    things_ranking = [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= 2
    ][:40]

    payload = {
        "updated_at": now.astimezone(JST).isoformat(),
        "categories": CATEGORIES,
        "axes": AXES,
        "media_kinds": MEDIA_KINDS,
        "regions": REGIONS,
        "things": things_ranking,
        "sources": sorted({item["source"] for item in merged}),
        "count": len(merged),
        "items": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    reduce_count = sum(1 for item in merged if item["axis"] == "reduce")
    print(f"■ 書き出し: {OUTPUT_PATH}"
          f"（掲載 {len(merged)}件 / 減らし方 {reduce_count} ・ 持ち物 {len(merged) - reduce_count}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
