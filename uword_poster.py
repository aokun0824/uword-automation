#!/usr/bin/env python3
"""
ユーワード自動投稿スクリプト
EGAO Works（えがおワークス）のサービス宣伝 × 今日のAIニュース
"""
import os
import sys
import asyncio
from datetime import datetime
from pathlib import Path
import anthropic
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ===== 設定 =====
HISTORY_FILE = Path(__file__).parent / "history.txt"
MAX_HISTORY = 10
TITLE_MAX = 30
BODY_MAX = 140
LOGIN_URL = "https://u-word.com/horby/login"
POST_URL = "https://u-word.com/horby/myPage/realTimePost"
SERVICE_URL = "https://u-word.com/horby/store/storeDetail/134041"
MODEL = "claude-haiku-4-5"

LINE_URL = "YOUR_LINE_URL_HERE"  # ← 後でLINEのURLに差し替えてください


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


def generate_post(history: list[str]) -> tuple[str, str]:
    """タイトルと本文を別々に生成して返す"""
    client = anthropic.Anthropic()
    history_block = "\n".join(f"- {h}" for h in history) if history else "（履歴なし）"

    prompt = f"""あなたはEGAO Works（えがおワークス）のSNS担当です。
EGAO Worksは初心者・個人事業主向けに、AIを活用したデザイン・HP・チラシ制作・アプリ開発・デジタルサポート・AI講座を提供しています。

今日のAIやデジタルに関する旬なトピックを1つ取り上げ、それをきっかけにEGAO Worksのサービスへ自然に誘導する投稿を作ってください。

【出力形式】必ず以下の形式で出力すること（余計な説明は不要）：
TITLE: （ここに見出し・25文字以内）
BODY: （ここに本文・140文字以内）

【本文のルール】
- 今日のAI/デジタルトレンドに触れる（例：画像生成AI、ChatGPT活用、SNS自動化など）
- 「初心者でも大丈夫」「プロに任せてラクに」など安心感のある言葉を入れる
- EGAO Worksへの誘導で締める（「えがおワークスにご相談ください」「えがおワークスがサポートします」など）
- URLやハッシュタグは不要
- 過去投稿との重複を避ける

【過去の投稿履歴（直近{MAX_HISTORY}件）】
{history_block}

投稿:"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    print(f"[Claude生成結果]\n{raw}")

    # TITLE: と BODY: をパース
    title = ""
    body = ""
    for line in raw.splitlines():
        if line.startswith("TITLE:"):
            title = line.replace("TITLE:", "").strip()
        elif line.startswith("BODY:"):
            body = line.replace("BODY:", "").strip()

    # フォールバック（パース失敗時）
    if not title:
        title = raw[:25]
    if not body:
        body = raw

    # 文字数制限
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX]
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
            await page.wait_for_timeout(5000)  # Angular初期化待ち
            print(f"[現在URL] {page.url}")
            print(f"[ページタイトル] {await page.title()}")

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
    print(f"=== EGAO Works 自動投稿 開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    history = load_history()
    print(f"[履歴] {len(history)} 件を参照")
    print("[Claude API] 投稿文を生成中...")
    title, body = generate_post(history)
    print(f"[タイトル] {title}")
    print(f"[本文] {body}")
    print("[Playwright] ブラウザ操作を開始...")
    await post_to_uword(title, body)
    save_history(title, body)
    print("=== 投稿完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
