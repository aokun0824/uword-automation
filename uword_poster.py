#!/usr/bin/env python3
"""
ユーワード自動投稿スクリプト
設定は users/<会員名>.yaml で管理します

使い方:
  python uword_poster.py --config users/egao-works.yaml
"""
import os
import sys
import asyncio
import argparse
import feedparser
import yaml
from datetime import datetime
from pathlib import Path
import anthropic
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] 設定ファイルが見つかりません: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f), path


def get_history_file(config_path: Path) -> Path:
    """設定ファイル名に対応した履歴ファイルパスを返す"""
    return config_path.parent.parent / f"history_{config_path.stem}.txt"


def fetch_news(rss_feeds: list[str], max_items: int = 5) -> list[str]:
    """RSSから最新ニュースタイトルを取得する"""
    headlines = []
    for url in rss_feeds:
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


def load_history(history_file: Path, max_history: int) -> list[str]:
    if not history_file.exists():
        return []
    lines = history_file.read_text(encoding="utf-8").splitlines()
    entries = [l for l in lines if l.strip()]
    return entries[-max_history:]


def save_history(history_file: Path, title: str, body: str) -> None:
    entry = f"[タイトル]{title} [本文]{body[:40]}"
    with history_file.open("a", encoding="utf-8") as f:
        f.write(entry.strip() + "\n")
    print(f"[履歴保存] {entry[:50]}...")


def generate_post(config: dict, history: list[str], news: list[str]) -> tuple[str, str]:
    """タイトルと本文を別々に生成して返す"""
    client = anthropic.Anthropic()

    profile      = config["profile"]
    post_cfg     = config["post"]
    ai_cfg       = config["ai"]
    tone_rules   = config["prompt"]["tone"]

    history_block = "\n".join(f"- {h}" for h in history) if history else "（履歴なし）"
    news_block    = "\n".join(f"- {n}" for n in news)     if news    else "（ニュース取得なし）"
    tone_block    = "\n".join(f"- {t}" for t in tone_rules)

    prompt = f"""あなたは{profile['name']}のSNS担当スタッフです。
{profile['name']}は、{profile['description']}。

【今日の実際のAIニュース（RSS取得）】
{news_block}

上のニュースの中から1つ選び、それをきっかけに「{profile['name']}に相談しよう」と思ってもらえるような投稿を作ってください。

【出力形式】必ず以下の形式のみで出力（余計な説明・前置き・コメントは一切不要）：
TITLE: （見出し・{post_cfg['title_max']}文字以内）
BODY: （本文・{post_cfg['body_max']}文字以内）

【文体・内容のルール】
{tone_block}
- 最後は「{profile['cta']}」のような自然な誘導で締める

【過去の投稿履歴（直近{post_cfg['history_max']}件）】
{history_block}

投稿:"""

    message = client.messages.create(
        model=ai_cfg["model"],
        max_tokens=ai_cfg["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    print(f"[Claude生成結果]\n{raw}")

    title = ""
    body  = ""
    for line in raw.splitlines():
        if line.startswith("TITLE:"):
            title = line.replace("TITLE:", "").strip()
        elif line.startswith("BODY:"):
            body = line.replace("BODY:", "").strip()

    if not title:
        title = raw[:post_cfg["title_max"]]
    if not body:
        body = raw

    if len(title) > post_cfg["title_max"]:
        title = title[:post_cfg["title_max"]]
    body = post_cfg["prefix"] + body
    if len(body) > post_cfg["body_max"]:
        body = body[:post_cfg["body_max"]]

    return title, body


async def post_to_uword(config: dict, title: str, body: str) -> bool:
    secrets_cfg = config["secrets"]
    uword_id = os.environ.get(secrets_cfg["id_env"])
    uword_pw = os.environ.get(secrets_cfg["pw_env"])

    if not uword_id or not uword_pw:
        raise ValueError(
            f"環境変数 {secrets_cfg['id_env']} / {secrets_cfg['pw_env']} が設定されていません"
        )

    user_path = config["uword"]["user_path"]
    login_url = f"https://u-word.com/{user_path}/login"
    post_url  = f"https://u-word.com/{user_path}/myPage/realTimePost"

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
            print(f"[アクセス] {login_url}")
            await page.goto(login_url, wait_until="networkidle", timeout=30000)
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

            print(f"[アクセス] {post_url}")
            await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

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
    parser = argparse.ArgumentParser(description="ユーワード自動投稿スクリプト")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルのパス（例: users/egao-works.yaml）"
    )
    args = parser.parse_args()

    config, config_path = load_config(args.config)
    history_file = get_history_file(config_path)
    profile_name = config["profile"]["name"]

    print(f"=== {profile_name} 自動投稿 開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    print(f"[設定] {config_path}")
    print(f"[履歴] {history_file}")

    history = load_history(history_file, config["post"]["history_max"])
    print(f"[履歴] {len(history)} 件を参照")

    news = fetch_news(config["rss"]["feeds"])

    print("[Claude API] 投稿文を生成中...")
    title, body = generate_post(config, history, news)
    print(f"[タイトル] {title}")
    print(f"[本文] {body}")

    print("[Playwright] ブラウザ操作を開始...")
    await post_to_uword(config, title, body)

    save_history(history_file, title, body)
    print(f"=== {profile_name} 投稿完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
