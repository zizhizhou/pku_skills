# 北大校内服务 CLI Skill

---

## 北大问题路由策略

当用户提问涉及北京大学 / 北大相关内容时，按以下优先级决定响应方式：

### 第一步：匹配已有 Skill

优先检查问题是否直接对应以下已实现功能，若匹配则**直接调用对应 skill**，不走小北：

| 用户意图关键词 | Skill | CLI 命令 |
|---|---|---|
| 食堂 / 餐厅 / 就餐人流 / 人少 | `canteen_index` | `canteen` |
| 空闲教室 / 自习室 / 哪里有教室 | `free_classroom` | `classroom` |
| 校内公告 / 通知 / 干部选任 | `portal_notices` | `notices` |
| 校历 / 放假 / 开学 / 考试周 / 五一 / 假期安排 | `school_calendar` | `calendar` |
| 图书馆 / 借书 / 查书 / 馆藏 / 预约图书 | `library_catalog` | Python API |
| 场馆公告 / 体育馆通知 | `venue_notices` | `venue-notices` |
| 我的成绩 / GPA / 挂科了没 | `my_grades` | `grades` |
| 校园卡 / 饭卡余额 / 一卡通 | `campus_card` | `card` |
| 班车 / 通勤车 / 去昌平 / 新燕园班车 | `bus_reservation` | `bus` / `bus-book` |
| 网费 / 网络余额 / 流量 | `my_network` | `network` |
| 场馆订单 / 羽毛球预约 / 体育场馆 | `venue_orders` | `venue-orders` |
| 看牙 / 口腔 / 牙科 / 拔牙 / 洗牙 | `dental_service` | Python API |
| 正版软件 / office / matlab / windows 下载 | `software_download` | Python API |
| 讲座 / 演出 / 校园招聘 / 近期活动 | `xiaobei_activity` | Python API |
| 已办事项 / 办理记录 | `completed_tasks` | `tasks` |

### 第二步：无匹配时走小北

当问题涉及北大但**没有对应 skill**（如：北大历史、政策规定、地点导航、学术资讯、生活服务、校园文化等），执行以下流程：

```
1. 调用 xiaobei_chat skill，将用户原始问题发送给小北
2. 解析小北 SSE 回复，拼接完整 answer
3. 判断小北的回答是否指向某个可执行操作：
   - 若回答暗示"可以预约 / 可以查询 / 可以办理"且对应某个 skill
     → 告知用户小北的回答，同时询问是否调用对应 skill 执行
   - 否则（纯信息性回答）
     → 直接将小北的回答返回给用户
```

### 第三步：小北也无法回答时

若小北返回"暂不了解"或空回复，则基于自身知识直接回答，并说明信息可能不是最新的。

### 路由示例

| 用户提问 | 路由决策 |
|---|---|
| "今天学一食堂人多吗" | → `canteen_index`（直接调用，无需小北） |
| "帮我查一下《活着》能不能借" | → `library_catalog` search + detail |
| "五一几天假" | → `school_calendar`（PDF 关键词匹配） |
| "北大什么时候建校的" | → 小北 → 纯信息回答，直接返回 |
| "图书馆几点关门" | → 小北 → 返回答案 |
| "最近有什么讲座" | → `xiaobei_activity`（lecture 类型） |
| "北大有没有游泳池，能预约吗" | → 小北询问地点/时间 → 若可预约则引导至 `venue_orders` |

---

## 调用方式

Claude 通过 Bash 工具运行以下命令访问所有北大校内服务：

```bash
python src/main.py <command> [options]
```

工作目录：`D:/zizhou_edit/agent/pku_skills/auto-pku-helper`

安装依赖（首次）：
```bash
pip install -r requirements.txt
```

## 凭据传递优先级

1. CLI 参数：`--username`、`--password`、`--otp`
2. `.env` 文件：`PKU_STUDENT_ID`、`PKU_PASSWORD`
3. 交互式输入（getpass，密码不回显）

**重要**：密码不要明文出现在命令行历史中。若用户已配置 `.env`，直接运行命令即可。
登录状态会持久化至 `.pku_session.json`，同一天内无需重复登录。

---

## 命令速查

### 无需登录（public）

| 命令 | 说明 | 示例 |
|------|------|------|
| `canteen` | 就餐指数 | `python src/main.py canteen` |
| `classroom` | 空闲教室 | `python src/main.py classroom --building 一教 --time 今天` |
| `notices` | 校内公告 | `python src/main.py notices --type school` |
| `calendar` | 校历 | `python src/main.py calendar` |
| `library` | 图书馆馆藏 | `python src/main.py library --keyword 机器学习` |
| `venue-notices` | 智慧场馆公告 | `python src/main.py venue-notices` |

classroom --building 可选值：`一教|二教|三教|四教|理教|文史|哲学|地学|国关|政管`
notices --type 可选值：`school`（学校公告）| `cadre`（干部选任）| `dept`（单位公告）

### 需门户登录（pku-login）

| 命令 | 说明 | 示例 |
|------|------|------|
| `schedule` | 我的课表（含自定义） | `python src/main.py schedule [--xndxq 25-26-2] [--refresh]` |
| `card` | 校园卡余额 | `python src/main.py card -u 学号 -p 密码` |
| `grades` | 我的成绩 | `python src/main.py grades [--xnd 25-26] [--xq 2]` |
| `tasks` | 已办事项 | `python src/main.py tasks` |
| `portlet` | portlet 跳转链接 | `python src/main.py portlet --name 课表` |
| `bus` | 班车时刻表 | `python src/main.py bus --date 2026-04-24` |
| `bus-book` | 预约班车 | `python src/main.py bus-book --time-id 44 --resource-id 7` |
| `bus-my` | 我的班车预约 | `python src/main.py bus-my` |
| `venue-orders` | 我的场馆订单 | `python src/main.py venue-orders` |

### 需门户登录（Python API，无 CLI 命令）

以下功能通过 Python 脚本调用，无独立 CLI 命令：

| 功能 | 模块 | 主要函数 |
|------|------|---------|
| 图书馆搜索/详情/预约/我的借阅 | `pku_public` / `pku_portal` | `search_library`, `get_library_detail`, `library_create_hold`, `library_get_my_holds`, `library_cancel_hold` |
| i看牙口腔预约 | `pku_portal` | `dental_login`, `dental_get_needs`, `dental_get_doctors`, `dental_get_schedule`, `dental_book`, `dental_get_appointments`, `dental_cancel` |
| 正版软件下载 | `pku_portal` | `search_software`, `get_software_detail`, `get_software_download_url` |
| 小北对话 / 活动查询 | `pku_portal` | `get_xiaobei_chat`, `get_xiaobei_activities` |

### 需双重认证（pku-login-token）

| 命令 | 说明 | 示例 |
|------|------|------|
| `network` | 我的网络状态 | `python src/main.py network --otp 123456` |

---

## 班车线路 ID 对照

| ID | 线路 |
|----|------|
| 7  | 燕园校区→新燕园校区（默认） |
| 6  | 燕园校区→新燕园校区→200号校区 |
| 4  | 新燕园校区→燕园校区 |
| 2  | 200号校区→新燕园校区→燕园校区 |
| 5  | 燕园校区→肖家河→西二旗→新燕园校区→200号校区 |
| 3  | 200号校区→新燕园校区→西二旗→肖家河→燕园校区 |
| 13 | 新燕园校区→200号校区 |
| 14 | 200号校区→新燕园校区 |

查看所有线路：`python src/main.py bus --list-routes`

## 班车时刻表字段说明

- `total=0`：该班次今日不运营
- `status=1`：开放预约；`status=0`：已结束或不可预约
- `remaining`：剩余名额；`total`：总容量
- `bookable=true`：可立即预约

## Session 缓存

登录 Cookie 存储于项目根目录 `.pku_session.json`（已加入 .gitignore）。
Cookie 过期或登录失败时自动重新登录。

## 错误处理

- OTP 错误：重新打开北京大学令牌 App 获取最新口令后重试
- SESSION 过期：删除 `.pku_session.json` 再运行
- 接口变更：查看 `skills/` 目录下对应 YAML 文件的 `extra_api` 字段
