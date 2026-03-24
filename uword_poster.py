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
MAX_HISTORY = 10        # 参照する過去履歴の最大件数
MAX_CHARS = 140         # 投稿文字数上限
LOGIN_URL = "https://u-word.com/horby/login"
POST_URL = "https://u-word.com/horby/myPage/realTimePost"
MODEL = "claude-3-5-haiku-20241022"  # Claude 3.5 Haiku


def load_history() -> list[str]:
        """history.txt から過去の投稿を読み込む"""
        if not HISTORY_FILE.exists():
                    return []
                lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
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

            # ID入力 (ion-input-0)
                        await page.fill("input#ion-input-0", uword_id, timeout=10_000)
                        print("[ID入力] 完了")

            # PW入力 (ion-input-1)
                        await page.fill("input#ion-input-1", uword_pw, timeout=10_000)
                        print("[PW入力] 完了")

            # ログインボタン
                        await page.click("button.mypage_btn_inner", timeout=5_000)
                        print("[ログインボタン] クリック")

            await page.wait_for_load_state("networkidle", timeout=20_000)
            print("[ログイン] 完了")

            # ---- 投稿ページに移動 ----
            print(f"[アクセス] {POST_URL}")
            await page.goto(POST_URL, wait_until="networkidle", timeout=30_000)

            # タイトル入力
            await page.fill("input[name='title']", post_text[:50], timeout=10_000)
            print(f"[タイトル入力] 完了")

            # 掲載情報（本文）入力
            await page.fill("textarea[name='content']", post_text, timeout=10_000)
            print(f"[本文入力] 完了: {post_text[:40]}...")

            # カテゴリー選択（出勤・空き情報 = radio_category_1）
            await page.click("input#radio_category_1", timeout=5_000)
            print("[カテゴリー] 出勤・空き情報 を選択")

            # 投稿ボタンが有効になるまで待機
            await page.wait_for_selector(
                                "ion-button.segment_btn_publish:not(.button-disabled)",
                                timeout=10_000
            )

            # 投稿ボタンをクリック
            await page.click("ion-button.segment_btn_publish", timeout=5_000)
            print("[投稿ボタン] クリック")

            await page.wait_for_load_state("networkidle", timeout=15_000)
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
