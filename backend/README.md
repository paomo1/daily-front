# 每日前线 · 后端（方案 A：静态 JSON，零数据库零 Docker）

> 每天定时抓取三模块（英雄联盟 / 足球 / AI）资讯，写成 `data/today.json`，
> 前端原型 `fetch('./data/today.json')` 直接读。整套**不需要 PostgreSQL、不需要 Docker、不需要常驻服务器**，月费 ¥0。

---

## 这套方案为什么这么轻

| 上一版（已放弃） | 本方案 |
|---|---|
| PostgreSQL + pgvector | 一个 JSON 文件 |
| Docker 容器常驻 | 无 |
| FastAPI 后端服务 | 无（前端直读静态文件）|
| 服务器月费 | ¥0（GitHub 免费额度）|

你说得对——你从没要求用 Postgres，是我把别的项目的 pgvector 思维带过来了。这 app 数据量极小（每天 ~25 条 × 3 模块），静态 JSON + 定时任务完全够用。

数据持久化用的是 `data/archive.json`（按内容指纹去重，全量存档）+ `data/today.json`（取近 7 天给前端）。两者都是普通文件，git 管理，CI 跑完自动提交。

---

## 文件结构

```
backend/
├── collector.py            # 采集器：写 data/today.json（替代数据库）
├── serve.py                # 本地预览用静态服务器（零依赖，仅标准库）
├── requirements.txt        # 只 5 个包
├── .env.example            # 环境变量模板
├── .github/
│   └── workflows/
│       └── daily-update.yml  # GitHub Actions 定时任务
└── README.md

data/                       # 采集产物（前端读这里）
├── today.json              # 近 7 天条目，前端 fetch 的对象
└── archive.json            # 全量去重存档

每日前线-可点原型.html       # 前端（在仓库根目录）
```

---

## 0. 装包（一次性）

PowerShell 风格，统一一种写法（用 ai-base 的 python）：

```powershell
cd "C:\Users\13656\WorkBuddy\2026-09-01-15-30-32\backend"
& "C:\Users\13656\anaconda3\envs\ai-base\python.exe" -m pip install -r requirements.txt -i http://pypi.tuna.tsinghua.edu.cn/simple --proxy http://127.0.0.1:7897
```

---

## 1. 配置环境变量

```powershell
cd "C:\Users\13656\WorkBuddy\2026-09-01-15-30-32\backend"
copy .env.example .env
notepad .env
```

| 变量 | 从哪拿 | 是否必须 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 阿里云百炼控制台（已有 `sk-41287...`） | 建议填，用于把长文压成摘要 |
| `FOOTBALL_API_KEY` | https://www.api-football.com/ 注册 | 选填，留空则跳过足球 |
| `HF_TOKEN` | https://huggingface.co/settings/tokens | 选填 |

> **⚠️ `.env` 不要 git 提交**（已在 `.gitignore`）。key 也千万别截图外传——你之前 DeepSeek key 泄露过。

---

## 2. 本地试跑

```powershell
cd "C:\Users\13656\WorkBuddy\2026-09-01-15-30-32\backend"
& "C:\Users\13656\anaconda3\envs\ai-base\python.exe" collector.py --modules ai
```

预期：抓到若干条 AI 资讯，写入 `..\data\today.json` 和 `..\data\archive.json`。

**单模块调试：**

```powershell
& "C:\Users\13656\anaconda3\envs\ai-base\python.exe" collector.py --modules football
& "C:\Users\13656\anaconda3\envs\ai-base\python.exe" collector.py --modules lol
```

> 没填 `FOOTBALL_API_KEY` 时足球模块会自动跳过，不影响其他模块。

---

## 3. 本地预览前端

前端通过 `fetch('./data/today.json')` 读数据，**双击打开 HTML（file://）会被浏览器禁止本地 fetch**，所以必须起个 http 服务：

```powershell
cd "C:\Users\13656\WorkBuddy\2026-09-01-15-30-32"
& "C:\Users\13656\anaconda3\envs\ai-base\python.exe" backend/serve.py
```

然后浏览器打开 `http://localhost:8080/每日前线-可点原型.html`。

- 有网络 + 跑过 collector → 页面显示 `today.json` 里的最新数据
- 没网络 / 直接双击 → 自动回退到页面**内置的 2026-09-01 真实快照**，照样能点

---

## 4. 部署到 GitHub（全自动每日更新）

### 4.1 推到 GitHub 仓库

```powershell
cd "C:\Users\13656\WorkBuddy\2026-09-01-15-30-32"
git init
git add backend/ data/ 每日前线-可点原型.html
git commit -m "feat: 每日前线 静态 JSON 版"
# 在 GitHub 创建 repo 后
git remote add origin https://github.com/你的用户名/daily-front.git
git push -u origin main
```

### 4.2 配置 Secrets / Variables

GitHub repo → Settings → Secrets and variables → Actions：

| 类型 | 名 | 值 |
|---|---|---|
| Secret | `DASHSCOPE_API_KEY` | `sk-...` |
| Secret | `FOOTBALL_API_KEY` | `xxx`（可空）|
| Secret | `HF_TOKEN` | `hf_...`（可空）|
| Variable | `DASHSCOPE_MODEL` | `qwen-plus`（可选）|

> 注意：没有 `DATABASE_URL` 了，那是上一版的残留，别再配。

### 4.3 开启 GitHub Pages

repo → Settings → Pages → Source 选 **Deploy from a branch** → 分支 `main`、目录 `/(root)`。

之后访问 `https://你的用户名.github.io/daily-front/` 就是线上版，每天 09:00 / 20:00（北京时间）Actions 自动跑、自动提交新数据，刷新页面即更新。

### 4.4 触发方式

- 定时：每天北京时间 09:00 / 20:00 自动跑
- 手动：Actions → Daily Update → Run workflow → 选模块
- 改了 `collector.py`：push 后自动跑一次

---

## 5. 前端怎么接数据（已接好，了解即可）

`每日前线-可点原型.html` 末尾有一段 `loadLive()`：

```js
const r = await fetch('./data/today.json', {cache:'no-store'});
const j = await r.json();
if (j && Array.isArray(j.items) && j.items.length) {
  ITEMS.length = 0;
  j.items.forEach(x => ITEMS.push(x));   // 用最新数据覆盖内置快照
}
```

`today.json` 结构：

```json
{
  "generated_at": "2026-09-01T16:40:00+00:00",
  "date": "2026-09-01",
  "keep_days": 7,
  "count": 25,
  "items": [ { "id", "module", "cat", "title", "summary", "source", "sourceUrl", "publishedAt", "tags", "body", "points", "live", "extra" }, ... ]
}
```

---

## 6. 排错

| 现象 | 排查 |
|---|---|
| `ModuleNotFoundError` | 没装包，重跑第 0 步 `pip install -r requirements.txt` |
| 足球 0 条 | `FOOTBALL_API_KEY` 没填或超额（免费层 100 req/天）|
| 摘要是原文截断 | `DASHSCOPE_API_KEY` 无效/无 key，已自动回退 |
| 本地页面空白/没数据 | 没起 http 服务（直接双击了）→ 跑 `serve.py` |
| Actions 报权限错误 | 确认 repo Settings → Actions 有 `contents: write`（workflow 已带 `permissions`）|

---

## 7. 扩展点

| 需求 | 怎么改 |
|---|---|
| 加新模块（股票/电影）| 新写一个 `XxxCollector(BaseCollector)`，在 `main()` 注册 |
| 中文 LOL 源 | 替换 `LOLCollector.FEEDS`（掌上英雄联盟 / 玩加电竞需反爬处理）|
| 真·实时比分 | 前端直接调免费足球 API（或 Actions 每小时跑一次 cron）；本方案 cron 频率够日常用 |
| 减少 API 成本 | 加个 `summary_cache`（同内容指纹不重跑 DashScope 摘要）|
| 想要搜索 | 前端对 `ITEMS` 做个 `filter` 即可，数据量小用不着后端检索 |
