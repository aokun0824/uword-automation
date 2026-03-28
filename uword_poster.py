#!/usr/bin/env python3
"""
ユーワード自動投稿スクリプト
設定は config.yaml で管理します
"""
import os
import sys
import asyncio
import feedparser
import yaml
from datetime import datetime
from pathlib import Path
import anthropic
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ===== 設定読み込み =====
CONFIG_FILE = Path(__file__).parent / "config.yaml"

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[ERROR] 設定ファイルが見つかりません: {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# 設定から値を取得
HISTORY_FILE = Path(__file__).parent / "history.txt"
MAX_HISTORY   = CONFIG["post"]["history_max"]
TITLE_MAX     = CONFIG["post"]["title_max"]
BODY_MAX      = CONFIG["post"]["body_max"]
POST_PREFIX   = CONFIG["post"]["prefix"]
MODEL         = CONFIG["ai"]["model"]
MAX_TOKENS    = CONFIG["ai"]["max_tokens"]
RSS_FEEDS     = CONFIG["rss"]["feeds"]
USER_PATH     = CONFIG["uword"]["user_path"]
LOGIN_URL     = f"https://u-word.com/{USER_PATH}/login"
POST_URL      = f"https://u-word.com/{USER_PATH}/myPage/realTimePost"

PROFILE_NAME  = CONFIG["profile"]["name"]
PROFILE_DESC  = CONFIG["profile"]["description"]
PROFILE_CTA   = CONFIG["profile"]["cta"]
TONE_RULES    = CONFIG["prompt"]["tone"]


def fetch_news(max_items: int = 5) -> list[str]:
    """RSSから最新ニュースタイトルを取得する"""
    headlines = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_items]:
                title = entry.get("title", "").strip()
                if title and title not in headlines:
                    headlines.append(title)
        except Exception as e:
            print(f"[RSS取得エラー] {url}: {e}", file=sys.stderr)
    print(f"[ニュース取得] {len(headlines)} 件")
    for h in headlines[:5]:
        print(f"  - {h}")
    return headlines[:5]


def load_history() -> list[str]:
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    entries = [l for l in lines if l.strip()]
    return entries[-MAX_HISTORY:]


def save_history(title: str, body: str) -> None:
    entry = f"[タイトル]{title} [本文]{body[:40]}"
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(entry.strip() + "\n")
    print(f"[履歴保存] {entry[:50]}...")


def generate_post(history: list[str], news: list[str]) -> tuple[str, str]:
    """タイトルと本文を別々に生成して返す"""
    client = anthropic.Anthropic()
    history_block = "\n".join(f"- {h}" for h in history) if history else "（履歴なし）"
    news_block    = "\n".join(f"- {n}" for n in news)     if news    else "（ニュース取得なし）"
    tone_block    = "\n".join(f"- {t}" for t in TONE_RULES)

    prompt = f"""あなたは{PROFILE_NAME}のSNS担当スタッフです。
{PROFILE_NAME}は、{PROFILE_DESC}。

【今日の実際のAIニュース（RSS取得）】
{news_block}

上のニュースの中から1つ選び、それをきっかけに「{PROFILE_NAME}に相談しよう」と思ってもらえるような投稿を作ってください。

【出力形式】必ず以下の形式のみで出力（余計な説明・前置き・コメントは一切不要）：
TITLE: （見出し・{TITLE_MAX}文字以内）
BODY: （本文・{BODY_MAX}文字以内）

【文体・内容のルール】
{tone_block}
- 最後は「{PROFILE_CTA}」のような自然な誘導で締める

【過去の投稿履歴（直近{MAX_HISTORY}件）】
{history_block}

投稿:"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    print(f"[Claude生成結果]\n{raw}")

    title = ""
    body = ""
    for line in raw.splitlines():
        if line.startswith("TITLE:"):
            title = line.replace("TITLE:", "").strip()
        elif line.startswith("BODY:"):
            body = line.replace("BODY:", "").strip()

    if not title:
        title = raw[:TITLE_MAX]
    if not body:
        body = raw

    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX]
    body = POST_PREFIX + body
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX]

    return title, body


async def post_to_uword(title: str, body: str) -> bool:
    uword_id = os.environ.get("UWORD_ID")
    uword_pw = os.environ.get("UWORD_PW")
    if not uword_id or not uword_pw:
        raise ValueError("環境変数 UWORD_ID / UWORD_PW が設定されていません")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        page = await context.new_page()
        success = False

        try:
            print(f"[アクセス] {LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
            await page.fill("input#ion-input-0", uword_id, timeout=10000)
            print("[ID入力] 完了")
            await page.fill("input#ion-input-1", uword_pw, timeout=10000)
            print("[PW入力] 完了")
            await page.wait_for_selector("div.submit_btn button:not([disabled])", timeout=10000)
            await page.click("div.submit_btn button", timeout=5000)
            print("[ログインボタン] クリック")
            await page.wait_for_url(lambda url: "login" not in url, timeout=20000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            print(f"[ログイン] 完了 URL: {page.url}")

            print(f"[アクセス] {POST_URL}")
            await page.goto(POST_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            print(f"[現在URL] {page.url}")
            print(f"[ページタイトル] {await page.title()}")

            await page.wait_for_url(lambda url: "realTimePost" in url or "realTimeEdit" in url, timeout=10000)
            await page.wait_for_selector("ion-input[name='title'] input", state="visible", timeout=30000)
            await page.fill("ion-input[name='title'] input", title, timeout=10000)
            print(f"[タイトル入力] 完了: {title}")

            await page.fill("textarea[name='content']", body, timeout=10000)
            print(f"[本文入力] 完了: {body[:40]}...")

            await page.click("label[for='radio_category_1']", timeout=5000)
            print("[カテゴリー] 選択完了")

            await page.wait_for_selector(
                "ion-button.segment_btn_publish:not(.button-disabled)",
                timeout=10000
            )
            await page.click("ion-button.segment_btn_publish", timeout=5000)
            print("[投稿ボタン] クリック")
            await page.screenshot(path="after_click.png")
            await page.wait_for_timeout(3000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            print(f"[送信後URL] {page.url}")
            print(f"[送信後タイトル] {await page.title()}")
            await page.screenshot(path="after_submit.png")
            print("[送信完了]")
            success = True

        except Exception as e:
            print(f"[ERROR] ブラウザ操作中にエラーが発生しました: {e}", file=sys.stderr)
            await page.screenshot(path="error_screenshot.png")
            raise
        finally:
            await browser.close()

    return success


async def main():
    print(f"=== {PROFILE_NAME} 自動投稿 開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    history = load_history()
    print(f"[履歴] {len(history)} 件を参照")
    news = fetch_news()
    print("[Claude API] 投稿文を生成中...")
    title, body = generate_post(history, news)
    print(f"[タイトル] {title}")
    print(f"[本文] {body}")
    print("[Playwright] ブラウザ操作を開始...")
    await post_to_uword(title, body)
    save_history(title, body)
    print("=== 投稿完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
