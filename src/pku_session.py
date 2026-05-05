import base64
import json
import random
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SESSION_FILE = Path(__file__).parent.parent / ".pku_session.json"
IAAA_BASE = "https://iaaa.pku.edu.cn/iaaa"
PORTAL_BASE = "https://portal.pku.edu.cn/portal2017"


def _rsa_encrypt(public_key_pem: str, plaintext: str) -> str:
    """Encrypt password with IAAA's RSA public key (PKCS1v15, base64url output)."""
    pub_key = serialization.load_pem_public_key(public_key_pem.encode())
    ciphertext = pub_key.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode("ascii")


class PKUSession:
    def __init__(self, app_id="portal2017", app_name="北京大学校内信息门户新版",
                 redir_url="https://portal.pku.edu.cn/portal2017/ssoLogin.do"):
        self.app_id = app_id
        self.app_name = app_name
        self.redir_url = redir_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        })
        self._load_cookies()

    def _load_cookies(self):
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                for cookie in data.get("cookies", []):
                    self.session.cookies.set(cookie["name"], cookie["value"],
                                             domain=cookie.get("domain", ""),
                                             path=cookie.get("path", "/"))
            except Exception:
                pass

    def _save_cookies(self):
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.session.cookies
        ]
        SESSION_FILE.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False, indent=2),
                                encoding="utf-8")

    def _rand(self):
        return str(random.random())

    def _get_public_key(self) -> str:
        resp = self.session.get(f"{IAAA_BASE}/getPublicKey.do",
                                headers={"Referer": f"{IAAA_BASE}/oauth.jsp"})
        return resp.json()["key"]

    def check_otp_required(self, username: str) -> dict:
        resp = self.session.get(f"{IAAA_BASE}/isMobileAuthen.do", params={
            "userName": username,
            "appId": self.app_id,
            "_rand": self._rand(),
        })
        data = resp.json()
        return {
            "otp_required": str(data.get("isMobileAuthen", "false")).lower() == "true",
            "mode": data.get("authenMode"),
            "mobile_mask": data.get("mobileMask"),
        }

    def login(self, username: str, password: str, otp: str = None) -> str:
        # First visit the login page to get JSESSIONID
        self.session.get(
            f"{IAAA_BASE}/oauth.jsp",
            params={"appID": self.app_id, "appName": self.app_name,
                    "redirectUrl": self.redir_url},
        )
        pub_key = self._get_public_key()
        encrypted_password = _rsa_encrypt(pub_key, password)

        body = {
            "appid": self.app_id,           # lowercase 'd' as browser sends
            "userName": username,
            "password": encrypted_password,
            "randCode": "",
            "smsCode": "",
            "otpCode": otp or "",
            "remTrustChk": "false",
            "redirUrl": self.redir_url,
        }

        resp = self.session.post(
            f"{IAAA_BASE}/oauthlogin.do",
            data=body,
            headers={"Referer": f"{IAAA_BASE}/oauth.jsp?appID={self.app_id}"},
        )
        data = resp.json()
        if not data.get("success"):
            err = data.get("errors", {}).get("msg", data.get("errMsg", "登录失败"))
            raise RuntimeError(f"IAAA login failed: {err}")

        token = data["token"]

        # Establish portal/service session
        self.session.get(
            self.redir_url,
            params={"rand": self._rand(), "token": token},
            allow_redirects=True,
        )

        self._save_cookies()
        return token

    def is_logged_in(self) -> bool:
        try:
            resp = self.session.post(
                f"{PORTAL_BASE}/account/retrBizCenterAll.do",
                timeout=10,
            )
            data = resp.json()
            return data.get("success") is True and "userName" in data
        except Exception:
            return False

    def ensure_login(self, username: str, password: str, otp: str = None) -> None:
        if self.is_logged_in():
            return
        otp_info = self.check_otp_required(username)
        if otp_info["otp_required"] and not otp:
            raise RuntimeError(
                f"账号需要手机动态令牌验证（尾号{otp_info['mobile_mask']}），"
                "请通过 --otp 参数提供当前6位动态口令"
            )
        self.login(username, password, otp)

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.session.post(url, **kwargs)

    @property
    def portal_session_cookie(self) -> str:
        return (self.session.cookies.get("SESSION", domain="portal.pku.edu.cn")
                or self.session.cookies.get("SESSION", ""))


class WprocSession(PKUSession):
    """Session for wproc.pku.edu.cn (bus reservation), uses separate IAAA appID."""

    WPROC_BASE = "https://wproc.pku.edu.cn"
    # IAAA validates redirUrl against a registered allowlist; this exact URL is registered for 'wproc'
    _WROPC_REDIR = ("https://wproc.pku.edu.cn/site/login/cas-login"
                    "?redirect_url=https%3A%2F%2Fwproc.pku.edu.cn%2Fv2%2F")

    def __init__(self):
        super().__init__(
            app_id="wproc",
            app_name="办事大厅预约版",
            redir_url=WprocSession._WROPC_REDIR,
        )

    def login(self, username: str, password: str, otp: str = None) -> str:
        self.session.get(
            f"{IAAA_BASE}/oauth.jsp",
            params={"appID": self.app_id, "appName": self.app_name,
                    "redirectUrl": self.redir_url},
        )
        pub_key = self._get_public_key()
        encrypted_password = _rsa_encrypt(pub_key, password)

        body = {
            "appid": self.app_id,
            "userName": username,
            "password": encrypted_password,
            "randCode": "",
            "smsCode": "",
            "otpCode": otp or "",
            "remTrustChk": "false",
            "redirUrl": self.redir_url,
        }

        resp = self.session.post(
            f"{IAAA_BASE}/oauthlogin.do",
            data=body,
            headers={"Referer": f"{IAAA_BASE}/oauth.jsp?appID={self.app_id}"},
        )
        data = resp.json()
        if not data.get("success"):
            err = data.get("errors", {}).get("msg", data.get("errMsg", "登录失败"))
            raise RuntimeError(f"IAAA login failed: {err}")

        token = data["token"]
        # CAS callback for wproc
        self.session.get(
            f"{self.WPROC_BASE}/site/login/cas-login",
            params={"token": token, "_rand": self._rand()},
            allow_redirects=True,
        )
        self._save_cookies()
        return token

    def is_logged_in(self) -> bool:
        try:
            resp = self.session.get(f"{self.WPROC_BASE}/site/user/auth", timeout=10)
            data = resp.json()
            return data.get("e") == 0 and data.get("d", {}).get("is_login") is True
        except Exception:
            return False
