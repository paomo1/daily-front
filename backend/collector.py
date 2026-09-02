"""
每日前线 数据采集器（方案 A · 静态 JSON 版）
==========================================
功能：抓取三模块（LOL / 足球 / AI）的资讯，写入静态 JSON 文件。

设计原则（和上一版 Postgres 版的差异）：
  - 不依赖任何数据库，不依赖 Docker。
  - 每次运行把抓到的条目合并进 data/archive.json（按内容指纹去重），
    再导出 data/today.json 给前端直接 fetch。
  - 前端（每日前线-可点原型.html）fetch('./data/today.json') 即可拿到最新数据；
    本地双击打开拿不到时，自动回退到页面内置的真实快照。

数据源（全部直连可达，无需代理）：
  - 足球：TheSportsDB（免 key，覆盖英超/西甲/德甲/意甲/法甲/欧冠，抓赛程 + 战报）
  - AI：行业 RSS（TechCrunch / VentureBeat / 量子位 / 机器之心 / 36氪）
        + HuggingFace Hub API + arXiv（公开，无需 key）；用 LLM 重组成「每日 AI 行业日报」
  - LOL：哔哩哔哩搜索 UGC（海斗黑科技 / 峡谷攻略 / 比赛复盘 / 云顶阵容）

运行：
  本地：  python backend/collector.py
  单模块：python backend/collector.py --modules football
  GitHub Actions：见 .github/workflows/daily-update.yml（跑完自动 commit 数据）

作者：刘亦菲搭子
"""
from __future__ import annotations

import os

# 采集所有源均为直连可达（B站 / TheSportsDB / DashScope），无需代理。
# 必须在 import openai 之前清除代理环境变量——openai 在 import 时即读取并缓存
# HTTPS_PROXY，GitHub Actions runner 自带该变量会触发新版 openai+httpx 的
# `proxies` 参数崩溃（TypeError: unexpected keyword argument 'proxies'）。
for _p in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
    os.environ.pop(_p, None)

import re
import sys
import json
import time
import hashlib
import argparse
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
import feedparser
from dateutil import parser as dtparser
from dotenv import load_dotenv
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ============================================================
# 配置
# ============================================================
load_dotenv(Path(__file__).resolve().parent / ".env")  # 总是读 backend/.env，无论从仓库根还是 backend 目录运行

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DASHSCOPE_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen-plus")

FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
FOOTBALL_BASE_URL = os.getenv("FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")

HF_TOKEN = os.getenv("HF_TOKEN", "")
ARXIV_MAX_RESULTS = int(os.getenv("ARXIV_MAX_RESULTS", "20"))

ENABLE_LOL = os.getenv("ENABLE_LOL", "true").lower() == "true"
ENABLE_FOOTBALL = os.getenv("ENABLE_FOOTBALL", "true").lower() == "true"
ENABLE_AI = os.getenv("ENABLE_AI", "true").lower() == "true"

MAX_FETCH_PER_SOURCE = int(os.getenv("MAX_FETCH_PER_SOURCE", "50"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "7"))

HTTP_TIMEOUT = 20

# 数据目录：固定写到仓库根目录的 data/（与前端 fetch('./data/today.json') 对应）
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daily-front")

# OpenAI 兼容客户端（DashScope），仅用于摘要；无 key 时跳过摘要
llm = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL, timeout=HTTP_TIMEOUT) if DASHSCOPE_API_KEY else None


# ============================================================
# 工具函数
# ============================================================
def content_hash(title: str, source_url: str) -> str:
    """稳定的内容指纹（幂等去重用）"""
    s = (title.strip() + "|" + (source_url or "").strip()).encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def normalize_dt(dt_str: str | datetime) -> datetime:
    """统一时区：源站时间字符串 → 感知时区的 datetime（UTC）"""
    if isinstance(dt_str, datetime):
        dt = dt_str
    else:
        dt = dtparser.parse(str(dt_str))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso(s: str) -> datetime:
    return normalize_dt(s)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def http_get(url: str, headers: dict | None = None, params: dict | None = None) -> requests.Response:
    r = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


def dashscope_summarize(text: str, max_words: int = 60) -> str:
    """用 qwen-plus 把长文本压成 1-2 句中文摘要；无 key 或失败则截断原文"""
    if not text or len(text) < 50:
        return text
    if llm is None:
        return text[:120].strip()
    try:
        resp = llm.chat.completions.create(
            model=DASHSCOPE_MODEL,
            messages=[
                {"role": "system", "content": f"你是一名中文体育/游戏/科技资讯编辑，请把下面内容压缩成 {max_words} 字以内的中文摘要，保留关键事实和数字。"},
                {"role": "user", "content": text[:1500]},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"summarize 失败，使用截断原文: {e}")
        return text[:120].strip()


def dashscope_daily(items: list[dict]) -> dict | None:
    """把当天抓到的 AI 条目，用 qwen-plus 重组成「每日 AI 行业日报」结构。

    返回 {insight, headline:{title,summary,source,sourceUrl},
          sections:[{name, items:[{title,summary,source,sourceUrl}]}]}
    无 key 或失败返回 None（前端自动降级为平铺）。
    """
    if not items:
        return None
    if llm is None:
        logger.info("无 DASHSCOPE_API_KEY，跳过 AI 日报编辑（前端降级平铺）")
        return None

    brief = []
    for it in items[:40]:
        brief.append(
            f"- [{it.get('cat', '')}] {it.get('title', '')}"
            f"｜{it.get('summary', '')[:80]}｜来源:{it.get('source', '')}"
        )
    prompt = "\n".join(brief)

    sys_prompt = (
        "你是资深 AI 行业分析师，负责把下面一堆零散的 AI 资讯编辑成一份"
        "「每日 AI 行业日报」。请只输出 JSON（不要 markdown 代码块、不要解释），结构如下：\n"
        "{\n"
        '  "insight": "一句话今日洞察，点出今天 AI 圈最值得关注的趋势（30字内）",\n'
        '  "headline": {"title": "", "summary": "", "source": "", "sourceUrl": ""},\n'
        '  "sections": [\n'
        '    {"name": "最新科技", "items": [{"title":"","summary":"","source":"","sourceUrl":""}]},\n'
        '    {"name": "行业动态", "items": [{"title":"","summary":"","source":"","sourceUrl":""}]},\n'
        '    {"name": "知识前沿", "items": [{"title":"","summary":"","source":"","sourceUrl":""}]},\n'
        '    {"name": "应用落地", "items": [{"title":"","summary":"","source":"","sourceUrl":""}]}\n'
        "  ]\n"
        "}\n"
        "硬性要求：\n"
        "1. 每条 item 的 title/summary/source/sourceUrl 必须原样照搬自输入列表，严禁编造或改写。\n"
        "2. headline 选今天最重要/最具代表性的一条；headline 与 sections 里的条目尽量不重复。\n"
        "3. 每个 section 放 2-4 条；按语义归类（科技突破→最新科技，公司/融资/政策→行业动态，"
        "论文/方法论→知识前沿，产品/场景落地→应用落地）。\n"
        "4. 若某 section 无对应内容，返回空 items 数组即可。"
    )
    try:
        resp = llm.chat.completions.create(
            model=DASHSCOPE_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt[:6000]},
            ],
            temperature=0.4,
            max_tokens=1600,
        )
        text = (resp.choices[0].message.content or "").strip()
        # 去掉可能的 ```json ... ``` 包裹
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"AI 日报编辑失败，前端将降级平铺: {e}")
        return None


def canon(raw: dict) -> dict:
    """把采集器产出的原始 dict 规范成前端消费的标准条目"""
    pub = normalize_dt(raw.get("publishedAt") or datetime.now(timezone.utc))
    h = content_hash(raw.get("title", ""), raw.get("sourceUrl") or raw.get("source_url") or "")
    body = raw.get("body")
    if isinstance(body, str):
        body = [body] if body.strip() else []
    elif not isinstance(body, list):
        body = []
    return {
        "id": h,
        "module": raw.get("module"),
        "seg": raw.get("seg"),
        "cat": raw.get("cat") or raw.get("category") or "",
        "title": raw.get("title", ""),
        "summary": raw.get("summary", ""),
        "source": raw.get("source") or raw.get("source_name") or "",
        "sourceUrl": raw.get("sourceUrl") or raw.get("source_url") or "",
        "publishedAt": pub.isoformat(),
        "tags": raw.get("tags", []),
        "body": body,
        "points": raw.get("points", []),
        "live": bool(raw.get("live", False)),
        "extra": raw.get("extra", {}),
    }


# ============================================================
# 数据存储器（替代数据库）
# ============================================================
class Store:
    """用 JSON 文件替代 Postgres：archive.json 全量去重，today.json 取近 N 天"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_path = self.data_dir / "archive.json"
        self.today_path = self.data_dir / "today.json"
        self.archive: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if self.archive_path.exists():
            try:
                return json.loads(self.archive_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"archive.json 读取失败，重建: {e}")
        return {}

    def merge(self, items: list[dict]) -> int:
        added = 0
        for it in items:
            h = it["id"]
            if h not in self.archive:
                self.archive[h] = it
                added += 1
        return added

    def save(self) -> int:
        # 全量存档
        self.archive_path.write_text(
            json.dumps(self.archive, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 近 N 天导出给前端
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        recent = [it for it in self.archive.values() if parse_iso(it["publishedAt"]) >= cutoff]
        recent.sort(key=lambda x: x["publishedAt"], reverse=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "keep_days": KEEP_DAYS,
            "count": len(recent),
            "items": recent,
        }
        self.today_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return len(recent)


# ============================================================
# 抽象采集器
# ============================================================
class BaseCollector(ABC):
    module: str  # 'lol' / 'football' / 'ai'

    @abstractmethod
    def fetch(self) -> Iterable[dict]:
        """返回标准化原始条目列表（每条需含 module / title / summary / source* / publishedAt）"""
        ...


# ============================================================
# 足球采集器（API-Football）
# ============================================================
class FootballCollector(BaseCollector):
    module = "football"

    # TheSportsDB 免费层：路径里自带公开测试 key "3"，无需注册、无需 key、零人机验证。
    # 免费层覆盖英超/西甲/德甲/意甲/法甲/欧冠等主流联赛，限速约 1 请求/秒，故每次请求后 sleep。
    # 注意：免费层提供「赛程(next) / 历史结果(past)」，不含实时比分推送；时间为 UTC，前端 +8 显示北京时间。
    BASE = "https://www.thesportsdb.com/api/v1/json/3"
    # (thesportsdb 联赛 id, 中文名) —— 与前端 renderFootball 的 chip 过滤对齐
    LEAGUES = [
        (4328, "英超"), (4335, "西甲"), (4331, "德甲"),
        (4332, "意甲"), (4334, "法甲"), (4480, "欧冠"),
    ]

    def __init__(self):
        self.struct = {"fixtures": [], "results": []}

    def fetch(self) -> Iterable[dict]:
        for lid, zh in self.LEAGUES:
            # 下一轮赛程
            try:
                nj = http_get(f"{self.BASE}/eventsnextleague.php", params={"id": lid}).json()
                for e in (nj.get("events") or [])[:6]:
                    self.struct["fixtures"].append(self._map(e, zh, finished=False))
            except Exception as ex:
                logger.warning(f"足球 赛程[{zh}] 失败: {ex}")
            # 上一轮战报（past 接口返回已结束赛事，过滤 strStatus 兜底）
            try:
                pj = http_get(f"{self.BASE}/eventspastleague.php", params={"id": lid}).json()
                evs = pj.get("events") or []
                finished = [e for e in evs if str(e.get("strStatus", "")).lower() in ("match finished", "finished")]
                for e in (finished or evs)[:5]:
                    self.struct["results"].append(self._map(e, zh, finished=True))
            except Exception as ex:
                logger.warning(f"足球 战报[{zh}] 失败: {ex}")
            time.sleep(1.0)  # 免费层限速

        # 同时产出 ITEMS 填底部「足球资讯」栏（赛程/战报摘要）
        for f in self.struct["fixtures"][:8]:
            yield self._to_item(f, "赛程")
        for r in self.struct["results"][:5]:
            yield self._to_item(r, "战报")

    def _map(self, e: dict, zh: str, finished: bool) -> dict:
        t_utc = f"{e.get('dateEvent', '')}T{e.get('strTime', '00:00:00')}"
        try:
            dt = datetime.fromisoformat(t_utc.replace("Z", ""))
            dt = dt + timedelta(hours=8)  # UTC → 北京时间（估算）
            t = dt.strftime("%H:%M")
        except Exception:
            t = (e.get("strTime") or "00:00")[:5]
        hs = e.get("intHomeScore")
        as_ = e.get("intAwayScore")
        s = f"{hs}-{as_}" if finished and hs is not None else None
        return {
            "t": t,
            "lg": zh,
            "h": e.get("strHomeTeam", "?"),
            "a": e.get("strAwayTeam", "?"),
            "s": s,
            "itemId": "fb-" + str(e.get("idEvent", "")),
            "url": e.get("strVideo") or f"https://www.thesportsdb.com/event/{e.get('idEvent', '')}",
        }

    def _to_item(self, row: dict, note: str) -> dict:
        score = f" {row['s']} " if row.get("s") else " VS "
        return {
            "module": "football",
            "cat": row["lg"],
            "title": f"{row['h']}{score}{row['a']}",
            "summary": f"{row['lg']} · {note} · {row['t']}（北京时间，估算）",
            "body": None,
            "source": "TheSportsDB",
            "sourceUrl": row["url"],
            "publishedAt": datetime.now(timezone.utc).isoformat(),
            "tags": [row["lg"], note],
            "live": False,
            "extra": {},
            "id": row["itemId"],
        }

    def build_football(self) -> dict | None:
        return self.struct if (self.struct["fixtures"] or self.struct["results"]) else None


# ============================================================
# AI 采集器（行业 RSS + HuggingFace + arXiv → 每日 AI 行业日报）
# ============================================================
class AICollector(BaseCollector):
    module = "ai"

    HF_API = "https://huggingface.co/api"
    ARXIV_API = "http://export.arxiv.org/api/query"

    # 行业 RSS（公开，无需 key）；单个源失败自动跳过，不影响其他
    FEEDS = [
        ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch"),
        ("https://venturebeat.com/category/ai/feed/", "VentureBeat"),
        ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "The Verge"),
        ("https://arstechnica.com/ai/feed/", "Ars Technica"),
        ("https://www.qbitai.com/feed", "量子位"),
        ("https://www.jiqizhixin.com/rss", "机器之心"),
        ("https://36kr.com/feed", "36氪"),
    ]

    def fetch(self) -> Iterable[dict]:
        yield from self._fetch_feeds()
        yield from self._fetch_hf_models()
        yield from self._fetch_arxiv_papers()

    def _fetch_feeds(self) -> Iterable[dict]:
        for feed_url, source_name in self.FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:8]:
                    raw = entry.get("summary", entry.get("description", ""))
                    summary = dashscope_summarize(raw) or raw[:150]
                    yield {
                        "module": "ai",
                        "cat": "行业动态",
                        "title": entry.title.replace("\n", " ").strip(),
                        "summary": summary,
                        "body": None,
                        "source": source_name,
                        "sourceUrl": entry.link,
                        "publishedAt": entry.get("published", datetime.now(timezone.utc).isoformat()),
                        "tags": ["ai-news", source_name],
                        "extra": {},
                    }
            except Exception as e:
                logger.warning(f"AI feed {source_name} 失败: {e}")

    def _fetch_hf_models(self) -> Iterable[dict]:
        url = f"{self.HF_API}/models"
        params = {"sort": "lastModified", "direction": -1, "limit": 20}
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        try:
            data = http_get(url, headers=headers, params=params).json()
        except Exception as e:
            logger.warning(f"HF models 失败: {e}")
            return

        for m in data[:15]:
            model_id = m.get("id", "")
            pipeline = (m.get("pipeline_tag") or "nlp")
            last_mod = m.get("lastModified")
            if not last_mod or not model_id:
                continue
            title = f"{model_id.split('/')[-1]}：{pipeline} 模型更新"
            summary = dashscope_summarize(
                f"HuggingFace 热门 {pipeline} 模型 {model_id}，{m.get('downloads', 0):,} 下载，{m.get('likes', 0)} 赞"
            )
            yield {
                "module": "ai",
                "cat": "模型/技术",
                "title": title,
                "summary": summary,
                "body": None,
                "source": "HuggingFace",
                "sourceUrl": f"https://huggingface.co/{model_id}",
                "publishedAt": last_mod,
                "tags": ["huggingface", pipeline],
                "extra": {"downloads": m.get("downloads", 0), "likes": m.get("likes", 0)},
            }

    def _fetch_arxiv_papers(self) -> Iterable[dict]:
        url = self.ARXIV_API
        params = {
            "search_query": "cat:cs.AI OR cat:cs.CL OR cat:cs.LG",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": ARXIV_MAX_RESULTS,
        }
        try:
            r = http_get(url, params=params)
            feed = feedparser.parse(r.content)
        except Exception as e:
            logger.warning(f"arXiv 失败: {e}")
            return

        for entry in feed.entries[:15]:
            title = entry.title.replace("\n", " ").strip()
            authors = ", ".join([a.name for a in entry.get("authors", [])][:3])
            if len(entry.get("authors", [])) > 3:
                authors += " et al."
            abstract = entry.get("summary", "").replace("\n", " ").strip()
            link = entry.get("link", "")
            summary = dashscope_summarize(f"{title}。作者：{authors}。摘要：{abstract[:600]}")
            yield {
                "module": "ai",
                "cat": "知识前沿",
                "title": title,
                "summary": summary or abstract[:120],
                "body": abstract,
                "source": "arXiv",
                "sourceUrl": link,
                "publishedAt": entry.get("published", datetime.now(timezone.utc).isoformat()),
                "tags": ["arxiv", "research"],
                "extra": {"authors": authors, "categories": [t.get("term") for t in entry.get("tags", [])]},
            }

    def build_daily(self, items: list[dict]) -> dict | None:
        """抓完后由 main() 调用：把当天 AI 条目重组成日报结构"""
        ai_items = [it for it in items if it.get("module") == "ai"]
        return dashscope_daily(ai_items)


# ============================================================
# LOL 采集器（RSS 公开源）
# ============================================================
class LOLCollector(BaseCollector):
    module = "lol"

    # 单源策略（前端 renderLol 按 seg 分 4 个 tab：rune=海斗资讯 / rift=峡谷攻略 / match=比赛 / tft=云顶之弈）：
    #  哔哩哔哩搜索 —— 玩家向 UGC 视频（海斗黑科技 / 云顶阵容 / 比赛复盘），内地直连可达，
    #     家庭宽带通常不被风控；裸 API 偶发返回验证码页（数据中心 IP 易触发），已做容错跳过。
    # 任一关键词失败仅打 warning，不阻塞其他关键词；前端按 seg 自动归位到对应 tab。
    #
    # 注：Google News 兜底源已移除——内地直连被墙、0 贡献，属死代码；B站已稳定出数。
    # 代理自愈见 auto_resolve_proxy：本机 Clash 开/没开都能跑，自动判定走代理还是直连。

    # (seg, cat, B站搜索关键词, 取前N条) —— 玩家向 UGC 主体
    BILI_SEARCHES = [
        ("rune", "海斗资讯", "英雄联盟 海斗 黑科技 上分", 5),
        ("rune", "海斗资讯", "英雄联盟 海斗 强化 套路", 3),
        ("rift", "峡谷攻略", "英雄联盟 上单 出装 攻略 教学", 5),
        ("match", "比赛", "英雄联盟 LPL LCK 比赛 集锦 复盘", 5),
        ("tft",  "云顶之弈", "云顶之弈 阵容 攻略 S级", 5),
    ]

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://search.bilibili.com/",
    }

    def fetch(self) -> Iterable[dict]:
        yield from self._fetch_bili()

    # ---------------- 哔哩哔哩（玩家向 UGC） ----------------
    def _fetch_bili(self) -> Iterable[dict]:
        for seg, cat, kw, topn in self.BILI_SEARCHES:
            try:
                items = self._bili_search(kw, topn)
                for it in items:
                    it["seg"] = seg
                    it["cat"] = cat
                    it["tags"] = ["lol", seg, kw]
                    yield it
                logger.info(f"B站搜索[{kw}] 抓取 {len(items)} 条")
            except Exception as e:
                logger.warning(f"B站搜索[{kw}] 失败: {e}")

    def _bili_search(self, keyword: str, topn: int) -> list[dict]:
        url = "https://api.bilibili.com/x/web-interface/search/type"
        params = {"search_type": "video", "keyword": keyword, "page": 1}
        r = http_get(url, headers=self.HEADERS, params=params)
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("application/json"):
            raise RuntimeError(f"非 JSON 响应（可能被风控）: {r.text[:80]!r}")
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"B站返回 code={data.get('code')} msg={data.get('message')}")
        out: list[dict] = []
        for v in (data.get("data", {}).get("result") or [])[:topn]:
            title = re.sub(r"<[^>]+>", "", v.get("title", "")).strip()
            author = v.get("author", "")
            play = v.get("play") or 0
            bvid = v.get("bvid", "")
            duration = v.get("duration", "")
            arcurl = v.get("arcurl") or f"https://www.bilibili.com/video/{bvid}"
            pub = v.get("pubdate") or v.get("senddate") or 0
            try:
                pub_iso = datetime.fromtimestamp(int(pub), tz=timezone.utc).isoformat()
            except Exception:
                pub_iso = datetime.now(timezone.utc).isoformat()
            out.append({
                "module": "lol",
                "title": title,
                "summary": f"UP主 {author} · {play:,}播放 · {duration}",
                "body": None,
                "source": "哔哩哔哩",
                "sourceUrl": arcurl,
                "publishedAt": pub_iso,
                "extra": {"author": author, "play": play, "bvid": bvid, "duration": duration},
            })
        return out

    # ---------------- Google News（媒体新闻兜底） ----------------
    # 已移除：内地直连被墙、0 贡献，属死代码。保留占位注释以明确决策。


# ============================================================
# 代理自愈
# ============================================================
def auto_resolve_proxy() -> None:
    """代理自愈：检测到 HTTPS_PROXY/HTTP_PROXY 时先探一次，
    若代理不可达（如本机 Clash 没开）则自动 pop 掉 env，回退直连，
    避免整模块因 proxy 连接被拒而 0 条。

    GitHub Actions 沙箱无此 env，直接走直连，不受影响。
    """
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if not proxy:
        logger.info("未检测到代理环境变量，使用直连模式")
        return
    try:
        # 强制走代理探测一个快返回点（204），timeout 短，失败立即回退
        with requests.Session() as s:
            s.proxies = {"http": proxy, "https": proxy}
            s.get("https://www.google.com/generate_204", timeout=4)
        logger.info(f"代理 {proxy} 探测可达，使用代理模式")
    except Exception as e:
        logger.warning(f"代理 {proxy} 不可达（{type(e).__name__}），自动回退直连模式")
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)


# ============================================================
# 主入口
# ============================================================
def main():
    auto_resolve_proxy()  # 代理自愈：先决定走代理还是直连，再发任何请求
    parser = argparse.ArgumentParser(description="每日前线 数据采集器（静态 JSON 版）")
    parser.add_argument(
        "--modules",
        default="all",
        choices=["all", "lol", "football", "ai"],
        help="只跑指定模块（默认 all）",
    )
    args = parser.parse_args()

    collectors: list[BaseCollector] = []
    if args.modules in ("all", "football") and ENABLE_FOOTBALL:
        collectors.append(FootballCollector())
    if args.modules in ("all", "ai") and ENABLE_AI:
        collectors.append(AICollector())
    if args.modules in ("all", "lol") and ENABLE_LOL:
        collectors.append(LOLCollector())

    if not collectors:
        logger.error("无启用模块，请检查 ENABLE_* 环境变量")
        sys.exit(1)

    logger.info(f"开始采集：{[c.module for c in collectors]}")
    raw_items: list[dict] = []
    for c in collectors:
        try:
            fetched = list(c.fetch())
            logger.info(f"[{c.module}] 抓取 {len(fetched)} 条")
            raw_items.extend(fetched)
        except Exception as e:
            logger.error(f"[{c.module}] 采集失败: {e}")

    # 规范成标准条目
    items = [canon(x) for x in raw_items if x.get("title")]

    # 写入 JSON（替代数据库）
    store = Store(DATA_DIR)
    added = store.merge(items)
    total = store.save()

    # 生成每日日报（按模块；提供 build_daily 的采集器才会产出）
    daily = {}
    for c in collectors:
        if hasattr(c, "build_daily"):
            try:
                d = c.build_daily(items)
                if d:
                    daily[c.module] = d
            except Exception as e:
                logger.warning(f"[{c.module}] 日报生成失败: {e}")

    if daily:
        try:
            tp = DATA_DIR / "today.json"
            payload = json.loads(tp.read_text(encoding="utf-8"))
            payload["daily"] = daily
            tp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已写入日报模块：{[k for k in daily]}")
        except Exception as e:
            logger.warning(f"日报写入 today.json 失败: {e}")

    # 生成足球结构化赛程/战报（提供 build_football 的采集器才产出）
    fb = None
    for c in collectors:
        if hasattr(c, "build_football"):
            try:
                d = c.build_football()
                if d:
                    fb = d
            except Exception as e:
                logger.warning(f"[{c.module}] 足球结构化生成失败: {e}")
    if fb:
        try:
            tp = DATA_DIR / "today.json"
            payload = json.loads(tp.read_text(encoding="utf-8"))
            payload["football"] = fb
            tp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已写入足球结构化：赛程 {len(fb['fixtures'])} 战报 {len(fb['results'])}")
        except Exception as e:
            logger.warning(f"足球结构化写入 today.json 失败: {e}")

    logger.info(f"本次新增 {added} 条，近 {KEEP_DAYS} 天共 {total} 条 → {DATA_DIR / 'today.json'}")


if __name__ == "__main__":
    main()
