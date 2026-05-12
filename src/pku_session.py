import base64
import json
import random
import sys
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SESSION_FILE = Path(__file__).parent.parent / ".pku_session.json"
IAAA_BASE = "https://iaaa.pku.edu.cn/iaaa"
PORTAL_BASE = "https://portal.pku.edu.cn/portal2017"
QR_IMAGE_FILE = Path(__file__).parent.parent / "qrcode.png"


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

    def get_qr_code(self) -> dict:
        """
        申请一个 QR Code 登录 token。
        返回 {"image_bytes": bytes}
        image_bytes 是 PNG 图片的原始字节，可直接写入文件或 base64 编码。
        调用后立即保存 IAAA Session cookie，使后续独立进程可继续轮询。
        """
        self.session.get(
            f"{IAAA_BASE}/oauth.jsp",
            params={"appID": self.app_id, "appName": self.app_name,
                    "redirectUrl": self.redir_url},
        )
        img_resp = self.session.get(
            "https://iaaa.pku.edu.cn/iaaa/genQRCode.do",
            params={"userName": "", "appId": self.app_id, "_rand": self._rand()},
        )
        if not img_resp.content:
            raise RuntimeError("QR Code 图片获取失败（空响应）")
        self._save_cookies()  # persist IAAA JSESSIONID so poll step can run separately
        return {"image_bytes": img_resp.content}

    def poll_qr_login(self, timeout: int = 180, interval: float = 3.0) -> str:
        """
        轮询 QR Code 扫码结果，直到成功或超时。
        返回 iaaa_token（成功后自动完成 Portal SSO 建立 Session）。
        timeout: 最长等待秒数（默认180秒，对应最多60次轮询）
        interval: 轮询间隔秒数
        raises RuntimeError: 超时或二维码过期
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.session.post(
                "https://iaaa.pku.edu.cn/iaaa/oauthlogin4QRCode.do",
                data={
                    "appId": "PKUApp",
                    "issuerAppId": "iaaa",
                    "targetAppId": self.app_id,
                    "redirectUrl": self.redir_url,
                },
                headers={"Referer": f"{IAAA_BASE}/oauth.jsp?appID={self.app_id}"},
            )
            data = resp.json()
            error_code = data.get("errors", {}).get("code", "")

            if data.get("success"):
                token = data["token"]
                self.session.get(
                    self.redir_url,
                    params={"rand": self._rand(), "token": token},
                    allow_redirects=True,
                )
                self._save_cookies()
                return token

            if error_code == "E10":
                time.sleep(interval)
                continue
            elif error_code == "E99" or data.get("isStop") == "是":
                raise RuntimeError("二维码已过期，请重新获取")
            else:
                time.sleep(interval)
                continue

        raise RuntimeError(f"扫码超时（{timeout}秒内未完成），请重新尝试")

    def login_with_qr(self, output_modes: list = None, poll: bool = True) -> str:
        """
        QR Code 登录流程：生成二维码 → 展示 → 可选轮询。
        output_modes: 输出方式列表，可包含 'terminal'、'file'、'base64'
                      默认 ['terminal', 'file']
        poll: True = 阻塞等待扫码完成（单进程模式）
              False = 生成二维码后立即返回，由调用方稍后执行 poll_qr_login()
        返回 iaaa_token（poll=False 时返回空字符串）。
        """
        if output_modes is None:
            output_modes = ["terminal", "file"]

        qr_data = self.get_qr_code()  # also saves IAAA cookies
        image_bytes = qr_data["image_bytes"]

        if "file" in output_modes:
            QR_IMAGE_FILE.write_bytes(image_bytes)
            print(f"[QR] 二维码图片已保存至: {QR_IMAGE_FILE.resolve()}")

        if "base64" in output_modes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            print(f"[QR-BASE64] data:image/png;base64,{b64}")

        if "terminal" in output_modes:
            _print_qr_terminal(image_bytes)

        print("[QR] 请使用北京大学令牌 App 扫描上方二维码登录（有效期约3分钟）")
        if not poll:
            return ""
        return self.poll_qr_login()

    def ensure_login(self, username: str = None, password: str = None,
                     otp: str = None, qr_output_modes: list = None) -> None:
        if self.is_logged_in():
            return
        if username and password:
            otp_info = self.check_otp_required(username)
            if otp_info["otp_required"] and not otp:
                raise RuntimeError(
                    f"账号需要手机动态令牌验证（尾号{otp_info['mobile_mask']}），"
                    "请通过 --otp 参数提供当前6位动态口令"
                )
            self.login(username, password, otp)
        else:
            self.login_with_qr(output_modes=qr_output_modes)

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


def _print_qr_terminal(image_bytes: bytes) -> None:
    """将二维码图片渲染为终端字符画。优先用半块字符，Windows GBK 环境回退到 #/空格。"""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes)).convert("1")
        w, h = img.size
        scale = max(1, w // 40)
        new_w, new_h = w // scale, h // scale
        img = img.resize((new_w, new_h), Image.NEAREST)
        pixels = list(img.getdata())

        # 检测终端是否支持 Unicode 半块字符
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
            # ASCII fallback: # = dark, space = light，每行直接输出
            for row in range(new_h):
                line = ""
                for col in range(new_w):
                    line += "#" if pixels[row * new_w + col] == 0 else " "
                print(line)
        print()
    except ImportError:
        print("[QR] 安装 pillow 可在终端显示二维码: pip install pillow")
    except Exception as e:
        print(f"[QR] 终端二维码渲染失败: {e}")
