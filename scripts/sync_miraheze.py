import json
import re
import time
import html
import hashlib
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


# ============================================================
# Miraheze -> GitHub Pages AI mirror sync script
# ============================================================
#
# 使い方:
# 1. API_URL, WIKI_BASE_URL, PUBLIC_BASE_URL を自分の環境に合わせて変更
# 2. requirements.txt に以下を入れる
#      requests
#      beautifulsoup4
# 3. 実行:
#      python scripts/sync_miraheze.py
#
# 出力:
#   public/
#     index.html
#     index.json
#     robots.txt
#     sitemap.xml
#     pages/
#       記事名.md
#
# ============================================================


# ===== 設定 =====

# 例: https://example.miraheze.org/w/api.php
API_URL = "https://kanjou.miraheze.org/w/api.php"

# 例: https://example.miraheze.org/wiki/
WIKI_BASE_URL = "https://kanjou.miraheze.org/wiki/"

# 例: https://YOUR_GITHUB_USERNAME.github.io/kanjouwiki-mirror/
# 末尾の / を付けてください
PUBLIC_BASE_URL = "https://pu-re-0.github.io/kanjouwiki-mirror/"

# 出力先
OUTPUT_DIR = Path("public")
PAGES_DIR = OUTPUT_DIR / "pages"

# 取得する名前空間
# 0: 通常記事
# 10: Template
# 14: Category
#
# 最初は [0] だけで動作確認するのがおすすめです。
NAMESPACES = [0]

# API連続アクセスの待機時間
SLEEP_SECONDS = 0.2

# 一時的なAPIエラーの再試行回数
MAX_RETRIES = 5
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# 差分判定用メタデータをまとめて取得する件数
METADATA_BATCH_SIZE = 25
PARTIAL_INDEX_SAVE_EVERY_BATCHES = 10

# 除外したいタイトルの接頭辞
# namespace 0 だけなら多くは不要ですが、念のため入れています。
EXCLUDE_PREFIXES = [
    "Special:",
    "特別:",
    "User:",
    "利用者:",
    "Talk:",
    "トーク:",
    "MediaWiki:",
    "Broken/",
    "Broken:",
]
# 除外したいタイトル内文字列
EXCLUDE_TITLE_CONTAINS = [
    # "下書き",
    # "テスト",
    # "sandbox",
]

# 「2000」「2000年」「1484年代」のような年号ページを除外するか
EXCLUDE_YEAR_ONLY_PAGES = True

FILESYSTEM_UNSAFE_CHARS = set('%<>:"/\\|?*')
URL_PATH_UNSAFE_CHARS = set("%#?/")
MAX_FILENAME_BYTES = 240


# ===== ユーティリティ =====

def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(title: str) -> str:
    """
    GitHub Pages上で扱いやすいファイル名にする。
    日本語はそのまま残し、WindowsやURLで問題になる文字だけURLエンコードする。
    """
    filename = "".join(
        quote(char, safe="")
        if char in FILESYSTEM_UNSAFE_CHARS or ord(char) < 32
        else char
        for char in title
    )

    while filename.endswith(" "):
        filename = filename[:-1] + "%20"

    while filename.endswith("."):
        filename = filename[:-1] + "%2E"

    return shorten_filename(filename, title)


def safe_html_filename(title: str) -> str:
    """
    Markdownと同じベース名でHTMLファイル名を作る。
    """
    markdown_filename = safe_filename(title)
    return markdown_filename[:-3] + ".html"


def page_markdown_path(title: str) -> str:
    """
    記事Markdownのpublic相対パスを作る。
    """
    return f"pages/{safe_filename(title)}"


def page_html_path(title: str) -> str:
    """
    記事HTMLのpublic相対パスを作る。
    """
    return f"pages/{safe_html_filename(title)}"


def shorten_filename(filename: str, title: str) -> str:
    """
    ファイルシステムの1ファイル名長制限に収まるよう、長いタイトルだけ短縮する。
    """
    extension = ".md"
    full_filename = filename + extension
    if len(full_filename.encode("utf-8")) <= MAX_FILENAME_BYTES:
        return full_filename

    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    suffix = f"--{digest}{extension}"
    max_prefix_bytes = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))

    prefix = ""
    used_bytes = 0
    for char in filename:
        char_bytes = len(char.encode("utf-8"))
        if used_bytes + char_bytes > max_prefix_bytes:
            break
        prefix += char
        used_bytes += char_bytes

    return prefix.rstrip(" .") + suffix


def source_page_url(title: str) -> str:
    """
    元wikiの記事URLを人間が読みやすいIRIとして作る。
    """
    path = "".join(
        quote(char, safe="")
        if char in URL_PATH_UNSAFE_CHARS or ord(char) < 32
        else char
        for char in title.replace(" ", "_")
    )
    return WIKI_BASE_URL + path


def encoded_source_page_url(title: str) -> str:
    """
    元wikiの記事URLを通常のURLとして安全な形にエンコードする。
    """
    return WIKI_BASE_URL + quote(title.replace(" ", "_"), safe="")


def should_exclude_title(title: str) -> bool:
    """
    除外対象タイトルなら True。
    """

    for prefix in EXCLUDE_PREFIXES:
        if title.startswith(prefix):
            return True

    lower_title = title.lower()
    for keyword in EXCLUDE_TITLE_CONTAINS:
        if keyword.lower() in lower_title:
            return True

    # 「2000」「2000年」「1484」「1484年」のような年号ページを除外
    if EXCLUDE_YEAR_ONLY_PAGES:
        normalized_title = title.strip()

        if re.fullmatch(r"\d{1,4}年?", normalized_title):
            return True

        if re.fullmatch(r"\d{1,4}年代?", normalized_title):
            return True

        if re.fullmatch(r"\d{1,2}月\d{1,2}日?", normalized_title):
            return True

    return False


def public_url(path: str) -> str:
    """
    GitHub Pages上の公開URLを人間が読みやすいIRIとして作る。
    """
    return PUBLIC_BASE_URL.rstrip("/") + "/" + path.lstrip("/")


def encoded_public_url(path: str) -> str:
    """
    sitemapやHTML属性に入れるため、URLとして安全な形にエンコードする。
    """
    encoded_path = quote(path.lstrip("/"), safe="/%")
    return PUBLIC_BASE_URL.rstrip("/") + "/" + encoded_path


def encoded_relative_url(path: str) -> str:
    """
    HTMLのhref属性に入れる相対URLを安全な形にエンコードする。
    """
    return quote(path, safe="/%")


# ===== MediaWiki API =====

def api_request(params: dict, method: str = "GET") -> dict:
    """
    MediaWiki APIリクエスト。
    """
    params = dict(params)
    params["format"] = "json"
    params["formatversion"] = "2"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request_kwargs = {"params": params} if method == "GET" else {"data": params}
            response = requests.request(
                method,
                API_URL,
                headers={
                    "User-Agent": "wiki-ai-mirror/1.0 (GitHub Pages mirror for AI reading)"
                },
                timeout=30,
                **request_kwargs,
            )

            if response.status_code in RETRY_STATUS_CODES:
                response.raise_for_status()

            response.raise_for_status()
            return response.json()

        except requests.RequestException as exc:
            last_error = exc
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            should_retry = status_code in RETRY_STATUS_CODES or status_code is None

            if not should_retry or attempt == MAX_RETRIES:
                raise

            wait_seconds = min(60, SLEEP_SECONDS * (2 ** attempt))
            print(
                f"WARNING: API request failed "
                f"(attempt {attempt}/{MAX_RETRIES}, status={status_code}): {exc}"
            )
            print(f"WARNING: retrying in {wait_seconds:.1f}s")
            time.sleep(wait_seconds)

    raise RuntimeError(f"API request failed: {last_error}")


def api_get(params: dict) -> dict:
    """
    MediaWiki API GETリクエスト。
    """
    return api_request(params, method="GET")


def api_post(params: dict) -> dict:
    """
    MediaWiki API POSTリクエスト。
    """
    return api_request(params, method="POST")


def get_all_page_titles(namespace: int) -> list[str]:
    """
    指定名前空間の全ページタイトルを取得する。
    """
    titles: list[str] = []

    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": namespace,
        "aplimit": "max",
    }

    while True:
        data = api_get(params)

        pages = data.get("query", {}).get("allpages", [])
        for page in pages:
            title = page["title"]
            if not should_exclude_title(title):
                titles.append(title)

        if "continue" not in data:
            break

        params.update(data["continue"])
        time.sleep(SLEEP_SECONDS)

    return titles


def get_page_data(title: str) -> dict | None:
    """
    1ページ分の本文・カテゴリ・更新日時などを取得する。
    """
    params = {
        "action": "query",
        "prop": "revisions|categories|info",
        "titles": title,
        "rvslots": "*",
        "rvprop": "content|timestamp",
        "cllimit": "max",
        "inprop": "url",
    }

    data = api_get(params)
    pages = data.get("query", {}).get("pages", [])

    if not pages:
        return None

    page = pages[0]

    if page.get("missing"):
        return None

    revisions = page.get("revisions", [])
    if not revisions:
        return None

    revision = revisions[0]
    slots = revision.get("slots", {})
    main_slot = slots.get("main", {})
    wikitext = main_slot.get("content", "")

    categories = [
        category["title"].split(":", 1)[-1]
        for category in page.get("categories", [])
    ]

    return {
        "title": page["title"],
        "namespace": page.get("ns"),
        "pageid": page.get("pageid"),
        "source_url": source_page_url(page["title"]),
        "encoded_source_url": page.get("fullurl") or encoded_source_page_url(page["title"]),
        "last_modified": revision.get("timestamp"),
        "categories": categories,
        "wikitext": wikitext,
    }


def get_pages_metadata(titles: list[str]) -> dict[str, dict]:
    """
    差分判定用に、複数ページの更新日時など軽いメタデータだけ取得する。
    """
    params = {
        "action": "query",
        "prop": "revisions|info",
        "titles": "|".join(titles),
        "rvprop": "timestamp",
        "inprop": "url",
    }

    data = api_post(params)
    pages = data.get("query", {}).get("pages", [])
    metadata: dict[str, dict] = {}

    for page in pages:
        if page.get("missing"):
            continue

        revisions = page.get("revisions", [])
        if not revisions:
            continue

        title = page["title"]
        metadata[title] = {
            "title": title,
            "namespace": page.get("ns"),
            "pageid": page.get("pageid"),
            "source_url": source_page_url(title),
            "encoded_source_url": page.get("fullurl") or encoded_source_page_url(title),
            "last_modified": revisions[0].get("timestamp"),
        }

    return metadata


def parse_wikitext_to_plain_text(title: str) -> str:
    """
    MediaWiki側でページをHTMLにパースし、AIが読みやすいプレーンテキストに変換する。
    失敗した場合は空文字を返す。
    """
    params = {
        "action": "parse",
        "page": title,
        "prop": "text",
    }

    try:
        data = api_get(params)
        html_text = data.get("parse", {}).get("text", "")
    except Exception as exc:
        print(f"WARNING: parse failed: {title}: {exc}")
        return ""

    soup = BeautifulSoup(html_text, "html.parser")

    # 編集リンクや不要要素を除去
    for tag in soup.select(".mw-editsection, style, script, noscript"):
        tag.decompose()

    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


# ===== Markdown / HTML / JSON 出力 =====

def make_markdown(page: dict, plain_text: str) -> str:
    """
    1ページ分のMarkdownを作る。
    AIにはプレーンテキスト本文を読ませつつ、必要に応じてRaw wikitextも参照できる形にする。
    """
    title = page["title"]
    categories = ", ".join(page.get("categories", [])) or "なし"

    header = f"""# {title}

Source: {page["source_url"]}
Last modified: {page.get("last_modified") or "unknown"}
Namespace: {page.get("namespace")}
Categories: {categories}

---

"""

    body = plain_text or page.get("wikitext", "")

    raw_wikitext = page.get("wikitext", "").strip()
    fence = "`" * (max((len(match) for match in re.findall(r"`+", raw_wikitext)), default=2) + 1)

    footer = f"""

---

## Raw wikitext

{fence}wikitext
{raw_wikitext}
{fence}
"""

    return header + body.strip() + footer


def make_page_html(page: dict, plain_text: str, markdown_url: str) -> str:
    """
    通常のブラウザ/AIブラウザが辿りやすい記事HTMLを作る。
    """
    title = html.escape(page["title"])
    source_url = html.escape(page["encoded_source_url"], quote=True)
    markdown_href = html.escape(encoded_relative_url(markdown_url), quote=True)
    last_modified = html.escape(page.get("last_modified") or "unknown")
    categories = html.escape(", ".join(page.get("categories", [])) or "なし")
    body = html.escape(plain_text or page.get("wikitext", ""))

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="robots" content="noindex, follow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <article>
    <h1>{title}</h1>
    <p>
      Source: <a href="{source_url}">original wiki page</a><br>
      Markdown: <a href="{markdown_href}">markdown source</a><br>
      Last modified: {last_modified}<br>
      Categories: {categories}
    </p>
    <hr>
    <pre>{body}</pre>
  </article>
</body>
</html>
"""


def write_index_html(index: list[dict]) -> None:
    """
    人間とAIの両方が辿りやすいトップページを出力する。
    """
    rows = []

    for item in index:
        title = html.escape(item["title"])
        href = html.escape(encoded_relative_url(page_html_path(item["title"])), quote=True)
        source = html.escape(encoded_source_page_url(item["title"]), quote=True)
        modified = html.escape(item.get("last_modified") or "")

        rows.append(
            f'<li><a href="{href}">{title}</a> '
            f'<small>({modified})</small> '
            f'<a href="{source}">source</a></li>'
        )

    body = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>Wiki AI Mirror</title>
  <meta name="robots" content="noindex, follow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <h1>Wiki AI Mirror</h1>
  <p>This is a static mirror for AI-assisted reading and article drafting.</p>
  <p>
    <a href="index.json">index.json</a> /
    <a href="sitemap.xml">sitemap.xml</a>
  </p>
  <ul>
    {''.join(rows)}
  </ul>
</body>
</html>
"""

    (OUTPUT_DIR / "index.html").write_text(body, encoding="utf-8")


def write_index_json(index: list[dict]) -> None:
    """
    AIに渡す記事一覧JSONを出力する。
    """
    (OUTPUT_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_partial_index_json(index: list[dict]) -> None:
    """
    途中失敗しても次回差分同期できるよう、処理済みindexを保存する。
    """
    if not index:
        return

    write_index_json(sorted(index, key=lambda item: item["title"]))


def write_robots() -> None:
    """
    ChatGPTから読ませることを意識したrobots.txt。
    通常取得を許可し、インデックス抑制はHTML側のnoindexに任せる。
    """
    robots = f"""User-agent: OAI-SearchBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: GPTBot
Disallow: /

User-agent: *
Allow: /

Sitemap: {public_url("sitemap.xml")}
"""

    (OUTPUT_DIR / "robots.txt").write_text(robots, encoding="utf-8")


def write_sitemap(index: list[dict]) -> None:
    """
    sitemap.xmlを出力する。
    """
    urls = [
        encoded_public_url(""),
        encoded_public_url("index.json"),
    ]

    for item in index:
        urls.append(encoded_public_url(page_html_path(item["title"])))

    xml_items = []
    for url in urls:
        xml_items.append(f"""  <url>
    <loc>{html.escape(url)}</loc>
  </url>""")

    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(xml_items)}
</urlset>
"""

    (OUTPUT_DIR / "sitemap.xml").write_text(sitemap, encoding="utf-8")


# ===== メイン処理 =====

def collect_titles() -> list[str]:
    """
    設定された名前空間から全タイトルを集める。
    """
    all_titles: list[str] = []

    for namespace in NAMESPACES:
        titles = get_all_page_titles(namespace)
        print(f"namespace {namespace}: {len(titles)} pages")
        all_titles.extend(titles)

    # 重複除去しつつソート
    return sorted(set(all_titles))


def chunked(items: list[str], size: int) -> list[list[str]]:
    """
    リストを指定件数ごとの塊に分ける。
    """
    return [items[i:i + size] for i in range(0, len(items), size)]


def load_existing_index() -> dict[str, dict]:
    """
    前回生成済みのindex.jsonを読み、差分同期に使う。
    """
    index_path = OUTPUT_DIR / "index.json"
    if not index_path.exists() or index_path.stat().st_size == 0:
        return {}

    try:
        items = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARNING: existing index.json is invalid; full sync required: {exc}")
        return {}

    return {
        item["title"]: item
        for item in items
        if isinstance(item, dict) and item.get("title")
    }


def make_index_item(page: dict) -> dict:
    """
    ページ情報からindex.json用メタデータを作る。
    """
    title = page["title"]
    html_filename = safe_html_filename(title)
    html_path = f"pages/{html_filename}"

    return {
        "title": title,
        "namespace": page.get("namespace"),
        "source_url": page["source_url"],
        "public_url": public_url(html_path),
        "encoded_public_url": encoded_public_url(html_path),
        "last_modified": page.get("last_modified"),
        "categories": page.get("categories", []),
    }


def cached_page_is_current(metadata: dict, existing_item: dict | None) -> bool:
    """
    前回生成済みMarkdownが更新不要ならTrue。
    """
    if not existing_item:
        return False

    if existing_item.get("last_modified") != metadata.get("last_modified"):
        return False

    expected_markdown_path = OUTPUT_DIR / page_markdown_path(metadata["title"])
    expected_html_path = OUTPUT_DIR / page_html_path(metadata["title"])
    return expected_markdown_path.exists() and expected_html_path.exists()


def cleanup_stale_pages(titles: list[str]) -> None:
    """
    今回の同期対象に存在しない古いMarkdownを削除する。
    """
    print("cleanup stale pages: start")
    expected_files = {safe_filename(title) for title in titles}
    expected_files.update(safe_html_filename(title) for title in titles)
    deleted_count = 0

    for path in PAGES_DIR.glob("*"):
        if not path.is_file():
            continue
        if path.name not in expected_files:
            print(f"DELETE stale page: {path}")
            path.unlink()
            deleted_count += 1

    print(f"cleanup stale pages: done ({deleted_count} deleted)")


def sync_pages(titles: list[str]) -> list[dict]:
    """
    各ページを取得してMarkdownとして保存し、index用メタデータを返す。
    """
    index: list[dict] = []
    failures: list[str] = []
    existing_index = load_existing_index()
    skipped_count = 0
    updated_count = 0
    metadata_batches = chunked(titles, METADATA_BATCH_SIZE)

    for batch_number, batch_titles in enumerate(metadata_batches, start=1):
        print(f"metadata batch {batch_number}/{len(metadata_batches)}")
        metadata_by_title = get_pages_metadata(batch_titles)

        for title in batch_titles:
            metadata = metadata_by_title.get(title)
            if not metadata:
                failures.append(f"{title}: no metadata")
                print(f"ERROR: no metadata: {title}")
                continue

            existing_item = existing_index.get(title)
            if cached_page_is_current(metadata, existing_item):
                index.append(make_index_item({
                    **metadata,
                    "categories": existing_item.get("categories", []),
                }))
                skipped_count += 1
                if skipped_count % 500 == 0:
                    print(f"cached pages skipped: {skipped_count}")
                continue

            print(f"[{updated_count + skipped_count + 1}/{len(titles)}] update {title}")

            try:
                page = get_page_data(title)
                if not page:
                    failures.append(f"{title}: no page data")
                    print(f"ERROR: no page data: {title}")
                    continue

                plain_text = parse_wikitext_to_plain_text(title)

                filename = safe_filename(title)
                html_filename = safe_html_filename(title)
                mirror_path = f"pages/{filename}"
                markdown = make_markdown(page, plain_text)
                (PAGES_DIR / filename).write_text(markdown, encoding="utf-8")
                (PAGES_DIR / html_filename).write_text(
                    make_page_html(page, plain_text, mirror_path),
                    encoding="utf-8",
                )

                index.append(make_index_item(page))
                updated_count += 1

                time.sleep(SLEEP_SECONDS)

            except Exception as exc:
                failures.append(f"{title}: {exc}")
                print(f"ERROR: {title}: {exc}")

        time.sleep(SLEEP_SECONDS)
        if batch_number % PARTIAL_INDEX_SAVE_EVERY_BATCHES == 0:
            write_partial_index_json(index)
            print(f"partial index saved: {len(index)} pages")

    print(f"sync pages: {updated_count} updated, {skipped_count} cached")
    write_partial_index_json(index)

    if failures:
        print("Sync failed for the following pages:")
        for failure in failures:
            print(f"- {failure}")
        raise RuntimeError(f"failed to sync {len(failures)} page(s)")

    index.sort(key=lambda item: item["title"])
    return index


def main() -> None:
    ensure_dirs()

    titles = collect_titles()
    print(f"total titles: {len(titles)}")

    if not titles:
        raise RuntimeError("no pages found; refusing to publish an empty mirror")

    cleanup_stale_pages(titles)
    print("sync pages: start")
    index = sync_pages(titles)

    write_index_json(index)
    write_index_html(index)
    write_robots()
    write_sitemap(index)

    print(f"Done. Exported {len(index)} pages.")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
