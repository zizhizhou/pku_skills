"""Portal-authenticated APIs (needs portal SESSION cookie)."""

import hashlib as _hashlib
import json as _json
import re as _re
from datetime import date
from pathlib import Path as _Path
from typing import Optional
from pku_session import PKUSession, WprocSession

PORTAL_BASE = "https://portal.pku.edu.cn/portal2017"
WPROC_BASE = "https://wproc.pku.edu.cn"

# Route IDs for bus reservation (discovered via Playwright 2026-04-24)
BUS_ROUTES = {
    7: "燕园校区→新燕园校区",
    6: "燕园校区→新燕园校区→200号校区",
    4: "新燕园校区→燕园校区",
    2: "200号校区→新燕园校区→燕园校区",
    5: "燕园校区→肖家河→西二旗→新燕园校区→200号校区",
    3: "200号校区→新燕园校区→西二旗→肖家河→燕园校区",
    13: "新燕园校区→200号校区",
    14: "200号校区→新燕园校区",
}


_ACTIVITY_TYPES = {"lecture": "讲座", "show": "演出", "recruitment": "招聘", "other": "其他"}


def get_xiaobei_activities(session: PKUSession, event_type: str = "lecture",
                           offset: int = 0, limit: int = 10) -> list[dict]:
    """
    查询小北活动列表。
    event_type: lecture | show | recruitment | other
    返回列表：[{name, organizer, location, start_time, intro, url, url_type}]
    url_type: 'external'（可直接访问）| 'internal'（小北内部页面）
    """
    from email.utils import parsedate_to_datetime as _parse_dt

    if event_type not in _ACTIVITY_TYPES:
        raise ValueError(f"event_type 须为 {list(_ACTIVITY_TYPES)}，收到：{event_type}")

    # Ensure xiaobei session
    test = session.session.get("https://xiaobei.pku.edu.cn/api/api_validate",
                               params={"token": ""},
                               headers={"Referer": "https://xiaobei.pku.edu.cn/"},
                               allow_redirects=False)
    if test.status_code in (401, 302) or "expired" in test.text.lower():
        xiaobei_login(session)

    r = session.session.get(
        "https://xiaobei.pku.edu.cn/api/api_activity",
        params={"event_type": event_type, "offset": offset, "limit": limit},
        headers={"Referer": "https://xiaobei.pku.edu.cn/"},
    )
    if r.status_code == 401:
        raise RuntimeError("小北 session 失效，请重新调用 xiaobei_login()")

    items = r.json().get("activity", [])
    results = []
    for item in items:
        url = item.get("url", "")
        url_type = "external" if url.startswith("http") else "internal"
        # Parse RFC 2822 time → local string
        try:
            dt = _parse_dt(item["start_time"])
            start_time = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            start_time = item.get("start_time", "")
        results.append({
            "name": item.get("event_name", ""),
            "organizer": item.get("event_organizer", ""),
            "location": item.get("event_location", ""),
            "start_time": start_time,
            "intro": item.get("event_introduction", ""),
            "url": url,
            "url_type": url_type,
        })
    return results


def xiaobei_login(session: PKUSession) -> dict:
    """建立小北 Flask session，返回 {campus_id, name, role}。"""
    r = session.get(
        f"{PORTAL_BASE}/util/appSysRedir.do?appId=xiaobei",
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    m = _re.search(r"token=([^&]+)", r.url)
    if not m:
        raise RuntimeError(f"小北 SSO token 未找到，落地 URL: {r.url}")
    token = m.group(1)
    r2 = session.session.get(
        "https://xiaobei.pku.edu.cn/api/api_validate",
        params={"token": token},
        headers={"Referer": "https://xiaobei.pku.edu.cn/"},
    )
    if r2.status_code != 200:
        raise RuntimeError(f"api_validate 失败: {r2.status_code} {r2.text[:200]}")
    return r2.json()


def xiaobei_chat(session: PKUSession, question: str,
                 history: Optional[list] = None,
                 unique_id: Optional[str] = None) -> dict:
    """
    小北 AI 对话（单轮或多轮）。
    question: 本次问题
    history: 之前的消息列表，每条 {role, content, timestamp, feedback}
    unique_id: 会话ID，多轮时保持不变；不传则自动生成
    返回 {answer, unique_id, docs, urls}
    """
    import hashlib as _hashlib
    import os as _os
    from datetime import datetime as _dt

    if unique_id is None:
        unique_id = _hashlib.sha256(_os.urandom(32)).hexdigest()

    # Ensure xiaobei session exists
    test = session.session.get(
        "https://xiaobei.pku.edu.cn/api/api_validate",
        params={"token": ""},
        headers={"Referer": "https://xiaobei.pku.edu.cn/"},
        allow_redirects=False,
    )
    if test.status_code in (401, 302) or "expired" in test.text.lower():
        xiaobei_login(session)

    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    messages = list(history or [])
    messages.append({"role": "user", "content": question, "timestamp": ts, "feedback": 0})
    messages.append({"role": "assistant", "content": "", "timestamp": ts, "feedback": 0})

    payload = {
        "messages": messages,
        "unique_id": unique_id,
        "kn_list": [],
        "timestamp": ts,
        "special_type": "default",
    }

    r = session.session.post(
        "https://xiaobei.pku.edu.cn/api/api_chat_playground",
        json=payload,
        headers={
            "Referer": "https://xiaobei.pku.edu.cn/",
            "Origin": "https://xiaobei.pku.edu.cn",
            "Accept": "text/event-stream",
        },
        stream=True,
        timeout=30,
    )

    if r.status_code == 401:
        raise RuntimeError(f"小北 session 失效（401），请重新调用 xiaobei_login()")

    answer_chunks = []
    docs = urls = locations = markdowns = None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        raw = line[6:]
        if raw == "[DONE]":
            break
        try:
            ev = _json.loads(raw)
            if "answer" in ev:
                answer_chunks.append(ev["answer"])
            elif "docs" in ev:
                docs = ev["docs"]
            elif "urls" in ev:
                urls = ev["urls"]
            elif "locations" in ev:
                locations = ev["locations"]
            elif "markdowns" in ev:
                markdowns = ev["markdowns"]
        except Exception:
            pass

    # Parse JSON-string fields if needed
    def _parse(v):
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except Exception:
                return v
        return v

    answer = "".join(answer_chunks).replace("@#@#", "\n\n").replace("@#", "\n")

    # xiaobei's location fields are swapped: 'latitude' holds lng (~116), 'longitude' holds lat (~39)
    parsed_locations = _parse(locations)
    if isinstance(parsed_locations, list):
        for loc in parsed_locations:
            if "latitude" in loc and "longitude" in loc:
                loc["lng"] = loc.pop("latitude")   # actual longitude
                loc["lat"] = loc.pop("longitude")  # actual latitude
                loc["amap_url"] = (f"https://uri.amap.com/marker"
                                   f"?position={loc['lng']},{loc['lat']}&name={loc['name']}")
                loc["google_url"] = f"https://maps.google.com/?q={loc['lat']},{loc['lng']}"

    return {
        "answer": answer,
        "unique_id": unique_id,
        "docs": _parse(docs),
        "urls": _parse(urls),
        "locations": parsed_locations,
        "markdowns": _parse(markdowns),
    }


def get_campus_card_balance(session: PKUSession) -> dict:
    """
    查询校园卡余额，通过 cardM portlet SSO 链到 card.pku.edu.cn。
    返回 {"balance": float, "account": str, "name": str, "status": str}
    """
    import re as _re
    import json as _json

    CARD_BASE = "https://card.pku.edu.cn"

    # Step 1: Follow portletRedir → sfrzcard prelogin page
    r1 = session.get(
        f"{PORTAL_BASE}/util/portletRedir.do?portletId=cardM",
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    ticket_m = _re.search(r'name=.ssoticketid.\s+id=.ssoticketid.\s+value=.([^"\']+).', r1.text)
    error_m = _re.search(r'name=.errorcode.\s+id=.errorcode.\s+value=.([^"\']*).', r1.text)
    if not ticket_m:
        raise RuntimeError("无法获取校园卡 SSO ticket")

    # Step 2: POST ticket to card.pku.edu.cn/cassyno/index
    session.post(
        f"{CARD_BASE}/cassyno/index",
        data={
            "errorcode": error_m.group(1) if error_m else "1",
            "continueurl": "",
            "ssoticketid": ticket_m.group(1),
        },
        headers={"Referer": r1.url},
        allow_redirects=True,
    )

    # Step 3: Query card info (account="" returns the logged-in user's card)
    r3 = session.post(
        f"{CARD_BASE}/User/GetCardInfoByAccountNoParm",
        data={"account": "", "json": True},
        headers={"Referer": f"{CARD_BASE}/user/user", "X-Requested-With": "XMLHttpRequest"},
    )
    data = r3.json()
    if not data.get("IsSucceed") and data.get("Msg"):
        card_json = _json.loads(data["Msg"])
        card = card_json["query_card"]["card"][0]
        balance_fen = int(card.get("db_balance", 0))
        return {
            "balance": round(balance_fen / 100, 2),
            "elec_balance": round(int(card.get("elec_accamt", 0)) / 100, 2),
            "account": card.get("account"),
            "name": card.get("name"),
            "yktno": card.get("yktno"),
            "status": "正常" if card.get("acc_status") == "0" else card.get("acc_status"),
        }
    raise RuntimeError(f"校园卡查询失败: {data}")


_CLASS_PERIODS = {
    "第一节": "08:00-08:50", "第二节": "09:00-09:50", "第三节": "10:10-11:00",
    "第四节": "11:10-12:00", "第五节": "13:00-13:50", "第六节": "14:00-14:50",
    "第七节": "15:10-16:00", "第八节": "16:10-17:00", "第九节": "17:10-18:00",
    "第十节": "18:40-19:30", "第十一节": "19:40-20:30", "第十二节": "20:40-21:30",
}
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ZH = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
           "fri": "周五", "sat": "周六", "sun": "周日"}


def _ensure_public_query_session(session: PKUSession) -> None:
    """跟随 portletRedir 建立 publicQuery JSESSIONID（幂等：已建立则跳过）。"""
    test = session.get(
        "https://portal.pku.edu.cn/publicQuery/ctrl/topic/myCourseTable/getCourseInfo.do",
        params={"xndxq": "25-26-1"},
        headers={"Referer": "https://portal.pku.edu.cn/publicQuery/"},
        allow_redirects=False,
    )
    if test.status_code == 200:
        return
    session.get(
        f"{PORTAL_BASE}/util/portletRedir.do?portletId=coursetable",
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )


def _parse_course_name(raw: str) -> dict:
    """解析 courseName HTML 字符串，返回 {name, location, frequency, note, exam}。"""
    parts = [p.strip() for p in _re.split(r"<br\s*/?>", raw) if p.strip()]
    name = parts[0] if parts else ""
    location = frequency = note = exam = ""
    for part in parts[1:]:
        if part.startswith("上课信息："):
            body = part[5:].strip()
            tokens = body.split()
            frequency = tokens[0] if tokens else ""
            location = tokens[1] if len(tokens) > 1 else ""
            note = " ".join(tokens[2:]) if len(tokens) > 2 else ""
        elif part.startswith("考试信息："):
            exam = part[5:].strip()
        elif part.startswith("备注："):
            note = part[3:].strip()
    return {"name": name, "location": location, "frequency": frequency, "note": note, "exam": exam}


_COURSE_CACHE_FILE = _Path(__file__).parent.parent / ".course_cache.json"


def _load_course_cache() -> dict:
    if _COURSE_CACHE_FILE.exists():
        try:
            return _json.loads(_COURSE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_course_cache(cache: dict) -> None:
    _COURSE_CACHE_FILE.write_text(
        _json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _course_id(course: dict) -> str:
    """生成课程稳定 ID（节次+星期+课程名的 MD5 前12位）。"""
    key = f"{course.get('timeNum','')}{course.get('weekday','')}{course.get('name','')}"
    return _hashlib.md5(key.encode()).hexdigest()[:12]


def _fetch_official_courses(session: PKUSession, xndxq: str) -> list:
    """从接口拉取官方课表原始列表，每门课含 _id 字段。"""
    _ensure_public_query_session(session)
    resp = session.get(
        "https://portal.pku.edu.cn/publicQuery/ctrl/topic/myCourseTable/getCourseInfo.do",
        params={"xndxq": xndxq},
        headers={"Referer": "https://portal.pku.edu.cn/publicQuery/"},
    )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"课表查询失败: {data}")

    courses = []
    for row in data.get("course", []):
        time_num = row.get("timeNum", "")
        time_range = _CLASS_PERIODS.get(time_num, "")
        for day in _DAYS:
            cell = row.get(day, {})
            raw = cell.get("courseName", "")
            if not raw:
                continue
            c = _parse_course_name(raw)
            c.update({"timeNum": time_num, "time": time_range,
                      "weekday": _DAY_ZH[day], "parity": cell.get("parity", "")})
            c["_id"] = _course_id(c)
            courses.append(c)
    return courses


def get_my_course_table(session: PKUSession, xndxq: Optional[str] = None,
                         force_refresh: bool = False) -> dict:
    """
    查询我的课表，支持本地缓存与自定义课程。
    xndxq: 学年学期，如 '25-26-2'，不填则自动推断当前学期。
    force_refresh: True 时强制重新从接口拉取官方课表（不影响自定义内容）。
    返回 {xndxq, from_cache, cached_at, courses: [{...字段, _id, source}]}
      source: 'official'（官方）| 'official_modified'（官方已修改）| 'custom_added'（自定义新增）
    """
    if xndxq is None:
        today = date.today()
        yy = today.year % 100
        xndxq = f"{yy:02d}-{yy+1:02d}-1" if today.month >= 9 else f"{yy-1:02d}-{yy:02d}-2"

    cache = _load_course_cache()
    semester = cache.get(xndxq, {})
    official_data = semester.get("official", {})

    if not force_refresh and official_data.get("courses"):
        official_courses = official_data["courses"]
        from_cache = True
        cached_at = official_data.get("cached_at", "")
    else:
        official_courses = _fetch_official_courses(session, xndxq)
        if xndxq not in cache:
            cache[xndxq] = {}
        cache[xndxq]["official"] = {
            "courses": official_courses,
            "cached_at": date.today().isoformat(),
        }
        if "custom" not in cache[xndxq]:
            cache[xndxq]["custom"] = {"add": [], "remove": [], "modify": {}}
        _save_course_cache(cache)
        from_cache = False
        cached_at = date.today().isoformat()

    custom = cache.get(xndxq, {}).get("custom", {"add": [], "remove": [], "modify": {}})
    removed_ids = set(custom.get("remove", []))
    modify_map = custom.get("modify", {})

    merged = []
    for c in official_courses:
        cid = c["_id"]
        if cid in removed_ids:
            continue
        if cid in modify_map:
            entry = dict(modify_map[cid])
            entry["_id"] = cid
            entry["source"] = "official_modified"
        else:
            entry = dict(c)
            entry["source"] = "official"
        merged.append(entry)

    for c in custom.get("add", []):
        entry = dict(c)
        entry["source"] = "custom_added"
        merged.append(entry)

    return {"xndxq": xndxq, "from_cache": from_cache,
            "cached_at": cached_at, "courses": merged}


def course_add(xndxq: str, course: dict) -> dict:
    """
    新增自定义课程。
    course 至少包含：name, weekday, timeNum（如 '第三节'）。
    可选：location, frequency, note, time（时间段）。
    返回含 _id 的完整课程 dict。
    """
    cache = _load_course_cache()
    if xndxq not in cache:
        cache[xndxq] = {"official": {"courses": [], "cached_at": ""},
                         "custom": {"add": [], "remove": [], "modify": {}}}
    custom = cache[xndxq].setdefault("custom", {"add": [], "remove": [], "modify": {}})

    entry = {
        "name": course.get("name", ""),
        "weekday": course.get("weekday", ""),
        "timeNum": course.get("timeNum", ""),
        "time": course.get("time", _CLASS_PERIODS.get(course.get("timeNum", ""), "")),
        "location": course.get("location", ""),
        "frequency": course.get("frequency", "每周"),
        "note": course.get("note", ""),
        "exam": course.get("exam", ""),
        "parity": course.get("parity", ""),
    }
    entry["_id"] = "custom_" + _hashlib.md5(
        f"{entry['timeNum']}{entry['weekday']}{entry['name']}".encode()
    ).hexdigest()[:10]
    custom["add"].append(entry)
    _save_course_cache(cache)
    return entry


def course_remove(xndxq: str, course_id: str) -> bool:
    """
    删除课程（官方或自定义）。
    官方课程：加入 remove 集合；自定义课程：从 add 列表移除。
    返回 True 表示找到并删除，False 表示未找到。
    """
    cache = _load_course_cache()
    custom = cache.get(xndxq, {}).get("custom", {"add": [], "remove": [], "modify": {}})

    # 尝试从自定义新增中移除
    before = len(custom.get("add", []))
    custom["add"] = [c for c in custom.get("add", []) if c.get("_id") != course_id]
    if len(custom["add"]) < before:
        cache.setdefault(xndxq, {})["custom"] = custom
        _save_course_cache(cache)
        return True

    # 官方课程：加入 remove 集合
    official_ids = {c["_id"] for c in cache.get(xndxq, {}).get("official", {}).get("courses", [])}
    if course_id in official_ids:
        if course_id not in custom.get("remove", []):
            custom.setdefault("remove", []).append(course_id)
        # 同时清除 modify 中的记录
        custom.get("modify", {}).pop(course_id, None)
        cache.setdefault(xndxq, {})["custom"] = custom
        _save_course_cache(cache)
        return True
    return False


def course_modify(xndxq: str, course_id: str, updates: dict) -> dict:
    """
    修改课程信息（官方或自定义均可）。
    updates: 需要修改的字段，如 {"name": "新名称", "location": "新教室"}。
    返回修改后的完整课程 dict，若未找到则抛出 ValueError。
    """
    cache = _load_course_cache()
    custom = cache.get(xndxq, {}).get("custom", {"add": [], "remove": [], "modify": {}})

    # 查找原始课程（官方或已在 modify 中）
    all_official = cache.get(xndxq, {}).get("official", {}).get("courses", [])
    official_map = {c["_id"]: c for c in all_official}

    # 自定义新增列表中查找
    for i, c in enumerate(custom.get("add", [])):
        if c.get("_id") == course_id:
            custom["add"][i] = {**c, **updates}
            cache.setdefault(xndxq, {})["custom"] = custom
            _save_course_cache(cache)
            return custom["add"][i]

    # 官方课程
    if course_id in official_map:
        base = dict(custom.get("modify", {}).get(course_id, official_map[course_id]))
        base.update(updates)
        base["_id"] = course_id
        custom.setdefault("modify", {})[course_id] = base
        cache.setdefault(xndxq, {})["custom"] = custom
        _save_course_cache(cache)
        return base

    raise ValueError(f"未找到课程 _id={course_id}")


_XQ_ZH = {"1": "秋季学期", "2": "春季学期", "3": "夏季小学期"}


def get_my_grades(session: PKUSession, xnd: Optional[str] = None,
                  xq: Optional[str] = None) -> dict:
    """
    查询我的成绩。
    xnd: 学年筛选，如 '25-26'，不填返回全部。
    xq: 学期筛选，'1'=秋季 '2'=春季 '3'=夏季小学期，不填返回全部。
    返回 {xh, xm, xslb, scores: [{xnd, xq, xq_name, kcmc, kch, xf, cj, hgbz, kclb, khfsm}]}
    """
    _ensure_public_query_session(session)

    resp = session.get(
        "https://portal.pku.edu.cn/publicQuery/ctrl/topic/myScore/retrScores.do",
        headers={"Referer": "https://portal.pku.edu.cn/publicQuery/"},
    )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"成绩查询失败: {data}")

    scores = []
    for row in data.get("scoreLists", []):
        if xnd and row.get("xnd") != xnd:
            continue
        if xq and str(row.get("xq", "")) != str(xq):
            continue
        scores.append({
            "xnd": row.get("xnd", ""),
            "xq": str(row.get("xq", "")),
            "xq_name": _XQ_ZH.get(str(row.get("xq", "")), ""),
            "kcmc": row.get("kcmc", ""),
            "kch": row.get("kch", ""),
            "xf": row.get("xf", ""),
            "cj": row.get("cj", "") or "未出",
            "hgbz": row.get("hgbz", ""),
            "kclb": row.get("kclb", ""),
            "khfsm": row.get("khfsm", ""),
        })

    return {
        "xh": data.get("xh", ""),
        "xm": data.get("xm", ""),
        "xslb": data.get("xslb", ""),
        "scores": scores,
    }


def get_completed_tasks(session: PKUSession) -> list[dict]:
    """查询已办事项列表。"""
    resp = session.post(
        f"{PORTAL_BASE}/bizcenter/todo/retrMyCompletedList.do",
        headers={
            "Referer": f"{PORTAL_BASE}/#/biz/todo",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={},
    )
    data = resp.json()
    return data.get("completedList", data.get("data", []))


def get_portlet_url(session: PKUSession, name_keyword: str) -> Optional[str]:
    """
    查询 portlet 跳转链接。
    name_keyword: 部分名称，如 "课表"、"成绩"、"小北"
    返回 portletHref 或 None
    """
    resp = session.post(
        f"{PORTAL_BASE}/account/retrBizCenterAll.do",
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    data = resp.json()
    all_portlets = []
    for group in data.get("all", []):
        all_portlets.extend(group.get("portlets", []))
    for p in all_portlets:
        if name_keyword in p.get("portletName", ""):
            href = p.get("portletHref", "")
            if href.startswith("/"):
                href = f"https://portal.pku.edu.cn{href}"
            return href
    return None


def get_all_portlets(session: PKUSession) -> list[dict]:
    """返回所有 portlet 的 {topic, portletId, portletName, portletHref} 列表。"""
    resp = session.post(
        f"{PORTAL_BASE}/account/retrBizCenterAll.do",
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    data = resp.json()
    results = []
    for group in data.get("all", []):
        for p in group.get("portlets", []):
            results.append({
                "topic": group.get("topicName"),
                "portletId": p.get("portletId"),
                "portletName": p.get("portletName"),
                "portletHref": p.get("portletHref"),
            })
    return results


def get_network_status(session: PKUSession) -> dict:
    """查询网络状态（需 OTP 登录）。"""
    import random
    resp = session.get(
        f"{PORTAL_BASE}/bizcenter/its/getIpgwinfo.do",
        params={"_rand": str(random.random())},
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    return resp.json()


def get_network_fee(session: PKUSession, otp_code: str) -> dict:
    """
    查询网费余额（校内外均可用）。
    需 OTP 登录后额外完成 ITS 跳转，正确接口为 itsUtil?operation=info。
    注意：ITSipgw?cmd=open 是连接网关而非查余额，校外会被拒。
    返回 {"fee_balance": "20元", "account": "...", "name": "...", "dept": "..."}
    """
    import re

    ITS_BASE = "https://its.pku.edu.cn"

    # Step 1: 门户 OTP 二次解锁
    session.get(
        f"{PORTAL_BASE}/util/validAppSysRedirCode.do",
        params={"validWay": "otpCode", "validCode": otp_code, "trustDevice": "N"},
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )

    # Step 2: 跳转 ITS，建立 ITS JSESSIONID 会话
    r = session.get(
        f"{PORTAL_BASE}/bizcenter/its/redirectToITS.do",
        params={"modId": "web"},
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    if "its.pku.edu.cn" not in r.url:
        raise RuntimeError(f"ITS 跳转失败，落地页: {r.url}（OTP 可能已过期，请重新获取）")

    # Step 3: 查账户信息（含网费余额）
    r2 = session.session.get(
        f"{ITS_BASE}/netportal/itsUtil",
        params={"operation": "info"},
        headers={"Referer": f"{ITS_BASE}/netportal/myits.jsp"},
    )
    html = r2.content.decode("utf-8", errors="replace")

    def _extract(label_pattern: str) -> str:
        m = re.search(
            label_pattern + r"[：:]\s*</td>\s*<td[^>]*>\s*([^<&\s][^<]*?)\s*</td>",
            html, re.S,
        )
        return m.group(1).strip() if m else ""

    fee = _extract(r"余\s*额")
    account = _extract(r"账\s*号")
    name = _extract(r"姓\s*名")
    dept = _extract(r"单\s*位")

    if not fee:
        raise RuntimeError("未能从 ITS 页面提取网费余额，页面结构可能已变更")

    return {"fee_balance": fee, "account": account, "name": name, "dept": dept}


# ── Bus schedule (uses separate WprocSession) ─────────────────────────────────

def _list_page(wproc: WprocSession, query_date: str, resource_id: Optional[int] = None) -> dict:
    params = {
        "hall_id": 1,
        "time": query_date,
        "resource_name": "",
        "resource_id": resource_id or "",
        "min_capacity": "",
        "max_capacity": "",
        "p": 1,
        "page_size": 0,
    }
    resp = wproc.get(
        f"{WPROC_BASE}/site/reservation/list-page",
        params=params,
        headers={"Referer": f"{WPROC_BASE}/v2/reserve/hallView?id=1"},
    )
    return resp.json()


def get_bus_routes(wproc: WprocSession, query_date: Optional[str] = None) -> list[dict]:
    """返回所有班车线路列表。"""
    today = query_date or date.today().isoformat()
    data = _list_page(wproc, today)
    return [
        {"id": item["id"], "name": item["name"],
         "campus": item.get("json_address", {}).get("campus_name")}
        for item in data.get("d", {}).get("list", [])
    ]


def get_bus_schedule(wproc: WprocSession, resource_id: int,
                     query_date: Optional[str] = None) -> list[dict]:
    """
    查询指定线路班车时刻表。
    resource_id: 线路ID，见 BUS_ROUTES 字典
    返回时刻列表，每条含 time, remaining, total, status, time_id
    status: 0=不可预约(已过期或无运营), 1=可预约
    total=0 表示该班次今日不运营
    """
    today = query_date or date.today().isoformat()
    data = _list_page(wproc, today, resource_id)
    items = data.get("d", {}).get("list", [])
    if not items:
        return []
    table = items[0].get("table", {})
    slots = table.get(str(resource_id), list(table.values())[0] if table else [])
    return [
        {
            "time": s["yaxis"],
            "date": s["date"],
            "total": s["row"]["total"],
            "remaining": s["row"]["margin"],
            "status": s["row"]["status"],
            "time_id": s["time_id"],
            "bookable": s["row"]["status"] == 1 and int(s["row"]["total"] or 0) > 0,
        }
        for s in slots
        if s["date"] == today
    ]


def book_bus(wproc: WprocSession, time_id: int, resource_id: int,
             book_date: Optional[str] = None) -> dict:
    """预约班车时间段。返回 API 响应。"""
    import json as _json
    target_date = book_date or date.today().isoformat()
    data_payload = _json.dumps([{"date": target_date, "period": time_id, "sub_resource_id": 0}])
    resp = wproc.post(
        f"{WPROC_BASE}/site/reservation/launch",
        data={
            "resource_id": resource_id,
            "code": "",
            "remarks": "",
            "deduct_num": "",
            "data": data_payload,
            "position_data": "",
        },
        headers={
            "Referer": f"{WPROC_BASE}/v2/reserve/reserveDetail?id={resource_id}",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    if not resp.text:
        return {"success": True}
    return resp.json()


def cancel_bus(wproc: WprocSession, appointment_id: int, data_id: int) -> dict:
    """
    取消班车预约。
    appointment_id: 预约ID（book_bus 返回的 appointment_id，或 my-list-time 返回的 id）
    data_id: 班次数据ID（my-list-time 返回的 hall_appointment_data_id，
             也是 periodList[0].id）
    """
    resp = wproc.post(
        f"{WPROC_BASE}/site/reservation/single-time-cancel",
        data={"appointment_id": appointment_id, "data_id": data_id},
        headers={
            "Referer": f"{WPROC_BASE}/v2/matter/reserveTime",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    if not resp.text:
        return {"success": True}
    data = resp.json()
    if data.get("e") != 0:
        raise RuntimeError(f"取消失败: {data.get('m', resp.text)}")
    return {"success": True, "msg": data.get("m", "操作成功")}


def get_my_bus_reservations(wproc: WprocSession, page: int = 1, page_size: int = 10) -> list[dict]:
    """查询我的班车预约记录。"""
    resp = wproc.get(
        f"{WPROC_BASE}/site/reservation/my-list-time",
        params={"p": page, "page_size": page_size, "status": 2,
                "sort_time": "true", "sort": "asc"},
        headers={"Referer": f"{WPROC_BASE}/v2/matter/reserveTime"},
    )
    data = resp.json()
    return data.get("d", {}).get("data", [])


# ── Venue orders (independent appID=ty auth chain) ────────────────────────────

_VENUE_BASE = "https://epe.pku.edu.cn"
_VENUE_S = "c640ca392cd45fb3a55b00a63a86c618"
_VENUE_APP_KEY = "8fceb735082b5a529312040b58ea780b"
_VENUE_ORDER_STATUS = {1: "正常", 2: "已取消"}
_VENUE_PAY_STATUS = {1: "未支付", 2: "已支付", 4: "已退款"}


def _venue_sign(path: str, timestamp: str, params: dict) -> str:
    import hashlib
    body = _VENUE_S + path
    for k in sorted(params):
        body += k + str(params[k])
    body += timestamp + " " + _VENUE_S
    return hashlib.md5(body.encode()).hexdigest()


def get_venue_orders(username: str, password: str,
                     page: int = 0, size: int = 20) -> list[dict]:
    """
    查询智慧场馆我的预约订单（独立 appID=ty 认证链，不复用门户 Session）。
    返回列表：[{id, trade_no, venue, site, space, campus, date, detail,
               start, end, status, pay_status, fee}]
    status: 正常 | 已取消
    pay_status: 未支付 | 已支付 | 已退款
    """
    import random
    import time

    import requests
    from pku_session import _rsa_encrypt, IAAA_BASE

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0.0.0 Safari/537.36")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    # Step 1: redirectVenue — MUST be first to establish CAS context
    sess.get(
        f"{_VENUE_BASE}/ggtypt/login",
        params={"service": f"{_VENUE_BASE}/venue-server/loginto"},
        allow_redirects=True,
    )

    # Step 2: IAAA home page (appID=ty)
    sess.post(
        f"{IAAA_BASE}/oauth.jsp",
        params={
            "appID": "ty",
            "appName": "北京大学体测系统",
            "redirectUrl": f"{_VENUE_BASE}/ggtypt/dologin",
        },
        headers={"Referer": _VENUE_BASE, "Upgrade-Insecure-Requests": "1"},
    )

    # Step 3: RSA-encrypted login
    pub_key_resp = sess.get(f"{IAAA_BASE}/getPublicKey.do",
                            headers={"Referer": f"{IAAA_BASE}/oauth.jsp"})
    encrypted_pw = _rsa_encrypt(pub_key_resp.json()["key"], password)
    r_login = sess.post(
        f"{IAAA_BASE}/oauthlogin.do",
        data={
            "appid": "ty",
            "userName": username,
            "password": encrypted_pw,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": f"{_VENUE_BASE}/ggtypt/dologin",
        },
        headers={"Referer": f"{IAAA_BASE}/oauth.jsp",
                 "X-Requested-With": "XMLHttpRequest"},
    )
    login_data = r_login.json()
    if not login_data.get("success"):
        raise RuntimeError(f"IAAA login (ty) 失败: {login_data}")
    token = login_data["token"]

    # Step 4: dologin → sets sso_pku_token cookie
    sess.get(
        f"{_VENUE_BASE}/ggtypt/dologin",
        params={"_rand": str(random.random()), "token": token},
        allow_redirects=True,
    )
    sso_pku_token = sess.cookies.get("sso_pku_token")
    if not sso_pku_token:
        raise RuntimeError("dologin 未设置 sso_pku_token cookie，认证链失败")

    # Step 5: venue api/login → access_token + role
    ts = str(int(time.time() * 1000))
    r_api = sess.post(
        f"{_VENUE_BASE}/venue-server/api/login",
        headers={
            "sso-token": sso_pku_token,
            "Cookie": f"sso_pku_token={sso_pku_token};menuPosition=88818889;logout_flag=",
            "sign": _venue_sign("/api/login", ts, {}),
            "timestamp": ts,
            "app-key": _VENUE_APP_KEY,
            "Referer": f"{_VENUE_BASE}/venue/login",
            "Origin": _VENUE_BASE,
        },
    )
    api_data = r_api.json()
    if api_data.get("code") != 200 or api_data.get("data") is None:
        raise RuntimeError(f"venue api/login 失败 (code={api_data.get('code')}): {api_data}")
    access_token = api_data["data"]["token"]["access_token"]
    role = str(api_data["data"]["role"])

    # Step 6: roleLogin → cgAuthorization
    ts = str(int(time.time() * 1000))
    role_params = {"roleid": role}
    r_role = sess.post(
        f"{_VENUE_BASE}/venue-server/roleLogin",
        params=role_params,
        headers={
            "cgAuthorization": access_token,
            "sign": _venue_sign("/roleLogin", ts, role_params),
            "timestamp": ts,
            "app-key": _VENUE_APP_KEY,
        },
    )
    role_data = r_role.json()
    if role_data.get("code") != 200 or role_data.get("data") is None:
        raise RuntimeError(f"venue roleLogin 失败: {role_data}")
    cg_auth = role_data["data"]["token"]["access_token"]

    # Step 7: orders
    ts = str(int(time.time() * 1000))
    order_params = {"page": str(page), "size": str(size), "nocache": ts}
    r_orders = sess.get(
        f"{_VENUE_BASE}/venue-server/api/orders/mine",
        params=order_params,
        headers={
            "cgAuthorization": cg_auth,
            "sign": _venue_sign("/api/orders/mine", ts, order_params),
            "timestamp": ts,
            "app-key": _VENUE_APP_KEY,
        },
    )
    orders_data = r_orders.json()
    if orders_data.get("code") != 200:
        raise RuntimeError(f"venue orders 查询失败: {orders_data}")

    results = []
    for item in orders_data.get("data", {}).get("content", []):
        o_code = item.get("orderStatus")
        p_code = item.get("payStatus")
        results.append({
            "id": item.get("id"),
            "trade_no": item.get("tradeNo"),
            "venue": item.get("venueName"),
            "site": item.get("siteName"),
            "space": item.get("venueSpaceName"),
            "campus": item.get("campusName"),
            "date": item.get("reservationDate"),
            "detail": item.get("reservationDateDetail"),
            "start": item.get("reservationStartDate"),
            "end": item.get("reservationEndDate"),
            "status": _VENUE_ORDER_STATUS.get(o_code, str(o_code)),
            "pay_status": _VENUE_PAY_STATUS.get(p_code, str(p_code)),
            "fee": item.get("amountFee"),
        })
    return results


# ── Licensed software (software.pku.edu.cn, independent Admin-Token) ──────────

_DENTAL_BASE = "http://222.29.72.252"
_DENTAL_STATUS = {"0": "正常(待就诊)", "-1": "已取消", "1": "已完成"}


def dental_login(session: PKUSession) -> tuple:
    """
    通过门户 portletRedir 建立 i看牙 会话。
    返回 (session_key, uid, name, phone)
    """
    import requests as _req
    r = session.get(
        f"{PORTAL_BASE}/util/portletRedir.do",
        params={"portletId": "dentistAppointment"},
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    m = _re.search(r"token=([^&]+)", r.url)
    if not m:
        raise RuntimeError(f"i看牙 IAAA token 未找到，落地 URL: {r.url}")
    iaaa_token = m.group(1)

    d = _req.Session()
    d.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": f"{_DENTAL_BASE}/"})
    r2 = d.post(f"{_DENTAL_BASE}/api/api/accounts/validate?sessionKey={iaaa_token}")
    data = r2.json()
    if data.get("errCode") != "0":
        raise RuntimeError(f"i看牙登录失败: {data.get('errMsg', data)}")
    user = data["user"]
    return user["token"], user["userid"], user["username"], user["phone"]


def dental_get_needs(session_key: str, uid: str) -> list:
    """
    查询可预约科目列表。
    返回 [{text, value, online}]
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/GetNeeds",
                  params={"sessionKey": session_key, "uid": uid},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    head = r.json()["Head"]
    if head["ErrorNo"] != "0":
        raise RuntimeError(f"GetNeeds 失败: {head['ErrorStr']}")
    results = []
    for item in r.json()["Data"].get("Needs") or []:
        results.append({"text": item["text"], "value": item["value"], "online": item.get("Online", "")})
        if item.get("text2"):
            results.append({"text": item["text2"], "value": item["value2"], "online": item.get("Online2", "")})
    return results


def dental_get_doctors(session_key: str, uid: str, need: str) -> list:
    """
    查询某科目的出诊医生列表。
    need: dental_get_needs 返回的 value 字段
    返回 [{text(科室-医生 余号), value(doctor_id), memo(费用), profile}]
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/GetDoctor",
                  params={"sessionKey": session_key, "need": need, "uid": uid},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    head = r.json()["Head"]
    if head["ErrorNo"] != "0":
        raise RuntimeError(f"GetDoctor 失败: {head['ErrorStr']}")
    return [{"text": d["text"], "value": d["value"],
             "memo": d.get("memo", ""), "profile": d.get("profile", "")}
            for d in r.json()["Data"].get("Doctor") or []]


def dental_get_schedule(session_key: str, uid: str, need: str, doctor: str) -> list:
    """
    查询某医生的可预约时段。
    返回 [{text(MM-DD HH:MM 星期X), value(yyxh)}]
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/GetYyrq",
                  params={"sessionKey": session_key, "uid": uid, "need": need, "doctor": doctor},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    head = r.json()["Head"]
    if head["ErrorNo"] != "0":
        raise RuntimeError(f"GetYyrq 失败: {head['ErrorStr']}")
    return [{"text": s["text"], "value": s["value"]}
            for s in r.json()["Data"].get("YYRQ") or []]


def dental_book(session_key: str, uid: str, name: str, yyxh: str, phone: str) -> dict:
    """
    预约挂号。
    yyxh: dental_get_schedule 返回的 value 字段
    返回原始 JSON 响应（Head.ErrorNo=="0" 为成功）
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/Upload",
                  params={"sessionKey": session_key, "id": uid,
                          "name": name, "yyxh": yyxh, "phone": phone},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    return r.json()


def dental_get_appointments(session_key: str, uid: str) -> list:
    """
    查询我的全部预约记录。
    返回 [{XH, Need, Doctor, YYRQ, Status, status_name}]
    Status: '-1'=待就诊, '1'=已完成, '0'/'2'=已取消
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/GetApptList",
                  params={"sessionKey": session_key, "uid": uid},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    head = r.json()["Head"]
    if head["ErrorNo"] != "0":
        raise RuntimeError(f"GetApptList 失败: {head['ErrorStr']}")
    results = []
    for item in r.json()["Data"].get("List") or []:
        item["status_name"] = _DENTAL_STATUS.get(str(item.get("Status", "")), "未知")
        results.append(item)
    return results


def dental_cancel(session_key: str, uid: str, yyxh: str) -> dict:
    """
    取消预约。
    yyxh: dental_get_appointments 返回的 XH 字段（转为字符串）
    返回原始 JSON 响应
    """
    import requests as _req
    r = _req.post(f"{_DENTAL_BASE}/api/Appt/UpdateAppt",
                  params={"sessionKey": session_key, "uid": uid, "yyxh": str(yyxh)},
                  headers={"Referer": f"{_DENTAL_BASE}/"})
    return r.json()


_SW_BASE = "https://software.pku.edu.cn"


def _software_login(session: PKUSession) -> str:
    """获取 software.pku.edu.cn 的 Admin-Token。"""
    r = session.get(
        f"{PORTAL_BASE}/util/portletRedir.do",
        params={"portletId": "softLegal"},
        allow_redirects=True,
        headers={"Referer": f"{PORTAL_BASE}/#/bizCenter"},
    )
    m = _re.search(r"token=([^&]+)", r.url)
    if not m:
        raise RuntimeError(f"正版软件 portal_token 未找到，落地 URL: {r.url}")
    portal_token = m.group(1)

    r2 = session.session.post(
        f"{_SW_BASE}/prod-api/sso/getUserInfo",
        data={"ticket": portal_token},
        headers={"Referer": f"{_SW_BASE}/", "Origin": _SW_BASE},
    )
    d = r2.json()
    if d.get("code") != 200 or not d.get("token"):
        raise RuntimeError(f"正版软件 SSO 失败: {d}")
    return d["token"]


def search_software(session: PKUSession, keyword: str) -> list[dict]:
    """
    搜索正版软件。
    keyword: 搜索词，如 'windows_10'、'office_2021'、'matlab'。
    若不确定命名，先用品牌词（如 'windows'）探查分类，再精确搜索。
    返回列表：[{id, name, intro, size, download_count, download_api}]
    """
    admin_token = _software_login(session)

    import requests as _req
    sw = _req.Session()
    sw.headers.update({"Authorization": admin_token, "Referer": f"{_SW_BASE}/"})

    r = sw.get(
        f"{_SW_BASE}/prod-api/category/double/list",
        params={"str": keyword, "type": "-1"},
    )
    if r.status_code == 401 or r.json().get("code") == 401:
        raise RuntimeError("Admin-Token 失效，请重新调用 _software_login()")

    results = []
    for cat in r.json().get("data", []):
        for child in cat.get("childMenusList") or []:
            for soft in child.get("softs") or []:
                results.append({
                    "id": soft.get("softwareId"),
                    "name": soft.get("name"),
                    "intro": soft.get("intro"),
                    "size": soft.get("size"),
                    "download_count": soft.get("downloadNum"),
                    "download_api": soft.get("downloadPath0", ""),
                })
    return results


def get_software_detail(session: PKUSession, software_id: int) -> dict:
    """
    获取软件详情页内容 + 真实下载链接。
    返回 {name, intro, details_text, system, size_mb, download_count, download_url}
    details_text 为去除 HTML 标签后的纯文本说明。
    """
    import re as _re2
    import requests as _req

    admin_token = _software_login(session)
    sw = _req.Session()
    sw.headers.update({"Authorization": admin_token, "Referer": f"{_SW_BASE}/"})

    r = sw.get(f"{_SW_BASE}/prod-api/softwareIndex/info", params={"id": software_id})
    d = r.json()
    if d.get("code") != 200:
        raise RuntimeError(f"获取软件详情失败: {d}")
    obj = d["data"]["software"]

    # Strip HTML tags from details
    details_html = obj.get("details", "")
    details_text = _re2.sub(r"<[^>]+>", "", details_html)
    details_text = details_text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    details_text = _re2.sub(r" {2,}", " ", details_text)
    details_text = _re2.sub(r"\n{3,}", "\n\n", details_text).strip()

    # Get real download URL
    r2 = sw.get(obj["downloadPath0"])
    dl = r2.json()
    download_url = dl.get("msg", "") if dl.get("code") == 200 else ""

    size_bytes = obj.get("size", 0)
    return {
        "name": obj.get("name"),
        "intro": obj.get("intro"),
        "details_text": details_text,
        "system": obj.get("systemVer"),
        "size_mb": round(size_bytes / 1024 / 1024, 1) if size_bytes else None,
        "download_count": obj.get("downloadNum"),
        "download_url": download_url,
    }


def get_software_download_url(session: PKUSession, software_id: int) -> str:
    """
    获取指定软件的真实下载链接。
    software_id: search_software 返回的 id 字段。
    返回直链 URL 字符串。
    """
    admin_token = _software_login(session)

    import requests as _req
    sw = _req.Session()
    sw.headers.update({"Authorization": admin_token, "Referer": f"{_SW_BASE}/"})

    r = sw.get(f"{_SW_BASE}/prod-api/download/{software_id}/170784/0")
    d = r.json()
    if d.get("code") != 200:
        raise RuntimeError(f"获取下载链接失败: {d}")
    return d["msg"]


# ── Library reservation (requires campus network for 162.105.138.62:8095) ─────

_LIB_BASE = "https://opac.lib.pku.edu.cn"
_LIB_SSO = "https://sso.lib.pku.edu.cn"
_LIB_CALLBACK = "http://162.105.138.62:8095"


def get_library_session(username: str, password: str) -> str:
    """
    获取图书馆 pkulibSession JWT（需 PKU 校园网访问 162.105.138.62:8095）。
    返回 JWT 字符串，用于预约接口的 pkulibSession 字段。
    """
    import re as _re2
    import requests as _req
    from urllib.parse import unquote as _unquote
    from pku_session import _rsa_encrypt, IAAA_BASE

    s = _req.Session()
    s.headers.update({"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )})

    # Step 1: initiate OAuth → lands on IAAA oauthLib.jsp
    r1 = s.get(
        f"{_LIB_SSO}/oauth/authorize",
        params={"client_id": "newopac",
                "redirect_uri": f"{_LIB_CALLBACK}/sso/login",
                "response_type": "code", "state": "PCReaderOAuth"},
        allow_redirects=True,
    )
    iaaa_url = r1.url
    m = _re2.search(r"redirectUrl=([^&]+)", iaaa_url)
    if not m:
        raise RuntimeError(f"未找到 redirectUrl，落地 URL: {iaaa_url}")
    redirect_url = _unquote(m.group(1))

    # Step 2: IAAA login with appID=lib_sso
    pub_key = s.get(f"{IAAA_BASE}/getPublicKey.do",
                    headers={"Referer": iaaa_url}).json()["key"]
    enc_pw = _rsa_encrypt(pub_key, password)
    r_login = s.post(
        f"{IAAA_BASE}/oauthlogin.do",
        data={"appid": "lib_sso", "userName": username, "password": enc_pw,
              "randCode": "", "smsCode": "", "otpCode": "", "redirUrl": redirect_url},
        headers={"Referer": iaaa_url, "X-Requested-With": "XMLHttpRequest"},
    )
    login_data = r_login.json()
    if not login_data.get("success"):
        raise RuntimeError(f"IAAA login (lib_sso) 失败: {login_data}")
    iaaa_token = login_data["token"]

    # Step 3: ssologin → sets IUAT/IUGT cookies on sso.lib.pku.edu.cn
    s.get(redirect_url, params={"_rand": "0.5", "token": iaaa_token}, allow_redirects=True)

    # Step 4: re-authorize (now authenticated) → 302 to internal callback with OAuth code
    r_auth2 = s.get(
        f"{_LIB_SSO}/oauth/authorize",
        params={"client_id": "newopac",
                "redirect_uri": f"{_LIB_CALLBACK}/sso/login",
                "response_type": "code", "state": "PCReaderOAuth"},
        allow_redirects=False,
    )
    loc = r_auth2.headers.get("Location", "")
    m2 = _re2.search(r"code=([^&]+)", loc)
    if not m2:
        raise RuntimeError(f"OAuth code 未找到，Location: {loc}")
    oauth_code = m2.group(1)

    # Step 5: exchange at internal backend → 302 to opac with pkulibSession in code param
    try:
        r_cb = s.get(
            f"{_LIB_CALLBACK}/sso/login?code={oauth_code}&state=PCReaderOAuth",
            allow_redirects=False, timeout=8,
        )
    except Exception as e:
        raise RuntimeError(
            f"无法访问 {_LIB_CALLBACK}（需要 PKU 校园网）: {e}"
        )
    m3 = _re2.search(r"code=([^&]+)", r_cb.headers.get("Location", ""))
    if not m3:
        raise RuntimeError(f"pkulibSession 未找到，Location: {r_cb.headers.get('Location','')}")
    return m3.group(1)


def library_advise_pickup(pkulib_session: str, item_barcode: str) -> dict:
    """
    查询指定馆藏条码可选取书地点。
    返回 {list: [...取书地点], advised: "推荐地点"}
    """
    import requests as _req
    r = _req.post(
        f"{_LIB_BASE}/ReservationAPI/circ/advisePickupAt",
        json={"itemBarcode": item_barcode, "pkulibSession": pkulib_session},
        headers={"Referer": f"{_LIB_BASE}/", "Content-Type": "application/json"},
    )
    d = r.json()
    if d.get("status") != 0:
        raise RuntimeError(f"advisePickupAt 失败: {d}")
    return d.get("data", {})


def library_create_hold(pkulib_session: str, call_number: str,
                        item_barcode: str, pickup_at: str,
                        expire_date: str = "") -> dict:
    """
    预约图书。
    call_number: 索书号（get_library_detail 返回 items[].callNumber）
    item_barcode: 馆藏条码（items[].itemID，需 chargeable=True）
    pickup_at: 取书地点（library_advise_pickup 返回的 list 中选择）
    返回 API 原始响应。
    """
    import requests as _req
    r = _req.post(
        f"{_LIB_BASE}/ReservationAPI/circ/createHold",
        json={"callNumber": call_number, "expireDate": expire_date,
              "itemBarcode": item_barcode, "pickupAt": pickup_at,
              "pkulibSession": pkulib_session},
        headers={"Referer": f"{_LIB_BASE}/", "Content-Type": "application/json"},
    )
    return r.json()


_LIB_A_BASE = "https://a.lib.pku.edu.cn/prod-api"


def library_get_my_holds(pkulib_session: str) -> list:
    """
    查询我的图书预约列表。
    返回 [{title, author, callNumber, itemID, pickupLibrary, placedDate,
           holdStatus, holdInactiveReasonID, queuePosition, queueLength}]
    holdInactiveReasonID 为空/None 表示预约有效，'CANCELLED' 表示已取消。
    """
    import requests as _req
    r = _req.get(
        f"{_LIB_A_BASE}/auth/getMyInfobySession",
        params={"pkulib_session": pkulib_session},
        headers={"Referer": "https://a.lib.pku.edu.cn/"},
    )
    data = r.json()
    if data.get("status") != 0:
        raise RuntimeError(f"getMyInfobySession 失败: {data}")
    holds = data["data"].get("patronHoldInfo") or []
    return [{
        "title": h.get("title", ""),
        "author": h.get("author", ""),
        "callNumber": h.get("callNumber", ""),
        "itemID": h.get("itemID", ""),
        "pickupLibrary": h.get("pickupLibraryDescription", ""),
        "placedDate": h.get("placedDate", ""),
        "holdStatus": h.get("holdStatus"),
        "holdInactiveReasonID": h.get("holdInactiveReasonID"),
        "queuePosition": h.get("queuePosition"),
        "queueLength": h.get("queueLength"),
        "available": h.get("available", False),
    } for h in holds]


def library_get_my_checkouts(pkulib_session: str) -> list:
    """
    查询我的借阅记录（当前借出未还）。
    返回 [{title, author, callNumber, itemID, dueDate, renewalCount, library}]
    """
    import requests as _req
    r = _req.get(
        f"{_LIB_A_BASE}/auth/getMyInfobySession",
        params={"pkulib_session": pkulib_session},
        headers={"Referer": "https://a.lib.pku.edu.cn/"},
    )
    data = r.json()
    if data.get("status") != 0:
        raise RuntimeError(f"getMyInfobySession 失败: {data}")
    checkouts = data["data"].get("patronCheckoutInfo") or []
    return [{
        "title": c.get("title", ""),
        "author": c.get("author", ""),
        "callNumber": c.get("callNumber", ""),
        "itemID": c.get("itemID", ""),
        "dueDate": c.get("dueDate", ""),
        "renewalCount": c.get("renewalCount", 0),
        "library": c.get("libraryDescription", ""),
    } for c in checkouts]


def library_cancel_hold(pkulib_session: str, item_barcode: str,
                        call_number: str, cancel_reason: str = "") -> dict:
    """
    取消图书预约。
    item_barcode: library_get_my_holds 返回的 itemID 字段
    call_number:  library_get_my_holds 返回的 callNumber 字段
    返回 API 原始响应（status=0 为成功）。
    """
    import requests as _req
    r = _req.post(
        f"{_LIB_A_BASE}/circ/cancelHold",
        json={"itemBarcode": item_barcode, "callNumber": call_number,
              "pkulibSession": pkulib_session, "cancelReason": cancel_reason},
        headers={"Referer": "https://a.lib.pku.edu.cn/",
                 "Content-Type": "application/json"},
    )
    return r.json()
