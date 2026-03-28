#!/usr/bin/env python3
"""
ユーワード自動投稿 管理Web UI
環境変数:
  ADMIN_PASSWORD   : 管理者パスワード
  GH_TOKEN         : GitHub Personal Access Token
  GITHUB_REPO      : オーナー/リポジトリ名 (例: aokun0824/uword-automation)
  FLASK_SECRET_KEY : セッション暗号化キー
  ENCRYPTION_KEY   : ユーワード認証情報の暗号化キー (Fernet)
"""
import os
import base64
import yaml
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet, InvalidToken
from github import Github, GithubException, UnknownObjectException

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GITHUB_TOKEN   = os.environ.get("GH_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "aokun0824/uword-automation")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")


# ─── 暗号化ヘルパー ───────────────────────────────────────────────────────────

def get_fernet() -> Fernet:
    if not ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY が設定されていません")
    key = ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY
    return Fernet(key)

def encrypt_str(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()

def decrypt_str(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()

def verify_uword_pw(config: dict, password: str) -> bool:
    """YAMLに保存された暗号化PWと照合する"""
    try:
        pw_encrypted = config.get("uword", {}).get("credentials", {}).get("pw_encrypted", "")
        if not pw_encrypted:
            return False
        return decrypt_str(pw_encrypted) == password
    except (InvalidToken, Exception):
        return False


# ─── GitHub ヘルパー ───────────────────────────────────────────────────────────

def get_repo():
    return Github(GITHUB_TOKEN).get_repo(GITHUB_REPO)

def gh_read_yaml(path: str):
    """GitHub から YAML ファイルを読み込んで (dict, sha) を返す"""
    try:
        f = get_repo().get_contents(path)
        raw = base64.b64decode(f.content).decode("utf-8")
        return yaml.safe_load(raw), f.sha
    except UnknownObjectException:
        return None, None
    except GithubException as e:
        raise RuntimeError(f"GitHub読み込みエラー: {e}") from e

def gh_write_yaml(path: str, data: dict, sha: str, message: str) -> bool:
    content = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    try:
        repo = get_repo()
        if sha:
            repo.update_file(path, message, content, sha)
        else:
            repo.create_file(path, message, content)
        return True
    except GithubException as e:
        app.logger.error("GitHub書き込みエラー: %s", e)
        return False

def gh_create_yaml(path: str, data: dict, message: str) -> bool:
    content = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    try:
        get_repo().create_file(path, message, content)
        return True
    except GithubException as e:
        app.logger.error("GitHub作成エラー: %s", e)
        return False

def get_all_members() -> dict:
    try:
        contents = get_repo().get_contents("users")
        members = {}
        for f in contents:
            if f.name.endswith(".yaml") and f.name not in ("template.yaml",):
                slug = f.name[:-5]
                raw  = base64.b64decode(f.content).decode("utf-8")
                members[slug] = yaml.safe_load(raw)
        return members
    except GithubException:
        return {}

def get_history(slug: str) -> list:
    try:
        f = get_repo().get_contents(f"history_{slug}.txt")
        raw = base64.b64decode(f.content).decode("utf-8")
        return [l for l in raw.splitlines() if l.strip()]
    except (GithubException, UnknownObjectException):
        return []

def default_tone():
    return [
        "友達に話しかけるような、自然でやわらかい口語調で書く",
        "話題のキーワードを使って共感を引き出す",
        "安心できる言葉を入れる",
        "最後は自然な誘導で締める",
        "「速報」「リアルタイム」のような仰々しい言葉は使わない",
        "URLやハッシュタグは不要",
        "過去投稿との重複を避ける",
    ]


# ─── 認証ヘルパー ─────────────────────────────────────────────────────────────

def logged_in():
    return "user" in session

def is_admin():
    return session.get("role") == "admin"

def require_login(slug=None):
    if not logged_in():
        return redirect(url_for("login"))
    if slug and not is_admin() and session["user"] != slug:
        flash("アクセス権限がありません", "danger")
        return redirect(url_for("index"))
    return None


# ─── ルーティング ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    if is_admin():
        return redirect(url_for("dashboard"))
    return redirect(url_for("member_edit", slug=session["user"]))


@app.route("/login", methods=["GET", "POST"])
def login():
    if logged_in():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # 管理者ログイン
        if username == "admin":
            if ADMIN_PASSWORD and password == ADMIN_PASSWORD:
                session.update(user="admin", role="admin")
                return redirect(url_for("dashboard"))
        else:
            config, _ = gh_read_yaml(f"users/{username}.yaml")
            if config:
                # 新方式: ユーワードPWで照合
                if verify_uword_pw(config, password):
                    session.update(user=username, role="member")
                    return redirect(url_for("member_edit", slug=username))
                # 旧方式: ハッシュ照合（後方互換）
                stored = config.get("auth", {}).get("password_hash", "")
                if stored and check_password_hash(stored, password):
                    session.update(user=username, role="member")
                    return redirect(url_for("member_edit", slug=username))

        flash("ユーザー名またはパスワードが違います", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── 会員自己登録 ───────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        member_no   = request.form.get("member_no", "").strip()
        uword_id    = request.form.get("uword_id", "").strip()
        uword_pw    = request.form.get("uword_pw", "").strip()
        user_path   = request.form.get("user_path", "").strip()
        name        = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        cta         = request.form.get("cta", "お気軽にご相談ください").strip()

        if not all([member_no, uword_id, uword_pw, user_path, name]):
            flash("すべての必須項目を入力してください", "danger")
            return render_template("register.html")

        import re
        slug = re.sub(r"[^0-9]", "", member_no)  # 数字のみ
        if not slug:
            flash("会員番号は数字のみ入力してください", "danger")
            return render_template("register.html")

        existing, _ = gh_read_yaml(f"users/{slug}.yaml")
        if existing:
            flash("このIDはすでに登録されています。担当者にお問い合わせください", "danger")
            return render_template("register.html")

        try:
            id_enc = encrypt_str(uword_id)
            pw_enc = encrypt_str(uword_pw)
        except RuntimeError as e:
            flash(f"暗号化エラー: {e}", "danger")
            return render_template("register.html")

        new_config = {
            "profile": {"name": name, "description": description, "cta": cta},
            "uword": {
                "user_path":   user_path,
                "credentials": {"id_encrypted": id_enc, "pw_encrypted": pw_enc},
            },
            "schedule": {"times": ["09:00", "21:00"], "timezone": "Asia/Tokyo"},
            "post": {
                "title_max": 30, "body_max": 140,
                "history_max": 10,
                "prefix": "【この投稿はAIで自動投稿しています】\n",
            },
            "rss":    {"feeds": ["https://news.google.com/rss/search?q=AI+人工知能&hl=ja&gl=JP&ceid=JP:ja"]},
            "ai":     {"model": "claude-haiku-4-5", "max_tokens": 300},
            "prompt": {"tone": default_tone()},
        }

        if gh_create_yaml(f"users/{slug}.yaml", new_config, f"feat: 新規会員登録 {slug}"):
            flash("登録が完了しました！ログインしてください", "success")
            return redirect(url_for("login"))
        else:
            flash("登録に失敗しました。しばらく経ってから再試行してください", "danger")

    return render_template("register.html")


# ── 管理者: 新規会員作成 ───────────────────────────────────────────────────

@app.route("/admin/new", methods=["GET", "POST"])
def admin_new():
    if r := require_login(): return r
    if not is_admin(): return redirect(url_for("index"))

    if request.method == "POST":
        import re
        member_no = re.sub(r"[^0-9]", "", request.form.get("member_no", "").strip())
        name      = request.form.get("name", "").strip()
        user_path = request.form.get("user_path", "").strip()
        uword_id  = request.form.get("uword_id", "").strip()
        uword_pw  = request.form.get("uword_pw", "").strip()
        slug      = member_no

        if not all([member_no, name, user_path, uword_id, uword_pw]):
            flash("すべての項目を入力してください", "danger")
            return render_template("admin_new.html")

        path = f"users/{slug}.yaml"
        existing, _ = gh_read_yaml(path)
        if existing:
            flash(f"スラグ '{slug}' はすでに使用されています", "danger")
            return render_template("admin_new.html")

        try:
            id_enc = encrypt_str(uword_id)
            pw_enc = encrypt_str(uword_pw)
        except RuntimeError as e:
            flash(f"暗号化エラー: {e}", "danger")
            return render_template("admin_new.html")

        new_config = {
            "profile": {
                "name":        name,
                "description": request.form.get("description", ""),
                "cta":         request.form.get("cta", "お気軽にご相談ください"),
            },
            "uword": {
                "user_path":   user_path,
                "credentials": {"id_encrypted": id_enc, "pw_encrypted": pw_enc},
            },
            "schedule": {"times": ["09:00", "21:00"], "timezone": "Asia/Tokyo"},
            "post": {
                "title_max": 30, "body_max": 140,
                "history_max": 10,
                "prefix": "【この投稿はAIで自動投稿しています】\n",
            },
            "rss":    {"feeds": ["https://news.google.com/rss/search?q=AI+人工知能&hl=ja&gl=JP&ceid=JP:ja"]},
            "ai":     {"model": "claude-haiku-4-5", "max_tokens": 300},
            "prompt": {"tone": default_tone()},
        }

        if gh_create_yaml(path, new_config, f"feat: 新規会員 {slug} を追加"):
            flash(f"会員 '{slug}' を作成しました", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("作成に失敗しました", "danger")

    return render_template("admin_new.html")


# ── 管理者: ダッシュボード ──────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if r := require_login(): return r
    if not is_admin(): return redirect(url_for("index"))
    members = get_all_members()
    return render_template("dashboard.html", members=members)


# ── 管理者: 会員削除 ────────────────────────────────────────────────────────

@app.route("/admin/member/<slug>/delete", methods=["POST"])
def admin_delete_member(slug):
    if r := require_login(): return r
    if not is_admin(): return redirect(url_for("index"))

    repo = get_repo()
    deleted = []
    for path in [f"users/{slug}.yaml", f"history_{slug}.txt"]:
        try:
            f = repo.get_contents(path)
            repo.delete_file(path, f"chore: 会員 {slug} を削除", f.sha)
            deleted.append(path)
        except UnknownObjectException:
            pass
        except GithubException as e:
            flash(f"{path} の削除に失敗しました: {e}", "danger")
            return redirect(url_for("dashboard"))

    flash(f"会員 '{slug}' を削除しました（{', '.join(deleted)}）", "success")
    return redirect(url_for("dashboard"))


# ── 管理者: 認証情報リセット ───────────────────────────────────────────────

@app.route("/admin/member/<slug>/reset-credentials", methods=["POST"])
def admin_reset_credentials(slug):
    if r := require_login(): return r
    if not is_admin(): return redirect(url_for("index"))

    uword_id = request.form.get("uword_id", "").strip()
    uword_pw = request.form.get("uword_pw", "").strip()
    if not uword_id or not uword_pw:
        flash("IDとパスワードを入力してください", "danger")
        return redirect(url_for("dashboard"))

    config, sha = gh_read_yaml(f"users/{slug}.yaml")
    if not config:
        flash("設定ファイルが見つかりません", "danger")
        return redirect(url_for("dashboard"))

    try:
        config.setdefault("uword", {})["credentials"] = {
            "id_encrypted": encrypt_str(uword_id),
            "pw_encrypted": encrypt_str(uword_pw),
        }
    except RuntimeError as e:
        flash(f"暗号化エラー: {e}", "danger")
        return redirect(url_for("dashboard"))

    if gh_write_yaml(f"users/{slug}.yaml", config, sha, f"chore: {slug} の認証情報を更新"):
        flash(f"{slug} の認証情報を更新しました", "success")
    else:
        flash("保存に失敗しました", "danger")

    return redirect(url_for("dashboard"))


# ── 会員: 設定編集 ─────────────────────────────────────────────────────────

@app.route("/member/<slug>/edit", methods=["GET", "POST"])
def member_edit(slug):
    if r := require_login(slug): return r

    config, sha = gh_read_yaml(f"users/{slug}.yaml")
    if not config:
        flash("設定ファイルが見つかりません", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        config["profile"]["name"]        = request.form.get("profile_name", "")
        config["profile"]["description"] = request.form.get("profile_description", "")
        config["profile"]["cta"]         = request.form.get("profile_cta", "")

        times_raw = request.form.get("schedule_times", "")
        config["schedule"]["times"] = [t.strip() for t in times_raw.split(",") if t.strip()]

        config["post"]["prefix"] = request.form.get("post_prefix", "")

        feeds_raw = request.form.get("rss_feeds", "")
        config["rss"]["feeds"] = [f.strip() for f in feeds_raw.splitlines() if f.strip()]

        tone_raw = request.form.get("prompt_tone", "")
        config["prompt"]["tone"] = [t.strip() for t in tone_raw.splitlines() if t.strip()]

        # 管理者のみ
        if is_admin():
            config["ai"]["model"]        = request.form.get("ai_model", "claude-haiku-4-5")
            config["uword"]["user_path"]  = request.form.get("uword_user_path", "")

        if gh_write_yaml(f"users/{slug}.yaml", config, sha, f"chore: {slug} の設定を更新"):
            flash("設定を保存しました", "success")
            return redirect(url_for("member_edit", slug=slug))
        else:
            flash("保存に失敗しました", "danger")

    return render_template("member_edit.html", slug=slug, config=config, is_admin=is_admin())


# ── 会員: 投稿履歴 ──────────────────────────────────────────────────────────

@app.route("/member/<slug>/history")
def member_history(slug):
    if r := require_login(slug): return r
    entries = get_history(slug)
    return render_template("member_history.html", slug=slug, entries=entries)


# ─── エントリポイント ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
