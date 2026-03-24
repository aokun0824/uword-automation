#!/usr/bin/env python3
"""
ユーワード自動投稿スクリプト
Claude 3.5 Haiku で速報文を生成し、Playwright でユーワードに投稿する
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
MAX_HISTORY = 10          # 参照する過去履歴の最大件数
MAX_CHARS = 140           # 投稿文字数上限
LOGIN_URL = "https://u-word.com/member/login"
MODEL = "claude-3-5-haiku-20241022"  # Claude 3.5 Haiku


def load_history() -> list[str]:
    """history.txt から過去の投稿を読み込む"""
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    # 空行を除去して末尾から MAX_HISTORY 件取得
    entries = [l for l in lines if l.strip()]
    return entries[-MAX_HISTORY:]


def save_history(text: str) -> None:
    """新しい投稿を history.txt に追記する"""
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    print(f"[履歴保存] {text[:30]}...")


def generate_post(history: list[str]) -> str:
    """Claude API を呼び出して速報文を生成する"""
    client = anthropic.Anthropic()

    history_block = "\n".join(f"- {h}" for h in history) if history else "（履歴なし）"

    prompt = f"""あなたはユーワード（SNS）のリアルタイム速報担当です。
以下の過去投稿と重複しない、新鮮で読者の関心を引く速報文を1件だけ生成してください。

【制約】
- 140文字以内（日本語）
- 過去投稿との内容・表現の重複を避ける
- 「速報」「リアルタイム」らしい臨場感のある文体
- URLやハッシュタグは不要
- 生成文のみを出力すること（説明文・前置きは不要）

【過去の投稿履歴（直近{MAX_HISTORY}件）】
{history_block}

速報文:"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    # 文字数が超えていたら末尾でカット
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return text


async def post_to_uword(post_text: str) -> bool:
    """Playwright でユーワードにログインし、速報を投稿する"""
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
            # ---- ログイン ----
            print(f"[アクセス] {LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            # ID 入力（複数セレクタ候補を順番に試行）
            id_selectors = [
                'input[name="login_id"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[id*="login"]',
                'input[id*="id"]',
            ]
            for sel in id_selectors:
                try:
                    await page.fill(sel, uword_id, timeout=3_000)
                    print(f"[ID入力] {sel}")
                    break
                except Exception:
                    continue

            # PW 入力
            await page.fill('input[type="password"]', uword_pw)
            print("[PW入力] 完了")

            # ログインボタン（複数候補を順番に試行）
            login_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("ログイン")',
                'a:has-text("ログイン")',
            ]
            for sel in login_selectors:
                try:
                    await page.click(sel, timeout=3_000)
                    print(f"[ログインボタン] {sel}")
                    break
                except Exception:
                    continue
            await page.wait_for_load_state("networkidle", timeout=20_000)
            print("[ログイン] 完了")

            # ---- 投稿欄に移動（トップまたは速報ページ） ----
            # ログイン後のトップで速報欄を探す
            # セレクタは実際のHTMLに合わせて調整が必要
            post_selectors = [
                'textarea[name*="content"]',
                'textarea[name*="text"]',
                'textarea[placeholder*="速報"]',
                'textarea[placeholder*="投稿"]',
                'textarea',
            ]

            post_area = None
            for selector in post_selectors:
                try:
                    post_area = page.locator(selector).first
                    await post_area.wait_for(timeout=5_000)
                    break
                except PlaywrightTimeoutError:
                    continue

            if post_area is None:
                raise RuntimeError("投稿テキストエリアが見つかりませんでした")

            await post_area.click()
            await post_area.fill(post_text)
            print(f"[入力完了] {post_text[:40]}...")

            # ---- 送信ボタン ----
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("送信")',
                'button:has-text("投稿")',
            ]
            for sel in submit_selectors:
                try:
                    submit_btn = page.locator(sel).first
                    await submit_btn.wait_for(timeout=3_000)
                    await submit_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    print("[送信完了]")
                    success = True
                    break
                except PlaywrightTimeoutError:
                    continue

            if not success:
                raise RuntimeError("送信ボタンが見つかりませんでした")

        except Exception as e:
            print(f"[ERROR] ブラウザ操作中にエラーが発生しました: {e}", file=sys.stderr)
            # スクリーンショットを保存してデバッグに活用
            await page.screenshot(path="error_screenshot.png")
            raise
        finally:
            await browser.close()

    return success


async def main():
    print(f"=== ユーワード自動投稿 開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")

    # 1. 履歴読み込み
    history = load_history()
    print(f"[履歴] {len(history)} 件を参照")

    # 2. 文章生成
    print("[Claude API] 速報文を生成中...")
    post_text = generate_post(history)
    print(f"[生成テキスト] {post_text}")

    # 3. ユーワードに投稿
    print("[Playwright] ブラウザ操作を開始...")
    await post_to_uword(post_text)

    # 4. 履歴更新
    save_history(post_text)

    print("=== 投稿完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
