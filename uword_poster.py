#!/usr/bin/env python3
"""
ユーワード自動投稿スクリプト
設定は users/<会員名>.yaml で管理します

使い方:
  # 単一会員（スケジュール照合あり）
  python uword_poster.py --config users/83900.yaml

  # 単一会員（スケジュール無視・手動実行用）
  python uword_poster.py --config users/83900.yaml --force

  # 全会員を一括処理（スケジュール該当者のみ投稿）
  python uword_poster.py --run-all

  # 全会員を一括処理（スケジュール無視）
  python uword_poster.py --run-all --force
"""
import os
import sys
import asyncio
import argparse
import feedparser
import yaml
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import anthropic
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


def is_scheduled_now(config: dict, tolerance_minutes: int = 45) -> bool:
    """現在時刻がschedule.timesに該当するか判定する（±tolerance分の余裕）"""
    schedule = config.get("schedule", {})
    times = schedule.get("times", [])
    tz_name = schedule.get("timezone", "Asia/Tokyo")

    if not times:
        print("[スケジュール] 時刻未設定のためスキップ")
        return False

    now = datetime.now(ZoneInfo(tz_name))
    now_minutes = now.hour * 60 + now.minute

    for t in times:
        h, m = map(int, str(t).split(":"))
        target_minutes = h * 60 + m
        diff = abs(now_minutes - target_minutes)
        # 日をまたぐケース（例: 23:50 と 00:10）
        diff = min(diff, 1440 - diff)
        if diff <= tolerance_minutes:
            print(f"[スケジュール] {t} に該当（現在 {now:%H:%M}、差分 {diff}分）")
            return True

    print(f"[スケジュール] 該当なし（現在 {now:%H:%M}、設定: {times}）")
    return False


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

    # キーワード・メニュー
    keywords = [k for k in config.get("keywords", []) if k and k.strip()]
    menu_items = [m for m in config.get("menu_items", []) if m and m.strip()]
    keyword_block = "、".join(keywords) if keywords else ""
    menu_block = "\n".join(f"- {m}" for m in menu_items) if menu_items else ""

    keyword_instruction = f"\n【必ず触れてほしいキーワード】\n{keyword_block}" if keyword_block else ""
    menu_instruction = f"\n【紹介したいメニュー・サービス（1つ選んで投稿に絡めてください）】\n{menu_block}" if menu_block else ""

    prompt = f"""あなたは{profile['name']}のSNS担当スタッフです。
{profile['name']}は、{profile['description']}。

【今日の実際のAIニュース（RSS取得）】
{news_block}

上のニュースの中から1つ選び、それをきっかけに「{profile['name']}に相談しよう」と思ってもらえるような投稿を作ってください。
{keyword_instruction}{menu_instruction}

【出力形式】必ず以下の形式のみで出力（余計な説明・前置き・コメントは一切不要）：
TITLE: （見出し・{post_cfg['title_max']}文字以内）
BODY: （本文・{post_cfg['body_max']}文字以内）

【重要な制約】
- BODYの冒頭に「【この投稿はAIで自動投稿しています】」は絶対に含めないこと（システムが自動で付与します）
- BODYの末尾は必ず「{profile['cta']}」で締めること

【文体・内容のルール】
{tone_block}

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
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("TITLE:"):
            title = line.replace("TITLE:", "").strip()
        elif line.startswith("BODY:"):
            # BODY: 以降の全行を本文として取得
            first_line = line.replace("BODY:", "").strip()
            remaining = "\n".join(lines[i + 1:]).strip()
            body = f"{first_line}\n{remaining}".strip() if remaining else first_line
            break

    if not title:
        title = raw[:post_cfg["title_max"]]
    if not body:
        body = raw

    # Claude がプレフィックスを誤って含めた場合は除去
    prefix_clean = post_cfg["prefix"].replace("\\n", "\n").strip()
    if body.startswith(prefix_clean):
        body = body[len(prefix_clean):].lstrip()

    if len(title) > post_cfg["title_max"]:
        title = title[:post_cfg["title_max"]]

    cta = config["profile"].get("cta", "").strip()
    prefix = post_cfg["prefix"].replace("\\n", "\n")

    # CTA を末尾に確実に付与（すでに含んでいれば追加しない）
    if cta and cta not in body:
        body = body.rstrip("。．.！!") + "\n" + cta

    body = prefix + body
    if len(body) > post_cfg["body_max"]:
        body = body[:post_cfg["body_max"]]

    return title, body


def get_uword_credentials(config: dict) -> tuple:
    """ユーワードのID/PWを取得（暗号化認証情報 → 環境変数の順でフォールバック）"""
    credentials = config.get("uword", {}).get("credentials", {})
    if credentials.get("id_encrypted") and credentials.get("pw_encrypted"):
        encryption_key = os.environ.get("ENCRYPTION_KEY", "")
        if not encryption_key:
            raise ValueError("ENCRYPTION_KEY 環境変数が設定されていません")
        from cryptography.fernet import Fernet
        f = Fernet(encryption_key.encode())
        uword_id = f.decrypt(credentials["id_encrypted"].encode()).decode()
        uword_pw = f.decrypt(credentials["pw_encrypted"].encode()).decode()
        print("[認証] YAMLの暗号化認証情報を使用")
    else:
        secrets_cfg = config.get("secrets", {})
        uword_id = os.environ.get(secrets_cfg.get("id_env", ""))
        uword_pw = os.environ.get(secrets_cfg.get("pw_env", ""))
        print("[認証] 環境変数を使用")
    if not uword_id or not uword_pw:
        raise ValueError("ユーワードの認証情報が取得できませんでした")
    return uword_id, uword_pw


async def post_to_uword(config: dict, title: str, body: str) -> bool:
    uword_id, uword_pw = get_uword_credentials(config)

    if not uword_id or not uword_pw:
        raise ValueError("ユーワードの認証情報が設定されていません")

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


async def run_single(config_path: str, force: bool = False) -> bool:
    """単一会員の投稿処理。投稿した場合True、スキップした場合Falseを返す。"""
    config, config_path = load_config(config_path)
    profile_name = config["profile"]["name"]

    print(f"\n=== {profile_name} 自動投稿 開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    print(f"[設定] {config_path}")

    # スケジュール照合（--force時はスキップ）
    if not force and not is_scheduled_now(config):
        print(f"=== {profile_name} スケジュール外のためスキップ ===")
        return False

    history_file = get_history_file(config_path)
    print(f"[履歴] {history_file}")

    history = load_history(history_file, config["post"]["history_max"])
    print(f"[履歴] {len(history)} 件を参照")

    # 自由文章が設定されていればそちらを優先
    manual = config.get("next_post", {})
    manual_title = (manual.get("title") or "").strip()
    manual_body  = (manual.get("body")  or "").strip()

    if manual_title and manual_body:
        print("[モード] 手動投稿文を使用")
        title = manual_title
        body  = config["post"]["prefix"].replace("\\n", "\n") + manual_body
        if len(body) > config["post"]["body_max"]:
            body = body[:config["post"]["body_max"]]
        use_manual = True
    else:
        news = fetch_news(config["rss"]["feeds"])
        print("[Claude API] 投稿文を生成中...")
        title, body = generate_post(config, history, news)
        use_manual = False

    print(f"[タイトル] {title}")
    print(f"[本文] {body}")

    print("[Playwright] ブラウザ操作を開始...")
    await post_to_uword(config, title, body)

    save_history(history_file, title, body)

    # 手動投稿文を使った場合、YAMLからクリアする
    if use_manual:
        config["next_post"] = {"title": "", "body": ""}
        with config_path.open("w", encoding="utf-8") as f:
            import yaml as _yaml
            _yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print("[手動文章] 使用済み・クリアしました")

    print(f"=== {profile_name} 投稿完了 ===")
    return True


async def run_all(force: bool = False):
    """users/フォルダ内の全会員を処理する。"""
    users_dir = Path(__file__).parent / "users"
    configs = sorted(users_dir.glob("*.yaml"))
    configs = [c for c in configs if c.name != "template.yaml"]

    if not configs:
        print("[ERROR] users/ に設定ファイルが見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"=== 一括投稿開始 ({datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    print(f"[対象] {len(configs)} 会員: {[c.stem for c in configs]}")

    posted = 0
    skipped = 0
    errors = []

    for config_file in configs:
        try:
            result = await run_single(str(config_file), force=force)
            if result:
                posted += 1
            else:
                skipped += 1
        except Exception as e:
            errors.append((config_file.stem, str(e)))
            print(f"[ERROR] {config_file.stem}: {e}", file=sys.stderr)

    print(f"\n=== 一括投稿完了 ===")
    print(f"  投稿: {posted} 件 / スキップ: {skipped} 件 / エラー: {len(errors)} 件")
    for slug, err in errors:
        print(f"  [ERROR] {slug}: {err}")

    if errors:
        sys.exit(1)


async def main():
    parser = argparse.ArgumentParser(description="ユーワード自動投稿スクリプト")
    parser.add_argument(
        "--config",
        help="設定ファイルのパス（例: users/83900.yaml）"
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="users/フォルダ内の全会員を一括処理"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="スケジュール照合をスキップ（手動実行用）"
    )
    args = parser.parse_args()

    if args.run_all:
        await run_all(force=args.force)
    elif args.config:
        await run_single(args.config, force=args.force)
    else:
        parser.error("--config または --run-all を指定してください")


if __name__ == "__main__":
    asyncio.run(main())
