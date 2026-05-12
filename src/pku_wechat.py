"""
微信公众平台爬虫（参考 pkucli info-spider）

Auth flow (QR code, no IAAA):
  1. GET  mp.weixin.qq.com/                      → cookies
  2. POST /cgi-bin/bizlogin?action=startlogin    → start session
  3. GET  /cgi-bin/scanloginqrcode?action=getqrcode → QR image
  4. Poll /cgi-bin/scanloginqrcode?action=ask    → wait for scan (status 1 = done)
  5. POST /cgi-bin/bizlogin?action=login         → get redirect_url with token
  6. GET  /cgi-bin/home?t=home/index             → finalize session

API:
  Search accounts: GET /cgi-bin/searchbiz
  List articles:   GET /cgi-bin/appmsgpublish
  Scrape article:  GET mp.weixin.qq.com/s/<id>  → extract #js_content
"""

import base64
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

MP_BASE = "https://mp.weixin.qq.com"
SESSION_FILE = Path(__file__).parent.parent / ".wechat_session.json"
QR_IMAGE_FILE = Path(__file__).parent.parent / "wechat_qrcode.png"
ACCOUNTS_FILE = Path(__file__).parent.parent / ".wechat_accounts.json"

# 内置北大公众号列表（可被用户覆盖/扩充）
_DEFAULT_PKU_ACCOUNTS = [
    {"name": "北京大学",                     "fakeid": "MzA3OTE0MjQzMw==", "alias": "iPKU1898"},
    {"name": "北京大学招生办",               "fakeid": "MzA4NjEzNDYxMQ==", "alias": "gotopku1898"},
    {"name": "燕京学堂",                     "fakeid": "MzI1NTk2MDMwMA==", "alias": "yanjinginstitute"},
    {"name": "北大团委",                     "fakeid": "MjM5NTI3MDYyNQ==", "alias": "pkutuanwei"},
    {"name": "北大学生就业指导服务中心",     "fakeid": "MjM5NTA4MDQ4MA==", "alias": "pkujob"},
    {"name": "北大图书馆",                   "fakeid": "MjM5NTI4NzM1Mg==", "alias": "lib_pku"},
    {"name": "北京大学教务部",               "fakeid": "MzA4NjEzNDYxMg==", "alias": "pkujwb"},
    {"name": "北大研究生院",                 "fakeid": "MjM5NTI2NjQxMg==", "alias": "pkugraduate"},
    {"name": "北京大学信息门户",             "fakeid": "MjM5ODI2NjM3OQ==", "alias": "bdxxfw"},
    {"name": "北京大学校园卡",               "fakeid": "MzU3NjU1NDY2OQ==", "alias": ""},
    {"name": "北京大学百周年纪念讲堂",       "fakeid": "MzU2ODY1ODE1MQ==", "alias": "pkuhall"},
    {"name": "北京大学勺园中关新园官方服务号", "fakeid": "MzA3Mjk3Mzg0Nw==", "alias": "pkushaoyuanpkugv"},
    {"name": "北京大学医院",                 "fakeid": "MzI0MTA5NDE4Mg==", "alias": "pkuh_pku_edu_cn"},
    {"name": "北京大学学生会",               "fakeid": "MzA3MDAxMTIxMQ==", "alias": "pkustudentunion"},
]


def load_pku_accounts() -> list[dict]:
    """Load PKU account list; user file overrides defaults if present."""
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return list(_DEFAULT_PKU_ACCOUNTS)


def save_pku_accounts(accounts: list[dict]):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def add_pku_account(name: str, fakeid: str, alias: str = "") -> list[dict]:
    accounts = load_pku_accounts()
    for a in accounts:
        if a.get("fakeid") == fakeid:
            a["name"] = name
            if alias:
                a["alias"] = alias
            save_pku_accounts(accounts)
            return accounts
    accounts.append({"name": name, "fakeid": fakeid, "alias": alias})
    save_pku_accounts(accounts)
    return accounts


def remove_pku_account(fakeid: str) -> tuple[list[dict], bool]:
    accounts = load_pku_accounts()
    new = [a for a in accounts if a.get("fakeid") != fakeid]
    removed = len(new) < len(accounts)
    if removed:
        save_pku_accounts(new)
    return new, removed


def reset_pku_accounts():
    if ACCOUNTS_FILE.exists():
        ACCOUNTS_FILE.unlink()
    return list(_DEFAULT_PKU_ACCOUNTS)

_UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_WECHAT = (
    "Mozilla/5.0 (Linux; Android 10; GM1910 Build/QKQ1.190716.003) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.120 Mobile Safari/537.36 "
    "MicroMessenger/8.0.16.2040(0x2800103D)"
)


def _jitter(base_ms: int = 800):
    time.sleep(base_ms / 1000 * (0.5 + random.random()))


def _load_session() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_session(data: dict):
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class WechatSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _UA_BROWSER,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._token: str = ""
        self._load()

    def _load(self):
        data = _load_session()
        self._token = data.get("token", "")
        for c in data.get("cookies", []):
            self.session.cookies.set(c["name"], c["value"],
                                     domain=c.get("domain", ""), path=c.get("path", "/"))

    def _save(self):
        data = {
            "token": self._token,
            "cookies": [
                {"name": c.name, "value": c.value,
                 "domain": c.domain or "", "path": c.path}
                for c in self.session.cookies
            ],
        }
        _save_session(data)

    def _xhr_headers(self) -> dict:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{MP_BASE}/cgi-bin/home",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    # ── Login ─────────────────────────────────────────────────────────────────

    def login_qr(self, output_modes: list = None) -> str:
        """
        Step 1: Generate QR code and save session for polling.
        output_modes: list of 'terminal', 'file', 'base64' (default: ['terminal', 'file'])
        After displaying QR, raises RuntimeError with instructions to run --wechat-poll.
        """
        if output_modes is None:
            output_modes = ["terminal", "file"]

        fingerprint = f"{random.randint(10**17, 10**18 - 1)}"
        session_id = f"{random.randint(10**17, 10**18 - 1)}"

        # Step 1: homepage → cookies
        self.session.get(f"{MP_BASE}/", timeout=15)
        _jitter(400)

        # Step 2: start login
        self.session.post(
            f"{MP_BASE}/cgi-bin/bizlogin",
            params={"action": "startlogin"},
            data={"userlang": "zh_CN", "redirect_url": "", "login_type": "3",
                  "sessionid": session_id, "token": "", "lang": "zh_CN",
                  "f": "json", "ajax": "1"},
            headers={**self._xhr_headers(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        _jitter(400)

        # Step 3: get QR image
        resp = self.session.get(
            f"{MP_BASE}/cgi-bin/scanloginqrcode",
            params={"action": "getqrcode", "param_type": "4",
                    "sessionid": session_id, "token": "", "lang": "zh_CN",
                    "f": "json", "ajax": "1",
                    "random": f"{random.random():.10f}"},
            headers=self._xhr_headers(),
            timeout=15,
        )
        if not resp.content:
            raise RuntimeError("获取微信 QR 码失败（空响应）")

        qr_bytes = resp.content
        if "file" in output_modes:
            QR_IMAGE_FILE.write_bytes(qr_bytes)
            print(f"[QR] 二维码已保存至: {QR_IMAGE_FILE.resolve()}")
        if "base64" in output_modes:
            b64 = base64.b64encode(qr_bytes).decode("ascii")
            print(f"[QR-BASE64] data:image/png;base64,{b64}")
        if "terminal" in output_modes:
            _print_qr_terminal(qr_bytes)

        # Save session state for poll step
        data = _load_session()
        data["_pending_session_id"] = session_id
        data["_pending_fingerprint"] = fingerprint
        data["cookies"] = [
            {"name": c.name, "value": c.value,
             "domain": c.domain or "", "path": c.path}
            for c in self.session.cookies
        ]
        _save_session(data)

        print("[QR] 请用微信扫描二维码，扫码后在微信中点击「确认登录」")
        print("扫码完成后请立即运行：")
        print("  python src/main.py wechat-login --poll")
        return ""

    def poll_qr_login(self) -> str:
        """Step 2: Poll for QR scan result and complete login."""
        data = _load_session()
        session_id = data.get("_pending_session_id", "")
        if not session_id:
            raise RuntimeError("未找到待确认的二维码会话，请先运行 wechat-login 生成二维码")

        for i in range(30):
            resp = self.session.get(
                f"{MP_BASE}/cgi-bin/scanloginqrcode",
                params={"action": "ask", "sessionid": session_id, "token": "",
                        "lang": "zh_CN", "f": "json", "ajax": "1",
                        "random": f"{random.random():.10f}"},
                headers=self._xhr_headers(),
                timeout=15,
            )
            d = resp.json() if resp.content else {}
            status = d.get("status", 0)
            if status == 1:
                token = self._complete_login(session_id, data.get("_pending_fingerprint", ""))
                self._token = token
                self._save()
                return token
            elif status in (5, 6):
                raise RuntimeError("二维码已过期，请重新运行 wechat-login 生成新二维码")
            _jitter(2000)

        raise RuntimeError("轮询超时，请确认已在微信中点击「确认登录」")

    def _complete_login(self, session_id: str, fingerprint: str) -> str:
        resp = self.session.post(
            f"{MP_BASE}/cgi-bin/bizlogin",
            params={"action": "login"},
            data={"userlang": "zh_CN", "redirect_url": "", "cookie_forbidden": "0",
                  "cookie_cleaned": "0", "plugin_used": "0", "login_type": "3",
                  "token": "", "lang": "zh_CN", "f": "json", "ajax": "1"},
            headers={**self._xhr_headers(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        redirect_url = data.get("redirect_url", "")
        if not redirect_url:
            raise RuntimeError(f"登录失败，未获得 redirect_url: {data}")

        qs = parse_qs(urlparse(redirect_url).query)
        token = (qs.get("token") or [""])[0]
        if not token:
            raise RuntimeError(f"无法从 redirect_url 提取 token: {redirect_url}")

        # Finalize session
        self.session.get(f"{MP_BASE}/cgi-bin/home",
                         params={"t": "home/index", "token": token, "lang": "zh_CN"},
                         timeout=15)
        return token

    def is_logged_in(self) -> bool:
        if not self._token:
            return False
        try:
            resp = self.session.get(
                f"{MP_BASE}/cgi-bin/home",
                params={"t": "home/index", "token": self._token, "lang": "zh_CN"},
                timeout=10,
                allow_redirects=False,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── Warmup (required before search/articles) ─────────────────────────────

    def _warmup(self):
        """Visit home + editor pages in order before API calls (mimics browser)."""
        self.session.get(f"{MP_BASE}/cgi-bin/home",
                         params={"t": "home/index", "token": self._token, "lang": "zh_CN"},
                         timeout=15)
        _jitter(600)
        self.session.get(f"{MP_BASE}/cgi-bin/appmsg",
                         params={"t": "media/appmsg_edit", "action": "edit",
                                 "type": "10", "token": self._token, "lang": "zh_CN"},
                         timeout=15)
        _jitter(600)

    # ── Search accounts ───────────────────────────────────────────────────────

    def search_accounts(self, query: str, count: int = 5) -> list[dict]:
        self._warmup()
        _jitter(800)
        resp = self.session.get(
            f"{MP_BASE}/cgi-bin/searchbiz",
            params={"action": "search_biz", "token": self._token, "lang": "zh_CN",
                    "f": "json", "ajax": "1", "query": query, "count": count,
                    "begin": "0", "type": "1"},
            headers=self._xhr_headers(),
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        base = data.get("base_resp", {})
        if base.get("ret") != 0:
            raise RuntimeError(f"搜索失败 ret={base.get('ret')}: {base.get('err_msg', '')}")
        return data.get("list", [])

    # ── List articles ─────────────────────────────────────────────────────────

    def list_articles(self, fakeid: str, count: int = 20,
                      begin: int = 0, query: str = "") -> dict:
        """
        Returns {"articles": [...], "total_count": N}
        Each article: {title, link, digest, author_name, update_time, cover}
        """
        resp = self.session.get(
            f"{MP_BASE}/cgi-bin/appmsgpublish",
            params={"sub": "list", "search_field": "null", "begin": begin,
                    "count": count, "query": query, "fakeid": fakeid,
                    "type": "101_1", "free_publish_type": "1",
                    "sub_action": "list_ex", "token": self._token,
                    "lang": "zh_CN", "f": "json", "ajax": "1"},
            headers=self._xhr_headers(),
            timeout=30,
        )
        data = resp.json() if resp.content else {}
        base = data.get("base_resp", {})
        ret = base.get("ret", -1)
        if ret == 200003:
            raise RuntimeError("微信 session 已过期，请重新登录")
        if ret == 200013:
            raise RuntimeError("请求过于频繁，请稍后再试")
        if ret != 0:
            raise RuntimeError(f"获取文章失败 ret={ret}: {base.get('err_msg', '')}")

        # Three-layer nested JSON
        publish_page_str = data.get("publish_page", "{}")
        publish_page = json.loads(publish_page_str) if isinstance(publish_page_str, str) else publish_page_str
        total_count = publish_page.get("total_count", 0)
        publish_list = publish_page.get("publish_list", [])

        articles = []
        for item in publish_list:
            info_str = item.get("publish_info", "{}")
            info = json.loads(info_str) if isinstance(info_str, str) else info_str
            for art in info.get("appmsgex", []):
                articles.append({
                    "title": art.get("title", ""),
                    "link": art.get("link", ""),
                    "digest": art.get("digest", ""),
                    "author_name": art.get("author_name", ""),
                    "update_time": art.get("update_time", 0),
                    "cover": art.get("cover", ""),
                })
        self._save()
        return {"articles": articles, "total_count": total_count}

    # ── Scrape article to Markdown ────────────────────────────────────────────

    def scrape_article(self, url: str) -> str:
        """Fetch a WeChat article and return its main content as plain text."""
        resp = requests.get(url, headers={"User-Agent": _UA_WECHAT}, timeout=30)
        resp.raise_for_status()
        html = resp.text

        # Extract #js_content div
        content_html = _extract_js_content(html)
        if not content_html:
            content_html = html

        # Strip scripts and styles
        content_html = _strip_tags(content_html, "script")
        content_html = _strip_tags(content_html, "style")

        # Simple HTML → text
        text = re.sub(r"<[^>]+>", "", content_html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\n{3,}", "\n\n", text.strip())
        return text


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _extract_js_content(html: str) -> str:
    """Extract the content of <div id="js_content">...</div>."""
    marker = 'id="js_content"'
    idx = html.find(marker)
    if idx == -1:
        return ""
    # Backtrack to find opening <div
    start = html.rfind("<div", 0, idx)
    if start == -1:
        return ""
    depth = 0
    pos = start
    while pos < len(html):
        next_open = html.find("<div", pos)
        next_close = html.find("</div>", pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                return html[start:next_close + 6]
            pos = next_close + 6
    return html[start:]


def _strip_tags(html: str, tag: str) -> str:
    return re.sub(rf"<{tag}[\s\S]*?</{tag}>", "", html, flags=re.IGNORECASE)


# ── QR terminal rendering ─────────────────────────────────────────────────────

def _print_qr_terminal(image_bytes: bytes) -> None:
    try:
        import sys
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes)).convert("1")
        w, h = img.size
        scale = max(1, w // 40)
        new_w, new_h = w // scale, h // scale
        img = img.resize((new_w, new_h), Image.NEAREST)
        pixels = list(img.getdata())

        use_unicode = True
        try:
            "▀▄█".encode(sys.stdout.encoding or "utf-8")
        except (UnicodeEncodeError, LookupError):
            use_unicode = False

        print()
        if use_unicode:
            for row in range(0, new_h - 1, 2):
                line = ""
                for col in range(new_w):
                    top = pixels[row * new_w + col]
                    bot = pixels[(row + 1) * new_w + col] if row + 1 < new_h else 255
                    if top == 0 and bot == 0:
                        line += "█"
                    elif top == 0:
                        line += "▀"
                    elif bot == 0:
                        line += "▄"
                    else:
                        line += " "
                print(line)
        else:
            for row in range(new_h):
                line = "".join("#" if pixels[row * new_w + col] == 0 else " "
                               for col in range(new_w))
                print(line)
        print()
    except ImportError:
        print("[QR] 安装 pillow 可在终端显示二维码: pip install pillow")
    except Exception as e:
        print(f"[QR] 终端渲染失败: {e}")


# ── Formatters ────────────────────────────────────────────────────────────────

def format_account(a: dict) -> str:
    name = a.get("nickname", a.get("name", ""))
    fakeid = a.get("fakeid", "")
    alias = a.get("alias", "")
    sig = a.get("signature", "")[:60]
    parts = [f"  {name}  fakeid={fakeid}"]
    if alias:
        parts.append(f"  alias={alias}")
    if sig:
        parts.append(f"  {sig}")
    return "\n".join(parts)


def format_article(a: dict, idx: int = 0) -> str:
    title = a.get("title", "")
    author = a.get("author_name", "")
    ts = a.get("update_time", 0)
    date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
    link = a.get("link", "")
    digest = a.get("digest", "")[:80]
    line1 = f"  [{idx}] {title}"
    line2 = f"      {date} {author}".strip()
    line3 = f"      {digest}" if digest else ""
    line4 = f"      {link}"
    return "\n".join(filter(None, [line1, line2, line3, line4]))
