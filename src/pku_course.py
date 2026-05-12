"""
北大教学网 (Blackboard Learn) API

Auth flow:
  1. IAAA oauthlogin.do (appid="blackboard")
  2. GET /webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin?_rand=...&token=<iaaa_token>
  3. Cookie-based session established
  Session file: .course_session.json (separate from portal session)
"""

import re
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

COURSE_BASE = "https://course.pku.edu.cn"
SSO_LOGIN = f"{COURSE_BASE}/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
# IAAA requires http:// (not https://) for the blackboard redirect URL
OAUTH_REDIR = "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
IAAA_BASE = "https://iaaa.pku.edu.cn/iaaa"
SESSION_FILE = Path(__file__).parent.parent / ".course_session.json"


class CourseSession:
    def __init__(self):
        import json, random
        self._json = json
        self._random = random
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._load_cookies()

    def _load_cookies(self):
        if SESSION_FILE.exists():
            try:
                data = self._json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                for c in data.get("cookies", []):
                    self.session.cookies.set(c["name"], c["value"],
                                             domain=c.get("domain", ""),
                                             path=c.get("path", "/"))
            except Exception:
                pass

    def _save_cookies(self):
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path}
            for c in self.session.cookies
        ]
        SESSION_FILE.write_text(
            self._json.dumps({"cookies": cookies}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def login(self, username: str, password: str, otp: str = None) -> None:
        from pku_session import _rsa_encrypt
        import random, urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        app_id = "blackboard"
        redir_url = OAUTH_REDIR  # must be http:// as registered in IAAA

        self.session.get(
            f"{IAAA_BASE}/oauth.jsp",
            params={"appID": app_id, "appName": "北京大学教学网", "redirectUrl": redir_url},
        )
        resp = self.session.get(f"{IAAA_BASE}/getPublicKey.do",
                                headers={"Referer": f"{IAAA_BASE}/oauth.jsp"})
        pub_key = resp.json()["key"]
        enc_password = _rsa_encrypt(pub_key, password)

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
            headers={"Referer": f"{IAAA_BASE}/oauth.jsp?appID={app_id}"},
        )
        data = resp.json()
        if not data.get("success"):
            err = data.get("errors", {}).get("msg", data.get("errMsg", "登录失败"))
            raise RuntimeError(f"IAAA 登录失败: {err}")

        iaaa_token = data["token"]
        rand_val = random.random()
        sso_resp = self.session.get(
            SSO_LOGIN,
            params={"_rand": f"{rand_val:.20f}", "token": iaaa_token},
            allow_redirects=True,
        )

        # Verify login by accessing homepage
        home = self.session.get(
            f"{COURSE_BASE}/webapps/portal/execute/tabs/tabAction",
            params={"tab_tab_group_id": "_1_1"},
        )
        if not home.ok:
            raise RuntimeError("教学网登录验证失败")

        self._save_cookies()

    def is_logged_in(self) -> bool:
        try:
            resp = self.session.get(
                f"{COURSE_BASE}/webapps/portal/execute/tabs/tabAction",
                params={"tab_tab_group_id": "_1_1"},
                timeout=10,
                allow_redirects=False,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def ensure_login(self, username: str = None, password: str = None, otp: str = None):
        if self.is_logged_in():
            return
        if not (username and password):
            raise RuntimeError("教学网需要登录，请在 .env 中设置 PKU_STUDENT_ID / PKU_PASSWORD")
        self.login(username, password, otp)

    # ── API ───────────────────────────────────────────────────────────────────

    def list_courses(self, current_only: bool = True) -> list[dict]:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = self.session.get(
            f"{COURSE_BASE}/webapps/portal/execute/tabs/tabAction",
            params={"tab_tab_group_id": "_1_1"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        courses = []

        # Detect current semester label (e.g. "25-26学年第2学期")
        current_markers = _detect_current_semester_markers(soup)

        for ul in soup.select("ul.courseListing"):
            for li in ul.select("li"):
                a = li.select_one("a")
                if not a:
                    continue
                href = a.get("href", "")
                title = a.get_text(strip=True)
                # Extract course_id from PkId{key=_XXXXX_1,...}
                m = re.search(r"key=(_\d+_\d+)", href)
                if not m:
                    continue
                course_id = m.group(1)
                is_current = any(marker in title for marker in current_markers) if current_markers else False
                if current_only and not is_current:
                    continue
                courses.append({
                    "id": course_id,
                    "title": title,
                    "is_current": is_current,
                    "href": COURSE_BASE + href if href.startswith("/") else href,
                })

        return courses

    def list_assignments(self, course_id: str = None) -> list[dict]:
        courses = self.list_courses(current_only=True) if not course_id else \
            [{"id": course_id, "title": "", "is_current": True}]
        all_assignments = []
        for course in courses:
            try:
                assignments = self._get_course_assignments(course["id"], course.get("title", ""))
                all_assignments.extend(assignments)
            except Exception:
                pass
        all_assignments.sort(key=lambda a: a.get("deadline_ts") or float("inf"))
        return all_assignments

    def _get_course_assignments(self, course_id: str, course_name: str) -> list[dict]:
        resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/execute/announcement",
            params={
                "method": "search",
                "context": "course_entry",
                "course_id": course_id,
                "handle": "announcements_entry",
                "mode": "view",
            },
        )
        if not resp.ok:
            return []

        # Get content list from course
        content_resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/content/listContent.jsp",
            params={"content_id": f"_", "course_id": course_id},
        )

        # Search for assignments via course outline
        assignments = []
        assignments.extend(self._find_assignments_recursive(course_id, course_name, None, depth=0))
        return assignments

    def _find_assignments_recursive(self, course_id: str, course_name: str,
                                     content_id: str | None, depth: int) -> list[dict]:
        if depth > 4:
            return []

        params = {"course_id": course_id}
        if content_id:
            params["content_id"] = content_id

        resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/content/listContent.jsp",
            params=params,
        )
        if not resp.ok:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        for li in soup.select("#content_listContainer > li"):
            item_type = "unknown"
            if li.select_one("img[src*='assignment']") or "assignment" in str(li.get("class", [])):
                item_type = "assignment"
            elif li.select_one("img[src*='folder']"):
                item_type = "folder"

            title_a = li.select_one("h3 a, h4 a")
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            href = title_a.get("href", "")

            if item_type == "folder":
                m = re.search(r"content_id=([^&]+)", href)
                if m:
                    sub = self._find_assignments_recursive(
                        course_id, course_name, m.group(1), depth + 1)
                    results.extend(sub)
                continue

            if "uploadAssignment" not in href and "assignment" not in href.lower():
                continue

            m_content = re.search(r"content_id=([^&]+)", href)
            content_id_val = m_content.group(1) if m_content else ""

            deadline_raw = ""
            deadline_ts = None
            for span in li.select("span, div"):
                txt = span.get_text(strip=True)
                if "截止" in txt or "Due" in txt or "due" in txt or re.search(r"\d{4}年\d+月\d+日", txt):
                    deadline_raw = txt
                    deadline_ts = _parse_deadline(txt)
                    break

            results.append({
                "course_id": course_id,
                "course_name": course_name,
                "content_id": content_id_val,
                "title": title,
                "deadline_raw": deadline_raw,
                "deadline_ts": deadline_ts,
            })

        return results

    def list_announcements(self, course_id: str = None, limit: int = 20) -> list[dict]:
        courses = self.list_courses(current_only=True) if not course_id else \
            [{"id": course_id, "title": "", "is_current": True}]
        all_ann = []
        for course in courses:
            try:
                anns = self._get_course_announcements(course["id"], course.get("title", ""))
                all_ann.extend(anns)
            except Exception:
                pass
        all_ann.sort(key=lambda a: a.get("date", ""), reverse=True)
        return all_ann[:limit]

    def _get_course_announcements(self, course_id: str, course_name: str) -> list[dict]:
        resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/execute/announcement",
            params={
                "method": "search",
                "context": "course_entry",
                "course_id": course_id,
                "handle": "announcements_entry",
                "mode": "view",
            },
        )
        if not resp.ok:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for li in soup.select("#announcementList > li, ul.announcementList > li"):
            title = li.select_one("h3, .announcement-title")
            title = title.get_text(strip=True) if title else ""
            date_tag = li.select_one(".announcementDatePosted, .date")
            date = date_tag.get_text(strip=True) if date_tag else ""
            author_tag = li.select_one(".announcementPostedBy, .author")
            author = author_tag.get_text(strip=True) if author_tag else ""
            body_tag = li.select_one("div.details, .announcementBody, p")
            body = body_tag.get_text(strip=True) if body_tag else ""
            if title:
                results.append({
                    "course_id": course_id,
                    "course_name": course_name,
                    "title": title,
                    "date": date,
                    "author": author,
                    "body": body[:300],
                })
        return results

    def list_content(self, course_id: str, content_id: str = None) -> list[dict]:
        if content_id is None:
            return self._list_course_menu(course_id)
        return self._list_content_folder(course_id, content_id)

    def _list_course_menu(self, course_id: str) -> list[dict]:
        """List the top-level course menu (left sidebar navigation items)."""
        resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/execute/announcement",
            params={
                "method": "search", "context": "course_entry",
                "course_id": course_id, "handle": "announcements_entry", "mode": "view",
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "listContent" not in href or "content_id" not in href:
                continue
            m = re.search(r"content_id=([^&]+)", href)
            if not m:
                continue
            content_id = m.group(1)
            if content_id in seen:
                continue
            seen.add(content_id)
            title = a.get_text(strip=True)
            if title:
                items.append({
                    "title": title,
                    "type": "folder",
                    "content_id": content_id,
                    "href": COURSE_BASE + href if href.startswith("/") else href,
                    "attachments": [],
                    "description": f"content_id={content_id}",
                })
        return items

    def _list_content_folder(self, course_id: str, content_id: str) -> list[dict]:
        """List items inside a specific content folder."""
        resp = self.session.get(
            f"{COURSE_BASE}/webapps/blackboard/content/listContent.jsp",
            params={"course_id": course_id, "content_id": content_id},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        container = soup.find(id="content_listContainer")
        if not container:
            return items
        for li in container.find_all("li", recursive=False):
            title_a = li.select_one("h3 a, h4 a")
            if not title_a:
                title_span = li.select_one("h3, h4")
                if not title_span:
                    continue
                title = title_span.get_text(strip=True)
                href = ""
            else:
                title = title_a.get_text(strip=True)
                href = title_a.get("href", "")

            item_type = "document"
            if "uploadAssignment" in href or li.select_one("img[src*='assignment']"):
                item_type = "assignment"
            elif "listContent" in href or li.select_one("img[src*='folder']"):
                item_type = "folder"

            attachments = []
            for att_a in li.select("ul.attachments li a, .attachments a"):
                att_href = att_a.get("href", "")
                att_name = att_a.get_text(strip=True)
                if att_href and att_name:
                    attachments.append({
                        "name": att_name,
                        "url": COURSE_BASE + att_href if att_href.startswith("/") else att_href,
                    })

            desc_tag = li.select_one("div.details, .vtbegenerated, p.detailsValue")
            desc = desc_tag.get_text(strip=True)[:200] if desc_tag else ""

            full_href = COURSE_BASE + href if href.startswith("/") else href
            m_cid = re.search(r"content_id=([^&]+)", href)
            items.append({
                "title": title,
                "type": item_type,
                "content_id": m_cid.group(1) if m_cid else "",
                "href": full_href,
                "attachments": attachments,
                "description": desc,
            })
        return items


def _detect_current_semester_markers(soup) -> list[str]:
    """Return only the latest semester label found on the page, e.g. ['25-26学年第2学期']."""
    text = soup.get_text(" ")
    found = []
    for m in re.finditer(r"(\d{2}-\d{2}学年第\d学期)", text):
        label = m.group(1)
        if label not in found:
            found.append(label)
    if not found:
        return []
    # Sort descending to pick the latest: '25-26第2' > '25-26第1' > '24-25第2' etc.
    def _sem_key(s):
        m2 = re.match(r"(\d{2})-(\d{2})学年第(\d)学期", s)
        if m2:
            return (int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        return (0, 0, 0)
    found.sort(key=_sem_key, reverse=True)
    return [found[0]]  # only the latest


def _parse_deadline(text: str):
    """Try to parse a Chinese deadline string to Unix timestamp."""
    try:
        import datetime
        m = re.search(r"(\d{4})年(\d+)月(\d+)日\s*(?:星期[一二三四五六日])?\s*([上下]午)?(\d+):(\d+)", text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            ampm = m.group(4) or ""
            hour, minute = int(m.group(5)), int(m.group(6))
            if ampm == "下午" and hour < 12:
                hour += 12
            dt = datetime.datetime(year, month, day, hour, minute)
            return int(dt.timestamp())
    except Exception:
        pass
    return None


def format_course(c: dict) -> str:
    tag = "[当前]" if c.get("is_current") else "[历史]"
    return f"{tag} {c['id']}  {c['title']}"


def format_assignment(a: dict) -> str:
    dl = a.get("deadline_raw") or "无截止日期"
    course = a.get("course_name", "")
    title = a.get("title", "")
    if a.get("deadline_ts"):
        now = time.time()
        left = a["deadline_ts"] - now
        if left < 0:
            dl = f"[已过期] {dl}"
        elif left < 86400:
            dl = f"[{int(left/3600)}h后截止] {dl}"
    return f"  [{course}] {title}\n    截止: {dl}"


def format_announcement(a: dict) -> str:
    course = a.get("course_name", "")
    title = a.get("title", "")
    date = a.get("date", "")
    author = a.get("author", "")
    body = a.get("body", "")
    header = f"  [{course}] {title}"
    meta = f"    {date} {author}".strip()
    return f"{header}\n{meta}\n    {body}" if body else f"{header}\n{meta}"


def format_content_item(item: dict, indent: int = 0) -> str:
    prefix = "  " * indent
    icon = {"assignment": "📝", "folder": "📁", "document": "📄"}.get(item["type"], "•")
    line = f"{prefix}{icon} {item['title']}"
    if item.get("description"):
        line += f"\n{prefix}   {item['description']}"
    for att in item.get("attachments", []):
        line += f"\n{prefix}   📎 {att['name']}"
    return line
