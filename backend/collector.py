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

数据源：
  - 足球：API-Football（免费 100 req/天，覆盖五大联赛 + 欧冠）
  - AI：HuggingFace Hub API + arXiv（公开，无需 key）
  - LOL：Reddit / 拳头官博 RSS（公开）

运行：
  本地：  python backend/collector.py
  单模块：python backend/collector.py --modules football
  GitHub Actions：见 .github/workflows/daily-update.yml（跑完自动 commit 数据）

作者：刘亦菲搭子
"""
from __future__ import annotations

import os
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

    def fetch(self) -> Iterable[dict]:
        if not FOOTBALL_API_KEY:
            logger.warning("FOOTBALL_API_KEY 未配置，跳过足球模块")
            return []
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yield from self._fetch_fixtures(headers, today)

    def _fetch_fixtures(self, headers: dict, date: str) -> Iterable[dict]:
        url = urljoin(FOOTBALL_BASE_URL, "/fixtures")
        try:
            data = http_get(url, headers=headers, params={"date": date}).json()
        except Exception as e:
            logger.warning(f"football fixtures 失败: {e}")
            return

        for f in data.get("response", [])[:30]:
            league = f.get("league", {})
            teams = f.get("teams", {})
            goals = f.get("goals", {})
            fixture = f.get("fixture", {})
            status = f.get("fixture", {}).get("status", {}).get("short", "NS")
            home = teams.get("home", {}).get("name", "?")
            away = teams.get("away", {}).get("name", "?")
            hg = goals.get("home")
            ag = goals.get("away")
            lg = league.get("name", "?")

            if status in ("NS", "TBD"):
                title = f"{home} VS {away}"
                summary = f"{lg} · {league.get('round', '?')} · {fixture.get('date', '')[:16]}"
            else:
                title = f"{home} {hg}-{ag} {away}"
                summary = f"{lg} · {status} · {league.get('round', '?')}"

            yield {
                "module": "football",
                "cat": lg,
                "title": title,
                "summary": summary,
                "body": None,
                "source": "API-Football",
                "sourceUrl": urljoin("https://www.fotmob.com/", f"match/{fixture.get('id', '')}"),
                "publishedAt": fixture.get("date", datetime.now(timezone.utc).isoformat()),
                "tags": [lg, status],
                "live": status in ("1H", "2H", "HT", "ET", "P"),
                "extra": {"home": home, "away": away, "league": lg, "status": status,
                          "home_score": hg, "away_score": ag},
            }


# ============================================================
# AI 采集器（HuggingFace + arXiv）
# ============================================================
class AICollector(BaseCollector):
    module = "ai"

    HF_API = "https://huggingface.co/api"
    ARXIV_API = "http://export.arxiv.org/api/query"

    def fetch(self) -> Iterable[dict]:
        yield from self._fetch_hf_models()
        yield from self._fetch_arxiv_papers()

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
                "cat": "模型发布",
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
                "cat": "知识",
                "title": title,
                "summary": summary or abstract[:120],
                "body": abstract,
                "source": "arXiv",
                "sourceUrl": link,
                "publishedAt": entry.get("published", datetime.now(timezone.utc).isoformat()),
                "tags": ["arxiv", "research"],
                "extra": {"authors": authors, "categories": [t.get("term") for t in entry.get("tags", [])]},
            }


# ============================================================
# LOL 采集器（RSS 公开源）
# ============================================================
class LOLCollector(BaseCollector):
    module = "lol"

    FEEDS = [
        ("https://www.reddit.com/r/leagueoflegends/.rss", "reddit-lol"),
        ("https://www.leagueoflegends.com/en-us/news/rss.xml", "lol-official-en"),
    ]

    def fetch(self) -> Iterable[dict]:
        for feed_url, source_name in self.FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:10]:
                    summary = dashscope_summarize(
                        entry.get("summary", entry.get("description", ""))
                    ) or entry.get("summary", "")[:120]
                    yield {
                        "module": "lol",
                        "seg": "rune",
                        "cat": "海斗资讯",
                        "title": entry.title,
                        "summary": summary,
                        "body": entry.get("content", [{}])[0].get("value", "") if entry.get("content") else None,
                        "source": source_name,
                        "sourceUrl": entry.link,
                        "publishedAt": entry.get("published", datetime.now(timezone.utc).isoformat()),
                        "tags": ["lol", source_name],
                        "extra": {},
                    }
            except Exception as e:
                logger.warning(f"LOL feed {feed_url} 失败: {e}")


# ============================================================
# 主入口
# ============================================================
def main():
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
    logger.info(f"本次新增 {added} 条，近 {KEEP_DAYS} 天共 {total} 条 → {DATA_DIR / 'today.json'}")


if __name__ == "__main__":
    main()
