"""智慧场馆 APIs (separate CAS auth via epe.pku.edu.cn)."""

import requests
from pku_session import PKUSession, IAAA_BASE

VENUE_BASE = "https://epe.pku.edu.cn/venue-server"
VENUE_CAS = "https://epe.pku.edu.cn/ggtypt"


class VenueSession(PKUSession):
    """Session for epe.pku.edu.cn (smart venues), uses CAS auth."""

    def login(self, username: str, password: str, otp: str = None) -> str:
        import random
        rand = str(random.random())
        service_url = f"{VENUE_BASE}/loginto"
        redir = f"{VENUE_CAS}/login?service={service_url}"

        body = {
            "userName": username,
            "password": password,
            "appId": "portal2017",
            "appName": "北京大学校内信息门户新版",
            "redirUrl": service_url,
            "_rand": rand,
        }
        if otp:
            body["otp"] = otp

        resp = self.session.post(
            f"{IAAA_BASE}/oauthlogin.do",
            data=body,
            headers={"Referer": f"{IAAA_BASE}/oauth.jsp"},
        )
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"IAAA login failed: {data.get('errMsg', '登录失败')}")

        token = data["token"]
        # CAS callback
        self.session.get(
            service_url,
            params={"rand": rand, "token": token},
            allow_redirects=True,
        )
        self._save_cookies()
        return token

    def is_logged_in(self) -> bool:
        try:
            resp = self.session.get(
                f"{VENUE_BASE}/api/front/user/info",
                headers={"Referer": "https://epe.pku.edu.cn/venue/orders"},
                timeout=10,
            )
            data = resp.json()
            return data.get("code") == 200
        except Exception:
            return False


def get_venue_orders(venue: VenueSession, page: int = 1, page_size: int = 10,
                     status: str = "") -> dict:
    """
    查询我的场馆订单。
    status: '' 全部 | '0' 待付款 | '1' 已确认 | '2' 已完成 | '3' 已取消
    """
    resp = venue.get(
        f"{VENUE_BASE}/api/front/order/my_list",
        params={"pageNum": page, "pageSize": page_size, "status": status},
        headers={
            "Referer": "https://epe.pku.edu.cn/venue/orders",
            "Origin": "https://epe.pku.edu.cn",
        },
    )
    return resp.json()


def get_venue_order_detail(venue: VenueSession, order_id: str) -> dict:
    """查询场馆订单详情。"""
    resp = venue.get(
        f"{VENUE_BASE}/api/front/order/detail",
        params={"orderId": order_id},
        headers={"Referer": "https://epe.pku.edu.cn/venue/orders"},
    )
    return resp.json()
