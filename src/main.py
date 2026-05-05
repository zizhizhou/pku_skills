#!/usr/bin/env python3
"""
北大校内服务 CLI
用法: python src/main.py <command> [options]
"""

import argparse
import getpass
import json
import os
import sys
from datetime import date
from pathlib import Path

# Load .env if present
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _get_creds(args):
    username = args.username or os.environ.get("PKU_STUDENT_ID") or input("学号: ")
    password = args.password or os.environ.get("PKU_PASSWORD") or getpass.getpass("密码: ")
    otp = getattr(args, "otp", None) or os.environ.get("PKU_OTP") or None
    return username, password, otp


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── Public commands ────────────────────────────────────────────────────────────

def cmd_canteen(args):
    from pku_public import query_canteen
    data = query_canteen()
    rows = data.get("rows", []) if isinstance(data, dict) else data
    t = data.get("time", "") if isinstance(data, dict) else ""
    print(f"就餐指数 ({t})")
    print(f"{'餐厅':<14} {'在座/总座':<10} 人均指数")
    print("-" * 38)
    for item in rows:
        name = item.get("name", "")
        seat = item.get("seat", "")
        ip = item.get("ip", "")
        print(f"{name:<14} {ip}/{seat}")


def cmd_classroom(args):
    from pku_public import query_free_classroom
    data = query_free_classroom(args.building, args.time)
    if isinstance(data, list):
        for item in data:
            print(json.dumps(item, ensure_ascii=False))
    else:
        _print_json(data)


def cmd_notices(args):
    from pku_public import query_portal_notices
    results = query_portal_notices(num=args.num)
    notice_type = args.type or "school"
    data = results.get(notice_type, results)
    _print_json(data)


def cmd_calendar(args):
    from pku_public import query_school_calendar
    result = query_school_calendar(
        academic_year=getattr(args, "year", None),
        keyword=getattr(args, "keyword", None),
        force_refresh=getattr(args, "refresh", False),
    )
    if "error" in result:
        print(f"错误: {result['error']}", file=sys.stderr)
        return
    if result.get("from_cache"):
        print(f"[提示] 以下校历来自本地缓存，更新时间：{result.get('cached_at', '未知')}")
        ans = input("是否重新获取最新校历？(y/N) ").strip().lower()
        if ans == "y":
            result = query_school_calendar(
                academic_year=result["year"],
                keyword=getattr(args, "keyword", None),
                force_refresh=True,
            )
            print(f"[已更新] 校历已重新下载（{result.get('cached_at')}）")
    snippets = result.get("matched_snippets")
    if snippets:
        print(f"\n关键词匹配结果（学年 {result['year']}）：")
        for s in snippets:
            print(" ", s)
    else:
        print(result.get("text", "")[:3000])


def cmd_library(args):
    from pku_public import query_library
    data = query_library(cat_marc=args.id, keyword=args.keyword)
    _print_json(data)


def cmd_venue_notices(args):
    from pku_public import query_venue_notices
    data = query_venue_notices(size=args.num)
    _print_json(data)


# ── Portal commands ────────────────────────────────────────────────────────────

def _make_portal_session(args):
    from pku_session import PKUSession
    username, password, otp = _get_creds(args)
    s = PKUSession()
    s.ensure_login(username, password, otp)
    return s


def _make_wproc_session(args):
    from pku_session import WprocSession
    username, password, otp = _get_creds(args)
    s = WprocSession()
    s.ensure_login(username, password, otp)
    return s


def cmd_card(args):
    from pku_portal import get_campus_card_balance
    s = _make_portal_session(args)
    result = get_campus_card_balance(s)
    print(f"电子账户余额: {result['elec_balance']:.2f} 元")
    print(f"卡余额: {result['balance']:.2f} 元")
    if result.get("account"):
        print(f"账户: {result['account']}  一卡通号: {result.get('yktno', '')}  状态: {result.get('status', '')}")


def cmd_tasks(args):
    from pku_portal import get_completed_tasks
    s = _make_portal_session(args)
    items = get_completed_tasks(s)
    if not items:
        print("暂无已办事项记录")
        return
    print(f"共 {len(items)} 条已办事项：")
    for item in items:
        name = item.get("taskName", item.get("name", ""))
        t = item.get("completeTime", item.get("time", ""))
        status = item.get("status", "")
        print(f"  [{t}] {name} ({status})")


def cmd_portlet(args):
    from pku_portal import get_portlet_url, get_all_portlets
    s = _make_portal_session(args)
    if args.name:
        url = get_portlet_url(s, args.name)
        if url:
            print(f"portlet 地址: {url}")
        else:
            print(f"未找到包含 '{args.name}' 的 portlet")
    else:
        portlets = get_all_portlets(s)
        for p in portlets:
            print(f"[{p['topic']}] {p['portletName']} → {p['portletHref']}")


def cmd_network(args):
    from pku_portal import get_network_fee, get_network_status
    if not args.otp:
        print("提示：查询网络状态需要手机动态令牌。请通过 --otp 提供6位动态口令。")
        args.otp = input("OTP动态口令: ")
    s = _make_portal_session(args)
    # 网费余额（校内外均可用）
    data = get_network_fee(s, args.otp)
    print(f"网费余额: {data['fee_balance']}")
    print(f"账号: {data['account']}  姓名: {data['name']}  单位: {data['dept']}")
    # 基础网络状态（仅校内有效）
    status = get_network_status(s)
    if status.get("success"):
        _print_json(status)
    else:
        print(f"网络连接状态: {status.get('msg', '未知')}（校外访问时正常，连接状态不可用）")


def cmd_bus(args):
    from pku_portal import get_bus_routes, get_bus_schedule, BUS_ROUTES
    s = _make_wproc_session(args)
    query_date = args.date or date.today().isoformat()

    if args.list_routes:
        routes = get_bus_routes(s, query_date)
        print(f"班车线路列表 ({query_date}):")
        for r in routes:
            print(f"  ID {r['id']:>2}: {r['name']}")
        return

    # Default: show schedule for 燕园→新燕园 (id=7) if not specified
    resource_id = args.resource_id or 7
    route_name = BUS_ROUTES.get(resource_id, f"线路{resource_id}")
    slots = get_bus_schedule(s, resource_id, query_date)

    if not slots:
        print(f"未查到 {route_name} {query_date} 的班车信息")
        return

    print(f"{route_name} 班车时刻表 ({query_date}):")
    print(f"{'时间':<8} {'剩余':<6} {'总量':<6} {'状态'}")
    print("-" * 35)
    for slot in slots:
        total = int(slot["total"] or 0)
        remaining = slot["remaining"]
        if total == 0:
            status_str = "不运营"
        elif slot["bookable"]:
            status_str = f"可预约 ({remaining}/{total})"
        else:
            status_str = "已结束"
        print(f"{slot['time']:<8} {remaining:<6} {total:<6} {status_str}")


def cmd_bus_book(args):
    from pku_portal import book_bus
    s = _make_wproc_session(args)
    result = book_bus(s, args.time_id, args.resource_id, book_date=args.date)
    _print_json(result)
    if result.get("e") == 0 or result.get("success"):
        appt_id = result.get("d", {}).get("appointment_id") if isinstance(result.get("d"), dict) else None
        if appt_id:
            print(f"\n取消命令: python src/main.py bus-cancel --appointment-id {appt_id} --data-id <hall_appointment_data_id>")
            print("（data-id 从 bus-my 命令的 periodList[0].id 获取）")


def cmd_bus_cancel(args):
    from pku_portal import cancel_bus, get_my_bus_reservations
    s = _make_wproc_session(args)
    if args.data_id is None:
        # 自动从 my-list-time 查 data_id
        items = get_my_bus_reservations(s)
        matched = [it for it in items if it["id"] == args.appointment_id]
        if not matched:
            print(f"未找到预约 id={args.appointment_id}")
            return
        args.data_id = matched[0]["periodList"][0]["id"]
        print(f"自动获取 data_id={args.data_id}")
    result = cancel_bus(s, args.appointment_id, args.data_id)
    print(f"取消结果: {result['msg']}")


def cmd_bus_my(args):
    from pku_portal import get_my_bus_reservations
    s = _make_wproc_session(args)
    items = get_my_bus_reservations(s)
    if not items:
        print("暂无班车预约记录")
        return
    _print_json(items)


def cmd_schedule(args):
    from pku_portal import get_my_course_table, course_add, course_remove, course_modify

    if args.action == "add":
        s = _make_portal_session(args)
        data = get_my_course_table(s, args.xndxq)
        xndxq = data["xndxq"]
        entry = course_add(xndxq, {
            "name": args.name, "weekday": args.weekday,
            "timeNum": args.time_num, "location": args.location or "",
            "frequency": args.frequency or "每周", "note": args.note or "",
        })
        print(f"已新增自定义课程：{entry['name']}  {entry['weekday']} {entry['timeNum']}  _id={entry['_id']}")
        return

    if args.action == "remove":
        s = _make_portal_session(args)
        data = get_my_course_table(s, args.xndxq)
        ok = course_remove(data["xndxq"], args.id)
        print("已删除" if ok else f"未找到 _id={args.id}")
        return

    if args.action == "modify":
        s = _make_portal_session(args)
        data = get_my_course_table(s, args.xndxq)
        updates = {}
        if args.name:     updates["name"] = args.name
        if args.location: updates["location"] = args.location
        if args.note:     updates["note"] = args.note
        c = course_modify(data["xndxq"], args.id, updates)
        print(f"已修改：{c}")
        return

    # 默认：查询课表
    s = _make_portal_session(args)
    data = get_my_course_table(s, args.xndxq, force_refresh=getattr(args, "refresh", False))

    if data.get("from_cache"):
        print(f"[提示] 以下课表来自本地缓存，更新时间：{data.get('cached_at', '未知')}")
        ans = input("是否重新从接口获取最新课表？(y/N) ").strip().lower()
        if ans == "y":
            data = get_my_course_table(s, data["xndxq"], force_refresh=True)
            print(f"[已更新] 课表已重新获取（{data.get('cached_at')}）")

    courses = data["courses"]
    official = [c for c in courses if c["source"] == "official"]
    modified = [c for c in courses if c["source"] == "official_modified"]
    custom   = [c for c in courses if c["source"] == "custom_added"]

    print(f"\n课表 {data['xndxq']}（共{len(courses)}门，"
          f"官方{len(official)}门，已修改{len(modified)}门，自定义新增{len(custom)}门）")
    if modified or custom:
        print("[提示] 带 [自定义] 标记的课程为用户修改或新增，官方课表中不存在。")
    print(f"[提示] 可通过 schedule add/remove/modify 命令自定义课表。")
    print()

    _DAYS_ORDER = ["周一","周二","周三","周四","周五","周六","周日"]
    for day in _DAYS_ORDER:
        day_courses = [c for c in courses if c.get("weekday") == day]
        if not day_courses:
            continue
        print(f"  {day}")
        for c in sorted(day_courses, key=lambda x: x.get("timeNum", "")):
            tag = "" if c["source"] == "official" else " [自定义]"
            loc = f"  {c['location']}" if c.get("location") else ""
            freq = f"  {c['frequency']}" if c.get("frequency") else ""
            print(f"    {c.get('timeNum',''):<6} {c.get('time',''):<15} {c['name']}{tag}{loc}{freq}")


def cmd_grades(args):
    from pku_portal import get_my_grades
    s = _make_portal_session(args)
    data = get_my_grades(s, xnd=args.xnd, xq=args.xq)
    print(f"姓名: {data['xm']}  学号: {data['xh']}  类型: {data['xslb']}")
    print(f"共 {len(data['scores'])} 条成绩记录\n")
    cur_xnd = None
    for sc in data["scores"]:
        header = f"{sc['xnd']}  {sc['xq_name']}"
        if header != cur_xnd:
            print(f"\n── {header} ──")
            cur_xnd = header
        cj = sc["cj"] if sc["cj"] else "未出"
        print(f"  {sc['kcmc']:<20} {sc['kclb']:<6} {sc['xf']}学分  成绩: {cj}")


def cmd_venue_orders(args):
    from pku_venue import VenueSession, get_venue_orders
    username, password, otp = _get_creds(args)
    s = VenueSession()
    s.ensure_login(username, password, otp)
    data = get_venue_orders(s, status=args.status or "")
    _print_json(data)


# ── CLI entry ──────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="北大校内服务 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    cred_parent = argparse.ArgumentParser(add_help=False)
    cred_parent.add_argument("--username", "-u", help="学号")
    cred_parent.add_argument("--password", "-p", help="密码（不建议明文传入）")
    cred_parent.add_argument("--otp", help="手机动态令牌6位口令")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # Public
    sub.add_parser("canteen", help="就餐指数")

    cr = sub.add_parser("classroom", help="空闲教室查询")
    cr.add_argument("--building", "-b", required=True,
                    help="楼栋: 一教|二教|三教|四教|理教|文史|哲学|地学|国关|政管")
    cr.add_argument("--time", "-t", default="今天", help="今天|明天|后天")

    nt = sub.add_parser("notices", help="校内公告")
    nt.add_argument("--type", choices=["school", "cadre", "dept"], default="school")
    nt.add_argument("--num", type=int, default=7)

    cal = sub.add_parser("calendar", help="校历")
    cal.add_argument("--year", help="学年，如 2026-2027（不填自动推断）")
    cal.add_argument("--keyword", "-k", help="关键词，如 五一 开学 考试")
    cal.add_argument("--refresh", action="store_true", help="强制重新下载并更新本地缓存")

    lib = sub.add_parser("library", help="图书馆馆藏查询")
    lib.add_argument("--id", help="图书 cat_marc 编号")
    lib.add_argument("--keyword", "-k", help="关键词搜索")

    vn = sub.add_parser("venue-notices", help="智慧场馆公告")
    vn.add_argument("--num", type=int, default=10)

    # Portal (login required)
    card_p = sub.add_parser("card", help="校园卡余额", parents=[cred_parent])

    tasks_p = sub.add_parser("tasks", help="已办事项查询", parents=[cred_parent])

    portlet_p = sub.add_parser("portlet", help="门户 portlet 跳转链接", parents=[cred_parent])
    portlet_p.add_argument("--name", "-n", help="portlet 名称关键词，如 '课表'")

    net_p = sub.add_parser("network", help="我的网络状态（需OTP）", parents=[cred_parent])

    bus_p = sub.add_parser("bus", help="班车时刻表", parents=[cred_parent])
    bus_p.add_argument("--date", "-d", help="查询日期 YYYY-MM-DD，默认今天")
    bus_p.add_argument("--resource-id", type=int, default=7,
                       help="线路ID (默认7=燕园→新燕园). 用 --list-routes 查看所有")
    bus_p.add_argument("--list-routes", action="store_true", help="列出所有班车线路")

    book_p = sub.add_parser("bus-book", help="预约班车", parents=[cred_parent])
    book_p.add_argument("--time-id", type=int, required=True, help="班次time_id（从bus命令获取）")
    book_p.add_argument("--resource-id", type=int, required=True, help="线路ID")
    book_p.add_argument("--date", "-d", help="预约日期 YYYY-MM-DD，默认今天")

    my_p = sub.add_parser("bus-my", help="我的班车预约", parents=[cred_parent])

    cancel_p = sub.add_parser("bus-cancel", help="取消班车预约", parents=[cred_parent])
    cancel_p.add_argument("--appointment-id", type=int, required=True, help="预约ID（从bus-book或bus-my获取）")
    cancel_p.add_argument("--data-id", type=int, default=None, help="班次数据ID（可选，不填则自动从bus-my查询）")

    sched_p = sub.add_parser("schedule", help="我的课表（支持本地缓存和自定义）", parents=[cred_parent])
    sched_p.add_argument("action", nargs="?", default="view",
                          choices=["view","add","remove","modify"],
                          help="操作：view(默认) | add | remove | modify")
    sched_p.add_argument("--xndxq", help="学年学期，如 25-26-2（不填自动推断）")
    sched_p.add_argument("--refresh", action="store_true", help="强制重新从接口获取官方课表")
    sched_p.add_argument("--id", help="课程 _id（remove/modify 时必填）")
    sched_p.add_argument("--name", help="课程名（add/modify）")
    sched_p.add_argument("--weekday", help="星期（add），如 周一")
    sched_p.add_argument("--time-num", dest="time_num", help="节次（add），如 第三节")
    sched_p.add_argument("--location", help="教室（add/modify）")
    sched_p.add_argument("--frequency", help="频率（add），如 每周/单周/双周")
    sched_p.add_argument("--note", help="备注（add/modify）")

    grades_p = sub.add_parser("grades", help="我的成绩", parents=[cred_parent])
    grades_p.add_argument("--xnd", help="学年筛选，如 25-26")
    grades_p.add_argument("--xq", help="学期筛选：1=秋季 2=春季 3=夏季")

    vo_p = sub.add_parser("venue-orders", help="我的场馆订单", parents=[cred_parent])
    vo_p.add_argument("--status", help="筛选状态: 0待付款|1已确认|2已完成|3已取消")

    return parser


def main():
    # Allow running from project root: `python src/main.py`
    sys.path.insert(0, str(Path(__file__).parent))

    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "canteen": cmd_canteen,
        "classroom": cmd_classroom,
        "notices": cmd_notices,
        "calendar": cmd_calendar,
        "library": cmd_library,
        "venue-notices": cmd_venue_notices,
        "card": cmd_card,
        "tasks": cmd_tasks,
        "portlet": cmd_portlet,
        "network": cmd_network,
        "bus": cmd_bus,
        "bus-book": cmd_bus_book,
        "bus-my": cmd_bus_my,
        "bus-cancel": cmd_bus_cancel,
        "schedule": cmd_schedule,
        "grades": cmd_grades,
        "venue-orders": cmd_venue_orders,
    }

    fn = dispatch.get(args.cmd)
    if fn:
        try:
            fn(args)
        except RuntimeError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n已取消", file=sys.stderr)
            sys.exit(0)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
