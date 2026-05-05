"""Public APIs - no login required."""

import hashlib
import json as _json_mod
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional
import requests

_s = requests.Session()
_s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
})

PORTAL_BASE = "https://portal.pku.edu.cn"
PUBLIC_QUERY = f"{PORTAL_BASE}/publicQuery"


def query_canteen() -> dict:
    """返回各餐厅就餐指数。返回 {time, rows: [{name, seat, ip}]}。"""
    resp = _s.post(
        f"{PUBLIC_QUERY}/canteenQuery/retrCarteenInfos.do",
        headers={"Referer": f"{PUBLIC_QUERY}/#/canteen"},
    )
    return resp.json()


def query_free_classroom(building: str, time_slot: str = "今天") -> list[dict]:
    """
    查询空闲教室。
    building: 一教|二教|三教|四教|理教|文史|哲学|地学|国关|政管
    time_slot: 今天|明天|后天
    """
    resp = _s.get(
        f"{PUBLIC_QUERY}/classroomQuery/retrClassRoomFree.do",
        params={"buildingName": building, "time": time_slot},
        headers={"Referer": f"{PUBLIC_QUERY}/#/freeClassroom"},
    )
    data = resp.json()
    return data.get("rows", data.get("data", []))


def query_portal_notices(num: int = 7) -> dict:
    """查询门户三类公告：学校公告、干部选任、单位公告。"""
    base = f"{PORTAL_BASE}/portal2017"
    endpoints = {
        "school": f"{base}/notice/retrRecentSchoolNotice.do?num={num}",
        "cadre": f"{base}/notice/retrRecentCadreApntNotice.do?num={num}",
        "dept": f"{base}/notice/retrRecentDeptNotice.do?num=21",
    }
    results = {}
    for key, url in endpoints.items():
        try:
            resp = _s.post(url, data="",
                           headers={"Referer": f"{base}/#/index"})
            data = resp.json()
            results[key] = data.get("rows", data.get("data", []))
        except Exception as e:
            results[key] = {"error": str(e)}
    return results


def query_portal_notice_detail(notice_type: str, notice_id: int) -> dict:
    """
    查询公告详情。
    notice_type: school | cadre | dept
    notice_id: 公告 Number 字段值
    返回 {noticeTitle, noticeContent(HTML), ...}
    """
    base = f"{PORTAL_BASE}/portal2017/notice"
    endpoints = {
        "school": f"{base}/getSchoolNoticeDetailById.do",
        "cadre": f"{base}/getCadreApntNoticeDetailById.do",
        "dept": f"{base}/getDeptNoticeDetailById.do",
    }
    url = endpoints.get(notice_type)
    if not url:
        raise ValueError(f"notice_type 必须为 school/cadre/dept，收到：{notice_type}")
    resp = _s.post(url, params={"id": notice_id}, data="",
                   headers={"Referer": f"{PORTAL_BASE}/portal2017/#/index"})
    data = resp.json()
    return data.get("notice", {})


_CALENDAR_PDFS = {
    "2025-2026": "https://www.pku.edu.cn/Uploads/File/2025/01/17/u6789e9c75f2f9.pdf",
    "2026-2027": "https://simso.pku.edu.cn/files/simso/schoolcalendar/2627.pdf",
}

_CALENDAR_CACHE_FILE = Path(__file__).parent.parent / ".calendar_cache.json"


def _load_calendar_cache() -> dict:
    if _CALENDAR_CACHE_FILE.exists():
        try:
            return _json_mod.loads(_CALENDAR_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_calendar_cache(cache: dict) -> None:
    _CALENDAR_CACHE_FILE.write_text(
        _json_mod.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def query_school_calendar(academic_year: Optional[str] = None,
                          keyword: Optional[str] = None,
                          force_refresh: bool = False) -> dict:
    """
    解析校历 PDF，返回提取文本。优先使用本地缓存，避免重复下载 PDF。
    academic_year: '2025-2026' | '2026-2027'（不填则按当前月份自动推断）
    keyword: 关键词过滤（如 '五一' '开学' '考试'），不填则返回全文
    force_refresh: True 时强制重新下载 PDF 并更新缓存
    返回 {year, text, matched_snippets, pdf_url, from_cache}
    """
    import io as _io
    try:
        import pdfplumber
    except ImportError:
        return {"error": "需要安装 pdfplumber：pip install pdfplumber"}

    if academic_year is None:
        today = date.today()
        academic_year = f"{today.year}-{today.year + 1}" if today.month >= 9 else f"{today.year - 1}-{today.year}"

    pdf_url = _CALENDAR_PDFS.get(academic_year)
    if not pdf_url:
        available = list(_CALENDAR_PDFS)
        return {"error": f"暂无 {academic_year} 学年校历 PDF，已收录学年：{available}。"
                         f"如需更新请在 _CALENDAR_PDFS 中添加新学年 URL 后传入 force_refresh=True。"}

    # 尝试读取本地缓存
    cache = _load_calendar_cache()
    if not force_refresh and academic_year in cache:
        text = cache[academic_year]["text"]
        from_cache = True
    else:
        # 下载并解析 PDF
        resp = _s.get(pdf_url, timeout=20)
        if resp.status_code != 200:
            return {"error": f"PDF 下载失败 {resp.status_code}", "pdf_url": pdf_url}
        with pdfplumber.open(_io.BytesIO(resp.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        # 写入缓存
        cache[academic_year] = {
            "text": text,
            "pdf_url": pdf_url,
            "cached_at": date.today().isoformat(),
        }
        _save_calendar_cache(cache)
        from_cache = False

    _ALIASES = {
        "五一": ["劳动节", "五一"],
        "国庆": ["国庆节", "国庆"],
        "元旦": ["元旦"],
        "春节": ["春节"],
        "清明": ["清明节", "清明"],
        "端午": ["端午节", "端午"],
    }

    cached_at = cache.get(academic_year, {}).get("cached_at", "") if from_cache else date.today().isoformat()
    result = {"year": academic_year, "pdf_url": pdf_url, "text": text,
              "from_cache": from_cache, "cached_at": cached_at}
    if keyword:
        terms = _ALIASES.get(keyword, [keyword])
        snippets = []
        for term in terms:
            for m in re.finditer(re.escape(term), text):
                start = max(0, m.start() - 80)
                end = min(len(text), m.end() + 120)
                snippet = text[start:end].replace("\n", " ").strip()
                snippets.append(f"[{term}] ...{snippet}...")
        result["matched_snippets"] = snippets if snippets else ["校历中未找到相关信息"]
    return result


def query_library(cat_marc: Optional[str] = None, keyword: Optional[str] = None) -> dict:
    """
    图书馆馆藏查询。
    cat_marc: 图书编号（如 oxttZ0gaqmKM4as04Zsrdg==）
    keyword: 关键词搜索（若无 cat_marc）
    """
    if cat_marc:
        resp = _s.get(
            "https://opac.lib.pku.edu.cn/BookAPI/new_opac/query_detail",
            params={"cat_marc": cat_marc},
            headers={"Referer": "https://opac.lib.pku.edu.cn/"},
        )
        return resp.json()
    elif keyword:
        resp = _s.get(
            "https://opac.lib.pku.edu.cn/BookAPI/new_opac/query",
            params={"keyword": keyword, "p": 1, "pageSize": 10},
            headers={"Referer": "https://opac.lib.pku.edu.cn/"},
        )
        return resp.json()
    else:
        raise ValueError("必须提供 cat_marc 或 keyword 之一")


_VENUE_S = "c640ca392cd45fb3a55b00a63a86c618"
_VENUE_APP_KEY = "8fceb735082b5a529312040b58ea780b"
_VENUE_BASE = "https://epe.pku.edu.cn/venue-server"


def _venue_headers(path: str, params: dict) -> dict:
    """计算智慧场馆签名头。params 中需已包含 nocache(ts)，值均为 str。"""
    ts = params["nocache"]
    s = _VENUE_S + path
    for key in sorted(params):
        s += key + params[key]
    s += ts + " " + _VENUE_S
    return {"sign": hashlib.md5(s.encode()).hexdigest(), "timestamp": ts, "app-key": _VENUE_APP_KEY}


def query_venue_notices(page: int = 0, size: int = 20) -> dict:
    """查询智慧场馆公告列表。返回 {data:{content:[{id,title,shortContent,content,gmtCreate}]}}。"""
    path = "/api/front/website/articles"
    ts = str(int(time.time() * 1000))
    params = {"nocache": ts, "page": str(page), "size=": str(size)}
    return _s.get(_VENUE_BASE + path, params=params, headers=_venue_headers(path, params)).json()


def query_venue_notice_detail(article_id: int) -> dict:
    """查询智慧场馆公告详情。返回 {data:{id,title,content(HTML),shortContent,...}}。"""
    path = f"/api/front/website/articles/{article_id}"
    ts = str(int(time.time() * 1000))
    params = {"nocache": ts}
    return _s.get(_VENUE_BASE + path, params=params, headers=_venue_headers(path, params)).json()


# ── Library catalog ────────────────────────────────────────────────────────────

_LIB_BASE = "https://opac.lib.pku.edu.cn"


def search_library(keyword: str, rows: int = 10, start: int = 0) -> dict:
    """
    搜索图书馆馆藏。无需登录。
    keyword: 中文或英文关键词，如 '钢铁是怎样炼成的'
    返回 {numFound, docs: [{cat_marc, title, author, publisher, date, callno}]}
    """
    r = _s.get(
        f"{_LIB_BASE}/BookAPI/new_opac/query",
        params={"cc_all": keyword, "rows": rows, "start": start,
                "sort": "", "facet.mincount": 1},
        headers={"Referer": f"{_LIB_BASE}/", "Accept": "application/json; charset=utf-8"},
    )
    import json as _json
    resp = _json.loads(r.content.decode("utf-8")).get("response", {})
    docs = []
    for doc in resp.get("docs", []):
        docs.append({
            "cat_marc": doc.get("id"),
            "title": doc.get("title", []),
            "author": doc.get("author", []),
            "publisher": doc.get("publisher", []),
            "date": doc.get("publishDate", []),
            "callno": doc.get("callnumber-search", []),
        })
    return {"numFound": resp.get("numFound", 0), "docs": docs}


def get_library_detail(cat_marc: str) -> dict:
    """
    获取馆藏详情（可借状态、馆藏位置、索书号）。无需登录。
    cat_marc: search_library 返回的 cat_marc 字段（含 %3D 等编码），直接传入即可。
    返回 {title_info: {...}, items: [{itemID, callNumber, chargeable, location, homeLocation}],
          availability: {totalAvailable, holdable, libraryWithAvailableCopies}}
    """
    # Must build URL directly — passing via params= causes double-encoding
    r = _s.get(
        f"{_LIB_BASE}/BookAPI/new_opac/query_detail?cat_marc={cat_marc}",
        headers={"Referer": f"{_LIB_BASE}/", "Accept": "application/json; charset=utf-8"},
    )
    import json as _json
    parsed = _json.loads(r.content.decode("utf-8"))
    data = parsed.get("data", {})
    book_info = parsed.get("BookInfo", {})
    title_infos = data.get("TitleInfo", [{}])
    ti = title_infos[0] if title_infos else {}

    items = []
    for ci in ti.get("CallInfo", []):
        call_no = ci.get("callNumber", "")
        for item in ci.get("ItemInfo", []):
            items.append({
                "itemID": item.get("itemID"),
                "callNumber": call_no,
                "chargeable": item.get("chargeable", False),
                "location": item.get("currentLocation", ""),
                "homeLocation": item.get("homeLocation", ""),
            })

    avail = ti.get("TitleAvailabilityInfo", {})
    return {
        "title": book_info.get("title", ti.get("author", "")),
        "author": book_info.get("author", ti.get("author", "")),
        "base_callno": ti.get("baseCallNumber", ""),
        "isbn": ti.get("ISBN", []),
        "items": items,
        "availability": {
            "totalAvailable": avail.get("totalCopiesAvailable", 0),
            "holdable": avail.get("holdable", False),
            "libraryWithAvailableCopies": avail.get("libraryWithAvailableCopies", []),
        },
    }
