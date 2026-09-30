#!/usr/bin/env python3
"""金沢大学 WebClass 講義資料ダウンローダー

履修中の科目の「資料」から PDF を探して、科目ごとのフォルダに保存します。

- ログインはブラウザ画面で自分で行います（パスワードは一切保存しません）。
- ログイン状態（クッキー）だけを ~/.webclass-downloader/ に保存し、次回に使います。
- WebClass へのアクセスは 1 件ずつ、間隔をあけて順番に行います。
- 資料の閲覧とダウンロード以外の操作（課題提出・アンケート回答など）はしません。

使い方は README.md を見てください。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright が見つかりません。README.md の「準備」の手順を先に行ってください。")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# WEBCLASS_DL_BASE_URL はテスト用（普段は設定しない）
BASE_URL = os.environ.get("WEBCLASS_DL_BASE_URL", "https://lms-wc.el.kanazawa-u.ac.jp").rstrip("/")
WEBCLASS_URL = BASE_URL + "/webclass/"
INDEX_URL = WEBCLASS_URL + "index.php"
# WebClass の login.php は管理者用なので、ログインはアカンサスポータルから行う
PORTAL_URL = os.environ.get("WEBCLASS_DL_PORTAL_URL", "https://acanthus.cis.kanazawa-u.ac.jp/")
WEBCLASS_HOST = urlparse(BASE_URL).netloc

DEFAULT_SAVE_DIR = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "講義資料"
APP_DIR = Path(os.environ.get("WEBCLASS_DL_APP_DIR", str(Path.home() / ".webclass-downloader")))
STATE_FILE = APP_DIR / "login_state.json"  # ログイン状態（クッキー）。パスワードは入らない
HISTORY_FILE = APP_DIR / "history.json"  # ダウンロード済みの資料の記録
DEBUG_DIR = APP_DIR / "debug"  # うまくいかなかったときの画面の保存先

# 目次のリンクをたどるときに、これらの語を含む URL には行かない（念のため）
UNSAFE_URL_WORDS = ("logout", "submit", "answer", "delete", "finish", "exit", "end_", "/end", "save")


class SessionLostError(Exception):
    """ログイン状態が切れた・ログアウト画面に飛ばされたとき"""


# ---------------------------------------------------------------------------
# 小さな道具
# ---------------------------------------------------------------------------


class Pacer:
    """アクセスとアクセスの間に、必ず一定の時間をあけるための道具"""

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        remain = self.interval - (time.monotonic() - self._last)
        if remain > 0:
            time.sleep(remain)
        self._last = time.monotonic()


@dataclass
class Course:
    course_id: str
    name: str
    url: str

    @property
    def folder_name(self) -> str:
        return course_folder_name(self.name)


@dataclass
class Material:
    course: Course
    contents_id: str
    name: str

    @property
    def do_contents_url(self) -> str:
        return WEBCLASS_URL + f"do_contents.php?reset_status=1&set_contents_id={self.contents_id}"


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def sanitize_filename(name: str, max_bytes: int = 200) -> str:
    """Mac で使えるファイル名に整える（★を取り除く・使えない記号を置き換える）"""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("★", "")
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', "_", name)
    name = normalize_space(name).strip(". ")
    if not name:
        name = "無題"
    stem, ext = os.path.splitext(name)
    if len(ext) > 10:  # 拡張子らしくないものは本体扱い
        stem, ext = name, ""
    while len((stem + ext).encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
    return (stem.rstrip(". ") or "無題") + ext


def course_folder_name(course_name: str) -> str:
    """「感染症学(Q3)(41165) (2026-通年-集中)」→「感染症学」"""
    m = re.search(r"\s*[(（]\s*(Q\d|\d{4,})", course_name)
    base = course_name[: m.start()] if m else course_name
    return sanitize_filename(base.strip() or course_name)


def course_terms(course_name: str) -> set[int]:
    """科目名の (Q3) や (Q3Q4)・(Q1-Q2) などから、開講クォーターの番号を取り出す"""
    terms: set[int] = set()
    for group in re.findall(r"[(（]\s*(Q[^()（）]*)[)）]", course_name):
        g = unicodedata.normalize("NFKC", group)
        for a, b in re.findall(r"Q?(\d)\s*[-~〜]\s*Q?(\d)", g):
            terms.update(range(int(a), int(b) + 1))
        terms.update(int(d) for d in re.findall(r"Q?(\d)", g))
    return {t for t in terms if 1 <= t <= 4}


def course_year(course_name: str) -> int | None:
    m = re.search(r"[(（]\s*(\d{4})\s*-", course_name)
    return int(m.group(1)) if m else None


def guess_current_term(today: datetime.date) -> tuple[int, int]:
    """今日の日付から (年度, クォーター) を推測する"""
    year = today.year if today.month >= 4 else today.year - 1
    if today.month in (4, 5):
        q = 1
    elif today.month in (6, 7):
        q = 2
    elif today.month in (8, 9, 10, 11):
        q = 3
    else:
        q = 4
    return year, q


def parse_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        charset = m.group(1) or "utf-8"
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=charset)
        except LookupError:
            return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]*)"', value, re.I) or re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        raw = m.group(1).strip()
        try:  # UTF-8 のバイト列が latin-1 として読まれている場合を直す
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return unquote(raw)
    return None


def looks_like_pdf(body: bytes) -> bool:
    return b"%PDF-" in body[:1024]


def redact_url(text: str) -> str:
    """URL の中のセッションに関わる値（acs_ など）を *** に置き換える"""
    return re.sub(r"((?:acs_|sid|session|token|PHPSESSID)[^=&\s]*=)[^&\s|]*", r"\1***", text, flags=re.I)


class FetchedFile:
    """ブラウザの fetch で取ってきたファイル"""

    def __init__(self, url: str, ctype: str, disposition: str, body: bytes):
        self.url = url
        self.headers = {"content-type": ctype, "content-disposition": disposition}
        self._body = body

    def body(self) -> bytes:
        return self._body

    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.headers["content-type"], re.I)
        try:
            return self._body.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            return self._body.decode("utf-8", errors="replace")


class LinkCollector(HTMLParser):
    """HTML から <a href> を集める"""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, normalize_space("".join(self._text))))
            self._href = None


def ask_yes_no(question: str) -> bool:
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes", "ｙ")


# ---------------------------------------------------------------------------
# 記録（ダウンロード済みの資料）
# ---------------------------------------------------------------------------


class History:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"materials": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("materials", {})
            except (OSError, ValueError):
                print(f"※ 記録ファイル {path} が読めなかったので、新しく作り直します。")

    def is_done(self, material: Material) -> bool:
        return material.contents_id in self.data["materials"]

    def mark_done(self, material: Material, files: list[str]) -> None:
        self.data["materials"][material.contents_id] = {
            "course": material.course.name,
            "name": material.name,
            "files": files,
            "downloaded_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# ブラウザ操作
# ---------------------------------------------------------------------------


class WebClassDownloader:
    def __init__(self, context, page, args, pacer: Pacer, history: History, save_dir: Path):
        self.context = context
        self.page = page
        self.args = args
        self.pacer = pacer
        self.history = history
        self.save_dir = save_dir
        self.allow_dialog = False
        self.fetch_page = None
        self.material_digests: dict[str, Path] = {}
        self.saved_files: list[Path] = []
        self.skipped_files: list[str] = []
        self.failed: list[str] = []
        context.on("page", self._setup_page)
        for p in context.pages:
            self._setup_page(p)

    # --- 共通 ---

    def _setup_page(self, page) -> None:
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        # 「開始」ボタンを押した直後の確認と、「このページを離れますか？」だけ OK する。
        # それ以外は全部キャンセルする
        if self.allow_dialog or dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()

    def goto(self, page, url: str) -> None:
        self.pacer.wait()
        try:
            page.goto(url, wait_until="load", timeout=60000)
        except PlaywrightError as e:
            # 前の画面の処理と重なって移動が中断されることがあるので、少し待って 1 回だけやり直す
            if "ERR_ABORTED" not in str(e) and "interrupted" not in str(e):
                raise
            time.sleep(3)
            self.pacer.wait()
            page.goto(url, wait_until="load", timeout=60000)
        self.check_session(page)

    def check_session(self, page) -> None:
        u = urlparse(page.url)
        path = u.path.lower()
        if (
            u.netloc != WEBCLASS_HOST
            or "logout" in path
            or path.endswith("/login.php")
            or path.rstrip("/").endswith("/webclass")
        ):
            raise SessionLostError(page.url)

    def dump_debug(self, page, label: str) -> None:
        """うまくいかなかったときに、画面の HTML を保存しておく（原因調べ用）"""
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w.-]", "_", label)[:60]
        try:
            for i, frame in enumerate(page.frames):
                path = DEBUG_DIR / f"{stamp}_{safe}_frame{i}.html"
                path.write_text(f"<!-- {frame.url} -->\n" + frame.content(), encoding="utf-8")
            print(f"    （調査用に画面を保存しました: {DEBUG_DIR}）")
        except PlaywrightError:
            pass

    def write_report(self, page, label: str) -> None:
        """画面の作り（リンク・埋め込み・ボタンなど）の要点を report.txt に書き出す。
        氏名などが入りにくいよう、URL の中の acs_ などは *** に置き換える"""
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [f"===== {label} ====="]
        for i, frame in enumerate(page.frames):
            try:
                info = frame.evaluate(
                    """() => {
                        const t = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
                        const out = [];
                        document.querySelectorAll('a').forEach(a => out.push(
                            'a  href=' + (a.getAttribute('href') || '') + ' | onclick=' + t(a.getAttribute('onclick')) + ' | ' + t(a.innerText)));
                        document.querySelectorAll('iframe,frame,embed,object').forEach(e => out.push(
                            e.tagName.toLowerCase() + '  src=' + (e.getAttribute('src') || e.getAttribute('data') || '')));
                        document.querySelectorAll('button,input[type=button],input[type=submit]').forEach(b => out.push(
                            'button  ' + t(b.innerText || b.value) + ' | onclick=' + t(b.getAttribute('onclick'))));
                        document.querySelectorAll('[onclick]').forEach(e => {
                            if (!['A', 'BUTTON', 'INPUT'].includes(e.tagName)) out.push(
                                e.tagName.toLowerCase() + '.' + t(e.className) + '  onclick=' + t(e.getAttribute('onclick')) + ' | ' + t(e.innerText));
                        });
                        return {title: document.title, items: out.slice(0, 150)};
                    }"""
                )
            except PlaywrightError:
                continue
            lines.append(f"--- frame{i}: {redact_url(frame.url)} ({info['title']})")
            lines.extend(redact_url(x) for x in info["items"])
        with open(DEBUG_DIR / "report.txt", "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")

    # --- ログイン ---

    def on_course_list(self, page) -> bool:
        if urlparse(page.url).netloc != WEBCLASS_HOST:
            return False
        try:
            return page.locator('a[href*="/course.php/"]').count() > 0
        except PlaywrightError:
            return False

    def ensure_login(self) -> None:
        if STATE_FILE.exists():
            self.goto_index_quietly(self.page)
            if self.on_course_list(self.page):
                print("前回のログイン状態でWebClassに入れました。")
                return

        # WebClass の login.php（メンテナンス用）ではなく、アカンサスポータルを開く
        self.pacer.wait()
        try:
            self.page.goto(PORTAL_URL, wait_until="load", timeout=60000)
        except PlaywrightError:
            pass

        print()
        print("=" * 60)
        print("開いたブラウザ（アカンサスポータル）で、金沢大学IDでログインしてください。")
        print("（多要素認証もブラウザ上で行ってください）")
        print("ログインできたら、ポータルの「時間割」などから WebClass を開いてください。")
        print("WebClass の画面が表示されたら、ターミナルに戻って Enter を押します。")
        print()
        print("※「メンテナンス用のログイン画面です」と書かれた WebClass の画面には")
        print("  パスワードを入力しないでください（そこからはログインできません）。")
        print("=" * 60)
        while True:
            try:
                input("\nWebClass が表示されたら、ここで Enter キーを押してください（やめるときは Ctrl+C）: ")
            except EOFError:
                raise KeyboardInterrupt
            # ポータルから開いた WebClass は別のタブになっていることが多い。
            # ログインできたかは、新しいタブでコース一覧を開いて確かめる
            check = self.context.new_page()
            self.goto_index_quietly(check)
            if self.on_course_list(check):
                # 使うタブを 1 つにまとめる（同時に複数の画面で操作しないため）
                for p in list(self.context.pages):
                    if p is not check:
                        p.close()
                self.page = check
                print("ログインを確認できました。")
                return
            check.close()
            print("まだ WebClass にログインできていないようです。")
            print("ポータルにログインしたあと、ポータルの中から WebClass を開いてから Enter を押してください。")

    def goto_index_quietly(self, page) -> None:
        self.pacer.wait()
        try:
            page.goto(INDEX_URL, wait_until="load", timeout=60000)
        except PlaywrightError:
            pass

    def save_login_state(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(STATE_FILE))
        os.chmod(STATE_FILE, 0o600)

    # --- コース一覧 ---

    def get_courses(self) -> list[Course]:
        self.goto(self.page, INDEX_URL)
        links = self.page.eval_on_selector_all(
            'a[href*="/course.php/"]',
            "els => els.map(a => ({href: a.href, text: a.innerText || a.textContent || ''}))",
        )
        courses: dict[str, Course] = {}
        for link in links:
            m = re.search(r"/course\.php/([^/?#]+)/login", link["href"])
            # 科目名の前に付いている「»」などの記号を取る
            name = re.sub(r"^[»›>▶▸・\s]+", "", normalize_space(link["text"]))
            if not m or not name or m.group(1) in courses:
                continue
            course_id = m.group(1)
            courses[course_id] = Course(course_id, name, WEBCLASS_URL + f"course.php/{course_id}/login")
        return list(courses.values())

    def open_course(self, course: Course) -> None:
        self.goto(self.page, course.url)
        # JavaScript で ?acs_=... 付きのコースページに移動するのを待つ
        if "acs_" not in self.page.url:
            try:
                self.page.wait_for_url(re.compile(r"acs_="), timeout=15000)
            except PlaywrightTimeoutError:
                pass
            self.page.wait_for_load_state("load")
        self.check_session(self.page)

    def list_materials(self, course: Course) -> list[Material]:
        self.open_course(course)
        items = self.page.eval_on_selector_all(
            "section[data-contents-id]",
            """els => els.map(s => {
                const label = s.querySelector('.cl-contentsList_categoryLabel');
                return {
                    id: s.getAttribute('data-contents-id'),
                    name: s.getAttribute('data-contents-name') || '',
                    category: label ? (label.innerText || label.textContent || '') : '',
                    available: !!s.querySelector('a[href*="do_contents"]'),
                };
            })""",
        )
        materials = []
        for it in items:
            if normalize_space(it["category"]) != "資料" or not it["available"] or not it["id"]:
                continue
            materials.append(Material(course, it["id"], normalize_space(it["name"])))
        return materials

    # --- 資料を開く ---

    def open_material(self, material: Material):
        """資料ビューアを開いて、そのページ（タブ）を返す"""
        self.open_course(material.course)  # 先にそのコースを開いておく必要がある
        self.goto(self.page, material.do_contents_url)

        # show_info.php（「開始」ボタンの画面）は、画面全体のこともあれば、
        # show_frame.php の枠（フレーム）の中にあることもある
        info_frame = self.find_info_frame(self.page)
        if info_frame is not None:
            start = info_frame.locator(
                "input[type=submit][value*='開始'], input[type=button][value*='開始'], "
                "button:has-text('開始'), a:has-text('開始')"
            ).first
            if start.count() == 0:
                raise RuntimeError("「開始」ボタンが見つかりませんでした")
            before = list(self.context.pages)
            self.pacer.wait()
            self.allow_dialog = True
            try:
                start.click()
                # 「開始」の画面から資料の画面に切り替わるのを待つ（最大 30 秒）
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    time.sleep(1)
                    if len(self.context.pages) > len(before) or self.find_info_frame(self.page) is None:
                        break
                try:
                    self.page.wait_for_load_state("load", timeout=30000)
                except PlaywrightTimeoutError:
                    pass
                time.sleep(1.5)
            finally:
                self.allow_dialog = False
            new_pages = [p for p in self.context.pages if p not in before]
            if new_pages:  # 別ウインドウで開いた場合
                viewer = new_pages[-1]
                viewer.wait_for_load_state("load")
                self.check_session(viewer)
                return viewer
            self.check_session(self.page)
        return self.page

    def find_info_frame(self, page):
        """「開始」ボタンのある show_info.php の画面（または枠）を探す"""
        try:
            for frame in page.frames:
                if "show_info.php" in frame.url:
                    return frame
        except PlaywrightError:
            pass
        return None

    def scan_viewer(self, page, wait_seconds: float = 15.0) -> dict:
        """ビューアの中から、添付資料のリンク・埋め込み PDF・目次のリンクを探す"""
        deadline = time.monotonic() + wait_seconds
        while True:
            result = {"files": [], "embeds": [], "direct": [], "links": []}
            for frame in page.frames:
                if "loadit.php" in frame.url:
                    result["embeds"].append(frame.url)
                try:
                    found = frame.evaluate(
                        """() => {
                            const abs = u => new URL(u, location.href).href;
                            const r = {files: [], embeds: [], direct: [], links: []};
                            // a.href は相対パスを解決した後の URL
                            document.querySelectorAll('a[href]').forEach(a => {
                                const h = a.href;
                                if (h.includes('file_down.php')) r.files.push(h);
                                else if (h.includes('download.php')) r.direct.push(h);
                                else if (h.includes('mbl.php/textbooks')) r.links.push(h);
                            });
                            document.querySelectorAll(
                                'iframe[src*="loadit.php"], frame[src*="loadit.php"], embed[src*="loadit.php"], object[data*="loadit.php"]'
                            ).forEach(e => r.embeds.push(abs(e.getAttribute('src') || e.getAttribute('data'))));
                            return r;
                        }"""
                    )
                except PlaywrightError:
                    continue
                for key in result:
                    result[key].extend(found[key])
            for key in result:
                result[key] = list(dict.fromkeys(result[key]))
            if result["files"] or result["embeds"] or result["direct"] or time.monotonic() > deadline:
                return result
            time.sleep(1)

    def chapter_links(self, page, links: list[str], visited: set[str]) -> list[str]:
        """目次（節）へのリンクだけを選ぶ"""
        out = []
        for url in links:
            base = url.split("#")[0]
            u = urlparse(base)
            if u.netloc != WEBCLASS_HOST or "/mbl.php/textbooks" not in u.path:
                continue
            if any(w in base.lower() for w in UNSAFE_URL_WORDS):
                continue
            if base in visited:
                continue
            out.append(base)
        return out

    # --- ダウンロード ---

    def fetch(self, url: str, referer: str | None = None) -> "FetchedFile":
        """ブラウザの中から fetch で取得する（ログイン状態のまま・ブラウザと同じ通信方式で）"""
        self.pacer.wait()
        page = self.fetch_page
        if page is None or page.is_closed() or urlparse(page.url).netloc != WEBCLASS_HOST:
            page = self.page
        r = page.evaluate(
            """async ([url, referrer]) => {
                const opts = {credentials: 'include'};
                if (referrer) opts.referrer = referrer;
                const res = await fetch(url, opts);
                const buf = new Uint8Array(await res.arrayBuffer());
                let s = '';
                for (let i = 0; i < buf.length; i += 0x8000) {
                    s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
                }
                return {
                    ok: res.ok, status: res.status, url: res.url,
                    ctype: res.headers.get('content-type') || '',
                    disp: res.headers.get('content-disposition') || '',
                    body: btoa(s),
                };
            }""",
            [url, referer],
        )
        if not r["ok"]:
            raise RuntimeError(f"ダウンロードに失敗しました（HTTP {r['status']}）: {redact_url(url)}")
        return FetchedFile(r["url"], r["ctype"], r["disp"], base64.b64decode(r["body"]))

    def save_file(self, material: Material, filename: str, body: bytes) -> Path | None:
        """保存する。同じ名前で中身が同じファイルがあれば保存しない"""
        # 同じ資料の中で、中身がまったく同じファイルは 1 回だけ保存する
        digest = hashlib.sha256(body).hexdigest()
        if digest in self.material_digests:
            print(f"    = 同じ内容のファイルなので省略しました（{sanitize_filename(filename)}）")
            return self.material_digests[digest]
        folder = self.save_dir / material.course.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        name = sanitize_filename(filename)
        if looks_like_pdf(body) and not name.lower().endswith(".pdf"):
            name = sanitize_filename(name + ".pdf")
        stem, ext = os.path.splitext(name)
        path = folder / name
        n = 2
        while path.exists():
            if path.read_bytes() == body:
                print(f"    = 同じファイルがすでにあります: {path.name}")
                self.material_digests[digest] = path
                return path
            path = folder / f"{stem} ({n}){ext}"
            n += 1
        tmp = folder / (".download-" + name)
        tmp.write_bytes(body)
        tmp.replace(path)
        print(f"    ✓ 保存しました: {material.course.folder_name}/{path.name}")
        self.saved_files.append(path)
        self.material_digests[digest] = path
        return path

    def handle_body(self, material: Material, filename: str, resp) -> Path | None:
        body = resp.body()
        ctype = (resp.headers.get("content-type") or "").lower()
        if looks_like_pdf(body):
            return self.save_file(material, filename, body)
        if "text/html" in ctype:
            raise RuntimeError("ファイルではなく画面（HTML）が返ってきました。ログインが切れた可能性があります")
        if self.args.include_non_pdf:
            return self.save_file(material, filename, body)
        print(f"    - PDF ではないので飛ばしました: {filename}")
        self.skipped_files.append(f"{material.course.folder_name} / {filename}")
        return None

    def download_attachment(self, material: Material, file_down_url: str) -> list[Path]:
        """(A) 添付ファイル型：file_down.php のページの中にある download.php が本体"""
        saved = []
        resp = self.fetch(file_down_url)
        ctype = resp.headers.get("content-type").lower()
        if "text/html" not in ctype:
            name = parse_content_disposition(resp.headers.get("content-disposition")) or material.name
            p = self.handle_body(material, name, resp)
            return [p] if p else []
        parser = LinkCollector()
        parser.feed(resp.text())
        targets = [urljoin(file_down_url, h) for h, _ in parser.links if h and "download.php" in h]
        targets = list(dict.fromkeys(targets))
        if not targets:
            raise RuntimeError("添付資料のページにダウンロードリンクが見つかりませんでした")
        for url in targets:
            saved.extend(self.download_direct(material, url, referer=file_down_url))
        return saved

    def download_direct(self, material: Material, url: str, referer: str | None = None) -> list[Path]:
        resp = self.fetch(url, referer=referer)
        name = parse_content_disposition(resp.headers.get("content-disposition"))
        if not name:
            name = unquote(os.path.basename(urlparse(url).path)) or material.name
        p = self.handle_body(material, name, resp)
        return [p] if p else []

    def download_embed(self, material: Material, loadit_url: str, index: int, referer: str) -> list[Path]:
        """(B) 埋め込み型：loadit.php?file=... の file の場所を直接取りに行く"""
        qs = parse_qs(urlparse(loadit_url).query)
        file_path = (qs.get("file") or [""])[0]
        if not file_path:
            raise RuntimeError(f"埋め込み資料の場所が読み取れませんでした: {loadit_url}")
        url = urljoin(BASE_URL + "/", file_path)
        if urlparse(url).netloc != WEBCLASS_HOST:
            raise RuntimeError(f"WebClass 以外の場所を指しているので取得しません: {url}")
        ext = os.path.splitext(urlparse(url).path)[1] or ".pdf"
        name = material.name + (f"_{index}" if index > 1 else "") + ext
        resp = self.fetch(url, referer=referer)
        p = self.handle_body(material, name, resp)
        return [p] if p else []

    def download_material(self, material: Material) -> bool:
        viewer = self.open_material(material)
        self.fetch_page = viewer
        self.material_digests = {}
        try:
            first_url = viewer.url
            visited = {first_url.split("#")[0]}
            queue: list[str] = []
            seen_files: set[str] = set()
            seen_embeds: set[str] = set()
            saved: list[Path] = []
            embed_count = 0
            found_anything = False
            page_no = 1

            while True:
                result = self.scan_viewer(viewer)
                # 枠（フレーム）の中で開いているページも「見た」ことにする
                visited.update(f.url.split("#")[0] for f in viewer.frames)
                if self.args.debug:
                    self.dump_debug(viewer, f"{material.contents_id}_p{page_no}")
                for url in result["files"]:
                    if url not in seen_files:
                        seen_files.add(url)
                        found_anything = True
                        saved.extend(self.download_attachment(material, url))
                for url in result["direct"]:
                    if url not in seen_files:
                        seen_files.add(url)
                        found_anything = True
                        saved.extend(self.download_direct(material, url, referer=viewer.url))
                for url in result["embeds"]:
                    if url not in seen_embeds:
                        seen_embeds.add(url)
                        found_anything = True
                        embed_count += 1
                        saved.extend(self.download_embed(material, url, embed_count, referer=viewer.url))

                # 目次の他の節もたどる
                for url in self.chapter_links(viewer, result["links"], visited):
                    if url not in queue:
                        queue.append(url)
                if not queue or page_no >= 50:
                    break
                next_url = queue.pop(0)
                visited.add(next_url)
                page_no += 1
                self.goto(viewer, next_url)

            if not found_anything:
                print("    ! この資料の中にファイルが見つかりませんでした（次回もう一度確認します）")
                self.dump_debug(viewer, f"{material.contents_id}_notfound")
                self.write_report(viewer, f"ファイルなし: {material.course.folder_name} / {material.name}")
                return False
            self.history.mark_done(material, [str(p) for p in dict.fromkeys(saved)])
            return True
        finally:
            if viewer is not self.page:
                viewer.close()


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="金沢大学 WebClass から講義資料（PDF）をまとめてダウンロードします。",
    )
    parser.add_argument("--course", help="科目名の一部を指定すると、その科目だけを対象にします（例: 感染症学）")
    parser.add_argument("--term", help="対象のクォーター（例: Q3）。all ですべての科目。省略すると今日の日付から推測します")
    parser.add_argument("--year", type=int, help="対象の年度（例: 2026）。省略すると今日の日付から推測します")
    parser.add_argument("--save-dir", type=Path, default=DEFAULT_SAVE_DIR, help="保存先のフォルダ")
    parser.add_argument("--interval", type=float, default=2.0, help="アクセスの間隔（秒）。1秒より短くはできません")
    parser.add_argument("--include-non-pdf", action="store_true", help="PDF 以外のファイル（pptx など）も保存する")
    parser.add_argument("--debug", action="store_true", help="調査用に、資料の画面を ~/.webclass-downloader/debug に保存する")
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # テスト用
    return parser.parse_args()


def select_courses(courses: list[Course], args) -> list[Course]:
    if args.course:
        key = unicodedata.normalize("NFKC", args.course)
        return [c for c in courses if key in unicodedata.normalize("NFKC", c.name)]

    year_guess, term_guess = guess_current_term(datetime.date.today())
    year = args.year or year_guess
    if args.term and args.term.lower() == "all":
        term = None
    elif args.term:
        m = re.search(r"\d", args.term)
        if not m:
            print(f"--term の指定が読み取れません: {args.term}（例: Q3）")
            sys.exit(1)
        term = int(m.group())
    else:
        term = term_guess
    print(f"対象: {year}年度 " + (f"Q{term}" if term else "全クォーター") + " の科目")

    selected = []
    for c in courses:
        cy = course_year(c.name)
        if cy is not None and cy != year:
            continue
        if term is not None and term not in course_terms(c.name):
            continue
        selected.append(c)
    return selected


def main() -> int:
    args = parse_args()
    args.interval = max(1.0, args.interval)
    save_dir: Path = args.save_dir.expanduser()

    if save_dir == DEFAULT_SAVE_DIR and not save_dir.parent.exists():
        print("iCloud Drive のフォルダが見つかりません。")
        print("Mac の「システム設定」→ Apple ID → iCloud で iCloud Drive がオンになっているか確認してください。")
        return 1

    APP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    history = History(HISTORY_FILE)
    report = DEBUG_DIR / "report.txt"
    if report.exists():
        report.unlink()
    pacer = Pacer(args.interval)

    with sync_playwright() as pw:
        launch_opts = {"headless": args.headless}
        exe = os.environ.get("WEBCLASS_DL_CHROMIUM")  # テスト用
        if exe:
            launch_opts["executable_path"] = exe
        try:
            browser = pw.chromium.launch(**launch_opts)
        except PlaywrightError as e:
            print("ブラウザを起動できませんでした。README.md の「準備」の手順（playwright install chromium）を確認してください。")
            print(f"（詳細: {str(e).splitlines()[0]}）")
            return 1

        context_opts = {"locale": "ja-JP"}
        if STATE_FILE.exists():
            context_opts["storage_state"] = str(STATE_FILE)
        context = browser.new_context(**context_opts)
        page = context.new_page()
        dl = WebClassDownloader(context, page, args, pacer, history, save_dir)

        try:
            # 1. ログイン
            dl.ensure_login()
            dl.save_login_state()

            # 2. 科目を選ぶ
            courses = select_courses(dl.get_courses(), args)
            if not courses:
                print("対象の科目が見つかりませんでした。--course や --term の指定を確認してください。")
                return 0
            print(f"\n対象の科目（{len(courses)}件）:")
            for c in courses:
                print(f"  ・{c.name}")

            # 3. 各科目の資料を 1 科目ずつ見て回る
            print("\n資料を探しています（1件ずつ順番に確認するので少し時間がかかります）...")
            new_materials: list[Material] = []
            for i, c in enumerate(courses, 1):
                print(f"  [{i}/{len(courses)}] {c.folder_name}", end="", flush=True)
                try:
                    mats = dl.list_materials(c)
                except SessionLostError:
                    raise
                except PlaywrightError as e:
                    print(f" … 確認できませんでした（{str(e).splitlines()[0]}）")
                    continue
                new = [m for m in mats if not history.is_done(m)]
                print(f" … 資料 {len(mats)}件（新しいもの {len(new)}件）")
                new_materials.extend(new)

            if not new_materials:
                print("\n新しい資料はありませんでした。")
                return 0

            # 4. 一覧を見せて確認する
            print(f"\n新しい資料が {len(new_materials)}件 見つかりました:")
            current = None
            for m in new_materials:
                if m.course is not current:
                    current = m.course
                    print(f"\n  【{current.folder_name}】")
                print(f"    ・{m.name.replace('★', '')}")
            print(f"\n保存先: {save_dir}")
            if not ask_yes_no("\nダウンロードしますか？ (y/n): "):
                print("ダウンロードせずに終了します。")
                return 0

            # 5. 1件ずつダウンロード
            print()
            for i, m in enumerate(new_materials, 1):
                print(f"[{i}/{len(new_materials)}] {m.course.folder_name} / {m.name.replace('★', '')}")
                try:
                    dl.download_material(m)
                except SessionLostError:
                    raise
                except (PlaywrightError, RuntimeError, OSError) as e:
                    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                    print(f"    ! うまくいきませんでした: {msg}")
                    dl.failed.append(f"{m.course.folder_name} / {m.name}")
                    try:
                        dl.dump_debug(dl.page, f"{m.contents_id}_error")
                        dl.write_report(dl.page, f"エラー: {m.course.folder_name} / {m.name}: {msg}")
                    except PlaywrightError:
                        pass

            dl.save_login_state()

        except SessionLostError as e:
            print("\n\nWebClass のログイン状態が切れたか、ログアウト画面に移動しました。")
            print(f"（移動先: {e}）")
            print("もう一度このツールを実行して、ログインし直してください。")
            print("ここまでにダウンロードした資料は記録されているので、次回は続きから進みます。")
            return 1
        except KeyboardInterrupt:
            print("\n\n中断しました。ここまでにダウンロードした資料は記録されています。")
            return 1
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass

    # 6. まとめ
    print("\n" + "=" * 60)
    print(f"保存したファイル: {len(dl.saved_files)}件")
    if dl.skipped_files:
        print(f"PDF ではないので飛ばしたファイル: {len(dl.skipped_files)}件（--include-non-pdf を付けると保存します）")
        for s in dl.skipped_files:
            print(f"  ・{s}")
    if dl.failed:
        print(f"うまくいかなかった資料: {len(dl.failed)}件（次回もう一度試します）")
        for s in dl.failed:
            print(f"  ・{s}")
    print(f"保存先: {save_dir}")
    if report.exists():
        print()
        print("うまくいかなかった資料の画面の作りを、次のファイルにまとめました。")
        print(f"  {report}")
        print("直すための手がかりになるので、次のコマンドで開いて中身を見せてください:")
        print(f"  open {report}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
