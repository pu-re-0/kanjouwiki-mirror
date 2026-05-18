import json
import re
import time
import html
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

# 例: https://YOUR_GITHUB_USERNAME.github.io/wiki-ai-mirror/
# 末尾の / を付けてください
PUBLIC_BASE_URL = "https://pu-re-0.github.io/wiki-ai-mirror/"

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

    return filename + ".md"


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

def api_get(params: dict) -> dict:
    """
    MediaWiki API GETリクエスト。
    """
    params = dict(params)
    params["format"] = "json"
    params["formatversion"] = "2"

    response = requests.get(
        API_URL,
        params=params,
        headers={
            "User-Agent": "wiki-ai-mirror/1.0 (GitHub Pages mirror for AI reading)"
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


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


def write_index_html(index: list[dict]) -> None:
    """
    人間とAIの両方が辿りやすいトップページを出力する。
    """
    rows = []

    for item in index:
        title = html.escape(item["title"])
        href = html.escape(encoded_relative_url(item["mirror_url"]), quote=True)
        source = html.escape(item.get("encoded_source_url") or item["source_url"], quote=True)
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


def write_robots() -> None:
    """
    ChatGPTから読ませることを意識したrobots.txt。
    AI関連クローラーと通常クローラーを許可する。
    """
    robots = f"""User-agent: OAI-SearchBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: GPTBot
Allow: /

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
        urls.append(encoded_public_url(item["mirror_url"]))

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


def cleanup_stale_pages(titles: list[str]) -> None:
    """
    今回の同期対象に存在しない古いMarkdownを削除する。
    """
    print("cleanup stale pages: start")
    expected_files = {safe_filename(title) for title in titles}
    deleted_count = 0

    for path in PAGES_DIR.glob("*.md"):
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

    for i, title in enumerate(titles, start=1):
        print(f"[{i}/{len(titles)}] {title}")

        try:
            page = get_page_data(title)
            if not page:
                failures.append(f"{title}: no page data")
                print(f"ERROR: no page data: {title}")
                continue

            plain_text = parse_wikitext_to_plain_text(title)

            filename = safe_filename(title)
            mirror_path = f"pages/{filename}"

            markdown = make_markdown(page, plain_text)
            (PAGES_DIR / filename).write_text(markdown, encoding="utf-8")

            index.append({
                "title": page["title"],
                "namespace": page.get("namespace"),
                "source_url": page["source_url"],
                "encoded_source_url": page["encoded_source_url"],
                "mirror_url": mirror_path,
                "public_url": public_url(mirror_path),
                "encoded_public_url": encoded_public_url(mirror_path),
                "last_modified": page.get("last_modified"),
                "categories": page.get("categories", []),
            })

            time.sleep(SLEEP_SECONDS)

        except Exception as exc:
            failures.append(f"{title}: {exc}")
            print(f"ERROR: {title}: {exc}")

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
