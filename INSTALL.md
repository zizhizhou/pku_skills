# 安装指南

> 本文档适用于 **GitHub** 版本：`https://github.com/zizhizhou/pku_skills`

---

## 前置要求

- Python 3.10+
- git
- 北大统一身份认证账号（学号 + 密码）
- 需要使用「我的网络」功能时：北京大学令牌 App（OTP）

---

## Open Claw

**方式一：克隆到个人 skills 目录（推荐，跨项目复用）**

```bash
git clone https://github.com/zizhizhou/pku_skills ~/.openclaw/skills/pku_skills
pip install -r ~/.openclaw/skills/pku_skills/requirements.txt
cp ~/.openclaw/skills/pku_skills/.env.example ~/.openclaw/skills/pku_skills/.env
```

编辑 `.env`，填入学号和密码：

```
PKU_STUDENT_ID=你的学号
PKU_PASSWORD=你的密码
```

重启 Open Claw，输入「显示 pku-skills 的所有技能」验证安装。

**方式二：克隆到当前工作区**

```bash
git clone https://github.com/zizhizhou/pku_skills <workspace>/skills/pku_skills
```

---

## QClaw

QClaw 与 Open Claw 共享 skill 目录格式：

```bash
git clone https://github.com/zizhizhou/pku_skills ~/.openclaw/skills/pku_skills
pip install -r ~/.openclaw/skills/pku_skills/requirements.txt
cp ~/.openclaw/skills/pku_skills/.env.example ~/.openclaw/skills/pku_skills/.env
```

或通过 QClaw Dashboard（`http://localhost:3000`）→ Skills 页面手动加载本地目录。

---

## Hermes Agent

2026 年了，你有 Agent，让它帮你装——把这句话扔给 Hermes：

> 帮我从 `https://github.com/zizhizhou/pku_skills` 安装 PKU Skills，克隆到 `~/.hermes/skills/pku_skills`，安装 Python 依赖，并配置好凭据

或者手动三步走：

```bash
# 1. 克隆完整仓库（不能只下 SKILL.md，src/ 目录是运行时必须的）
git clone https://github.com/zizhizhou/pku_skills ~/.hermes/skills/pku_skills

# 2. 安装依赖
pip install -r ~/.hermes/skills/pku_skills/requirements.txt

# 3. 配置凭据（Hermes 也支持通过 required_environment_variables 在首次调用时交互式输入）
cp ~/.hermes/skills/pku_skills/.env.example ~/.hermes/skills/pku_skills/.env
```

Hermes 通过 `${HERMES_SKILL_DIR}` 变量自动解析安装目录，**无需手动配置路径**，macOS / Linux / Windows 均可直接使用。

**方式二：配置外部 skills 目录**

在 `~/.hermes/config.yaml` 中添加：

```yaml
skills:
  external_dirs:
    - ~/path/to/pku_skills
```

---

## 更新

```bash
cd ~/.openclaw/skills/pku_skills  # 或对应安装目录
git pull origin master
pip install -r requirements.txt   # 如有新依赖
```

---

## 卸载

```bash
rm -rf ~/.openclaw/skills/pku_skills   # Open Claw / QClaw
rm -rf ~/.hermes/skills/pku_skills     # Hermes
```

---

## 常见问题

**Q：登录失败 / Cookie 失效**
```bash
rm .pku_session.json  # 删除缓存，下次自动重新登录
```

**Q：网络服务提示 OTP 错误**

OTP 有效期约 30 秒，打开北京大学令牌 App 获取最新口令后立即重试。

**Q：接口返回 404 / 数据结构变化**

查看 `skills/` 目录下对应 YAML 文件的 `extra_api` 字段，或提交 [Issue](https://github.com/zizhizhou/pku_skills/issues)。
