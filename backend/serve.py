"""
本地预览静态服务器（零依赖，仅用 Python 标准库）
================================================
用途：前端原型通过 fetch('./data/today.json') 读数据，
但双击打开（file://）时浏览器禁止本地 fetch，必须起个 http 服务。

用法：
    python backend/serve.py
然后浏览器打开 http://localhost:8080/   （即 index.html；也可显式访问 /index.html）

线上则不需要这个 —— 直接把仓库开 GitHub Pages 即可。
"""
from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("PORT", "8080"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

    def end_headers(self):
        # 避免 JSON 被缓存导致看不到更新
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默


if __name__ == "__main__":
    os.chdir(REPO_ROOT)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"📡 每日前线预览：http://localhost:{PORT}/")
    print(f"   数据目录：{REPO_ROOT / 'data'}")
    print("   按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止")
        sys.exit(0)
