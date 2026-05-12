"""
北大树洞 API（只读）

Auth flow (following pkucli implementation):
  1. POST IAAA oauthlogin.do, redirUrl = /chapi/cas_iaaa_login?version=3&uuid=...&plat=web
  2. GET /chapi/cas_iaaa_login?version=3&uuid=...&plat=web&token=<iaaa_token>
     → response JSON contains pku_token (JWT) + pku_uid
  3. If API returns code 40002 → SMS verification required:
     POST /chapi/api/jwt_send_msg  → sends SMS
     POST /chapi/api/jwt_msg_verify {code} → returns new JWT
  All API calls: Authorization: Bearer <pku_token>, UUID: <uuid>
  API base: /chapi/api/v3/
"""

import json
import random
import time
import uuid as _uuid_mod
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

import requests

TREEHOLE_BASE = "https://treehole.pku.edu.cn"
CHAPI_BASE = f"{TREEHOLE_BASE}/chapi"
API_BASE = f"{CHAPI_BASE}/api/v3"
IAAA_BASE = "https://iaaa.pku.edu.cn/iaaa"
SESSION_FILE = Path(__file__).parent.parent / ".treehole_session.json"

_CLIENT_UUID = None


def _get_uuid() -> str:
    global _CLIENT_UUID
    if _CLIENT_UUID:
        return _CLIENT_UUID
    data = _load_session()
    if data.get("uuid"):
        _CLIENT_UUID = data["uuid"]
        return _CLIENT_UUID
    _CLIENT_UUID = f"Web_PKUHOLE_2.0.0_WEB_UUID_{_uuid_mod.uuid4()}"
    data["uuid"] = _CLIENT_UUID
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return _CLIENT_UUID


def _load_session() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_session(data: dict):
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TreeholeSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": f"{TREEHOLE_BASE}/web/",
            "Content-Type": "application/json",
        })
        self._jwt: str | None = None
        self._uuid = _get_uuid()
        self._load_jwt()

    def _load_jwt(self):
        data = _load_session()
        self._jwt = data.get("jwt")
        if self._jwt:
            self.session.headers["Authorization"] = f"Bearer {self._jwt}"
            self.session.headers["UUID"] = self._uuid

    def _save_jwt(self, jwt: str):
        data = _load_session()
        data["jwt"] = jwt
        data["uuid"] = self._uuid
        # persist all session cookies so SMS verify step can reuse same session state
        data["cookies"] = [
            {"name": c.name, "value": c.value, "domain": c.domain or "treehole.pku.edu.cn", "path": c.path}
            for c in self.session.cookies
        ]
        _save_session(data)
        self._jwt = jwt
        self.session.headers["Authorization"] = f"Bearer {jwt}"
        self.session.headers["UUID"] = self._uuid

    def _load_cookies(self):
        """Restore treehole session cookies from disk (for SMS verify step)."""
        data = _load_session()
        for c in data.get("cookies", []):
            self.session.cookies.set(c["name"], c["value"],
                                     domain=c.get("domain", ""), path=c.get("path", "/"))

    def login(self, username: str, password: str, otp: str = None,
              sms_code_getter=None) -> str:
        """
        Full IAAA → Treehole login.
        sms_code_getter: optional callable() → str, called if SMS verification needed.
        If None and SMS is required, raises RuntimeError with instructions.
        """
        from pku_session import _rsa_encrypt

        redir_url = (
            f"{TREEHOLE_BASE}/chapi/cas_iaaa_login"
            f"?version=3&uuid={quote(self._uuid)}&plat=web"
        )
        app_id = "PKU Helper"

        # Step 1: IAAA OAuth page → JSESSIONID
        self.session.get(
            f"{IAAA_BASE}/oauth.jsp",
            params={"appID": app_id, "appName": "北大树洞", "redirectUrl": redir_url},
            headers={"Content-Type": None},
        )

        # Step 2: RSA public key + encrypt password
        resp = self.session.get(
            f"{IAAA_BASE}/getPublicKey.do",
            headers={"Referer": f"{IAAA_BASE}/oauth.jsp", "Content-Type": None},
        )
        pub_key = resp.json()["key"]
        enc_password = _rsa_encrypt(pub_key, password)

        # Step 3: POST oauthlogin.do
        body = {
            "appid": app_id,
            "userName": username,
            "password": enc_password,
            "randCode": "",
            "smsCode": "",
            "otpCode": otp or "",
            "remTrustChk": "false",
            "redirUrl": redir_url,
        }
        resp = self.session.post(
            f"{IAAA_BASE}/oauthlogin.do",
            data=body,
            headers={
                "Referer": f"{IAAA_BASE}/oauth.jsp?appID={app_id}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        data = resp.json()
        if not data.get("success"):
            err = data.get("errors", {}).get("msg", data.get("errMsg", "登录失败"))
            raise RuntimeError(f"IAAA 登录失败: {err}")

        iaaa_token = data["token"]

        # Step 4: Treehole SSO callback → get pku_token JWT
        resp2 = self.session.get(
            f"{TREEHOLE_BASE}/chapi/cas_iaaa_login",
            params={"version": "3", "uuid": self._uuid, "plat": "web",
                    "token": iaaa_token, "_rand": str(random.random())},
            headers={"Content-Type": None},
            allow_redirects=True,
        )

        jwt = self._extract_jwt(resp2)
        if not jwt:
            raise RuntimeError(
                "树洞 SSO 回调未返回 token，可能接口有变化"
            )

        self._save_jwt(jwt)

        # Step 5: send SMS immediately — no other requests between login and send
        send_resp = self.session.post(f"{CHAPI_BASE}/api/jwt_send_msg", json={}, timeout=15)
        send_data = send_resp.json() if send_resp.content else {}
        self._save_cookies_after_sms()  # save _session right after send, before any other request

        if send_data.get("code") not in (20000, 40001):
            # Not sent and not "already sent" — unexpected error
            raise RuntimeError(f"短信发送失败: {send_data}")

        phone_mask = send_data.get("phone_mask", "")
        hint = f"短信验证码已发送至手机{phone_mask}" if phone_mask else "短信验证码已发送至绑定手机"

        if sms_code_getter is None:
            raise RuntimeError(
                f"树洞需要短信验证（{hint}）。\n"
                "短信发出后请立即运行：\n"
                "  python src/main.py login --treehole-sms <验证码>"
            )

        code = sms_code_getter(hint)
        return self.verify_sms(code)

        return self._jwt

    def _extract_jwt(self, resp) -> str:
        """Try to get JWT from response JSON, URL params, or cookie."""
        try:
            d = resp.json()
            if isinstance(d, dict):
                jwt = d.get("pku_token") or d.get("token") or d.get("jwt", "")
                if jwt:
                    return jwt
        except Exception:
            pass
        qs = parse_qs(urlparse(resp.url).query)
        jwt = (qs.get("token") or [""])[0]
        if jwt:
            return jwt
        return (self.session.cookies.get("pku_token", domain="treehole.pku.edu.cn")
                or self.session.cookies.get("pku_token", ""))

    def _do_sms_verify(self, sms_code_getter) -> str:
        """Send SMS and save session cookies immediately after. Optionally verify inline."""
        send_resp = self.session.post(
            f"{CHAPI_BASE}/api/jwt_send_msg",
            json={},
            timeout=15,
        )
        send_data = send_resp.json() if send_resp.content else {}
        sent_ok = send_data.get("code") == 20000
        # 40001 = "code not expired, don't resend" — still valid
        phone_mask = send_data.get("phone_mask", send_data.get("mobile_mask", ""))
        hint = f"短信验证码已发送至手机{phone_mask}" if phone_mask else "短信验证码已发送至绑定手机"

        # Save cookies IMMEDIATELY after SMS send — this _session is what the server expects
        self._save_cookies_after_sms()

        if sms_code_getter is None:
            raise RuntimeError(
                f"树洞需要短信验证（{hint}）。\n"
                "短信发出后请立即运行：\n"
                "  python src/main.py login --treehole-sms <验证码>"
            )

        code = sms_code_getter(hint)
        return self.verify_sms(code)

    def _save_cookies_after_sms(self):
        """Persist only treehole session cookies (XSRF-TOKEN and _session) right after SMS send."""
        data = _load_session()
        data["cookies"] = [
            {"name": c.name, "value": c.value,
             "domain": "treehole.pku.edu.cn", "path": c.path}
            for c in self.session.cookies
            if c.name in ("XSRF-TOKEN", "_session")
        ]
        _save_session(data)

    def verify_sms(self, code: str) -> str:
        """Submit SMS code using saved JWT + UUID (no cookie dependency, matches pkucli behavior)."""
        verify_resp = self.session.post(
            f"{CHAPI_BASE}/api/jwt_msg_verify",
            json={"valid_code": code},
            headers={
                "Authorization": f"Bearer {self._jwt}",
                "UUID": self._uuid,
            },
            timeout=15,
        )
        verify_data = verify_resp.json() if verify_resp.content else {}
        if not verify_data.get("success"):
            raise RuntimeError(f"短信验证失败 (code={verify_data.get('code')}): {verify_data.get('message', '')}")
        new_jwt = (verify_data.get("data") or {}).get("token") or verify_data.get("pku_token") or self._jwt
        self._save_jwt(new_jwt)
        return new_jwt

    def is_logged_in(self) -> bool:
        if not self._jwt:
            return False
        try:
            resp = self.session.get(
                f"{API_BASE}/hole/list_comments",
                params={"page": 1, "limit": 1, "comment_limit": 0, "comment_stream": 1},
                timeout=8,
            )
            d = resp.json() if resp.content else {}
            return resp.status_code == 200 and d.get("code") == 20000
        except Exception:
            return False

    def _api_get(self, path: str, **params):
        resp = self.session.get(
            f"{API_BASE}/{path.lstrip('/')}", params=params, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _check_response(self, data):
        if isinstance(data, dict):
            code = data.get("code", 20000)
            if code == 40002:
                raise RuntimeError(
                    "树洞需要短信验证码。请用 --sms 参数提供验证码后重试。"
                )
            if code != 20000:
                raise RuntimeError(f"树洞 API 错误 {code}: {data.get('msg', data.get('message', data))}")
        return data

    # ── Read-only API ──────────────────────────────────────────────────────────

    def list_holes(self, page: int = 1, limit: int = 25) -> list[dict]:
        data = self._check_response(
            self._api_get("hole/list_comments",
                          page=page, limit=limit, comment_limit=3, comment_stream=1)
        )
        return _extract_list(data)

    def get_hole(self, hole_id: int) -> dict:
        data = self._check_response(
            self._api_get("hole/one", pid=hole_id, comment_stream=1)
        )
        inner = data.get("data") if isinstance(data, dict) else data
        if isinstance(inner, dict) and "hole" in inner:
            return inner["hole"]
        return _extract_item(data)

    def get_floors(self, hole_id: int, page: int = 1, limit: int = 50) -> list[dict]:
        data = self._check_response(
            self._api_get("hole/one", pid=hole_id, comment_stream=1,
                          comment_page=page, comment_limit=limit)
        )
        inner = data.get("data") if isinstance(data, dict) else {}
        if isinstance(inner, dict) and "list" in inner:
            return inner["list"] if isinstance(inner["list"], list) else []
        item = _extract_item(data)
        comments = item.get("comment_list") or item.get("comments") or []
        return comments if isinstance(comments, list) else []

    def search(self, keyword: str, page: int = 1, limit: int = 25) -> list[dict]:
        data = self._check_response(
            self._api_get("hole/list", keyword=keyword, page=page, limit=limit)
        )
        return _extract_list(data)


def _extract_list(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Prefer data.list (actual treehole API structure)
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("list"), list):
            return inner["list"]
        if isinstance(inner, list):
            return inner
        for key in ("hole_list", "holes", "list", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _extract_item(data) -> dict:
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
        return data
    return {}


def format_hole(h: dict, brief: bool = True) -> str:
    hid = h.get("pid") or h.get("hole_id") or h.get("id", "?")
    text = h.get("text") or h.get("content") or ""
    if brief:
        short = text[:120].replace("\n", " ")
        if len(text) > 120:
            short += "…"
        text = short
    reply_cnt = h.get("reply") or h.get("reply_num") or h.get("comment_num") or 0
    ts = h.get("timestamp") or h.get("created_at") or ""
    if ts and str(ts).isdigit():
        ts = time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    return f"#{hid} [{ts}] 回复:{reply_cnt}  {text}"


def format_floor(f: dict) -> str:
    fid = f.get("cid") or f.get("floor_id") or f.get("id", "?")
    anon = f.get("name_tag") or f.get("anonyname") or f.get("name") or "洞主"
    text = (f.get("text") or f.get("content") or "").replace("\n", " ")
    ts = f.get("timestamp") or f.get("created_at") or ""
    if ts and str(ts).isdigit():
        ts = time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    return f"  [{fid}] {anon} {ts}: {text}"
