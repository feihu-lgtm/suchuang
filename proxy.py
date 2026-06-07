"""
速创 GPT-Image-2 本地代理 (v3 - 安全版)
─────────────────────────────────────────
- API key 从环境变量或同目录的 .env 文件读取, 不写进代码
- 解决浏览器 CORS, 把异步轮询封装成一次同步调用
- 仅 Python 3.7+ 标准库, 不需要 pip

启动:
  方式一 (.env 文件):
    1. 复制 .env.example 为 .env
    2. 把 .env 里 SUKE_API_KEY=xxx 改成你的真实 key
    3. 终端: python3 proxy.py

  方式二 (临时设环境变量):
    SUKE_API_KEY=你的key python3 proxy.py
"""

import json
import os
import sys
import time
import ssl
import urllib.request
import urllib.error
import urllib.parse
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

# 全局 SSL context：速创等上游证书偶发 hostname mismatch，统一跳过校验，
# 所有对外 urlopen 都带上它，避免个别请求因证书问题直接报错。
SSL_CTX = ssl._create_unverified_context()

# ════════════════════════════════════════════════════════
# 输出目录（画廊）— 可由前端设置，存到配置文件，跨重启保留
# 优先级：配置文件 > 环境变量 GALLERY_DIR > 默认 Documents/速创画廊
# ════════════════════════════════════════════════════════
DEFAULT_GALLERY = Path.home() / "Documents" / "速创画廊"
# 配置文件放在 proxy.py 同目录，记住用户选的目录
_CFG_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / ".gallery_dir"

def _read_gallery_dir():
    # 1. 配置文件
    try:
        if _CFG_PATH.exists():
            p = _CFG_PATH.read_text(encoding="utf-8").strip()
            if p:
                return Path(p).expanduser()
    except Exception:
        pass
    # 2. 环境变量
    env = os.environ.get("GALLERY_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    # 3. 默认
    return DEFAULT_GALLERY

# 全局当前目录（运行中可被 /set-gallery-dir 改）
GALLERY_DIR = _read_gallery_dir()

def gallery_dir():
    """所有存取都通过它拿当前输出目录，并确保目录存在"""
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    return GALLERY_DIR

def set_gallery_dir(new_path):
    """切换输出目录并持久化到配置文件，返回解析后的绝对路径字符串"""
    global GALLERY_DIR
    p = Path(new_path).expanduser()
    p.mkdir(parents=True, exist_ok=True)   # 建不出来会抛异常，由调用方捕获
    GALLERY_DIR = p
    try:
        _CFG_PATH.write_text(str(p), encoding="utf-8")
    except Exception:
        pass
    return str(p.resolve())

# ════════════════════════════════════════════════════════════
# 云端共享画廊 — gallery.json（与 IndexedDB 双写，共享给所有用户）
# ════════════════════════════════════════════════════════════
GALLERY_JSON = Path(os.path.dirname(os.path.abspath(__file__))) / "gallery.json"
_GALLERY_LOCK = threading.Lock()

def _gj_read():
    try:
        if GALLERY_JSON.exists():
            return json.loads(GALLERY_JSON.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def _gj_write(records):
    GALLERY_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def gj_list():
    with _GALLERY_LOCK:
        return _gj_read()

def gj_save(record):
    """新增或覆盖更新（按 id 匹配）"""
    with _GALLERY_LOCK:
        records = _gj_read()
        rid = record.get("id")
        for i, r in enumerate(records):
            if r.get("id") == rid:
                records[i] = {**r, **record}
                _gj_write(records)
                return
        records.insert(0, record)
        _gj_write(records)

def gj_delete(record_id):
    with _GALLERY_LOCK:
        records = _gj_read()
        records = [r for r in records if r.get("id") != record_id]
        _gj_write(records)

def gj_update(record_id, patch):
    """局部更新（只改传入的字段，比如 tags）"""
    with _GALLERY_LOCK:
        records = _gj_read()
        for i, r in enumerate(records):
            if r.get("id") == record_id:
                records[i] = {**r, **patch, "id": record_id}
                _gj_write(records)
                return

def pick_folder_dialog(initial=None):
    """弹出系统原生「选择文件夹」窗口，返回选中的路径；用户取消返回 ""。
    没有 tkinter / 无图形界面时抛 RuntimeError，由调用方退回手填。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError(f"该电脑的 Python 没有图形界面组件（tkinter）：{e}")
    root = None
    try:
        root = tk.Tk()
        root.withdraw()                 # 不显示主窗，只要对话框
        root.attributes("-topmost", True)  # 强制置顶，别被浏览器盖住
        try:
            root.update()
        except Exception:
            pass
        chosen = filedialog.askdirectory(
            title="选择「速创」图片保存到哪个文件夹",
            initialdir=str(initial) if initial else str(gallery_dir()),
        )
        return chosen or ""
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

# ════════════════════════════════════════════════════════
# 强制日志 — 始终写文件，不依赖终端
# ════════════════════════════════════════════════════════
LOG_DIR = gallery_dir()
LOG_FILE = LOG_DIR / "proxy.log"

logger = logging.getLogger("suchuang")
logger.setLevel(logging.DEBUG)
# 文件 handler — 始终写入
fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
fh.setLevel(logging.INFO)  # 只记关键事件; 轮询心跳/HTTP 请求等 debug 不写文件
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)
# 终端 handler — 同时输出
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(ch)

def log(msg, level="info"):
    """同时写文件和终端"""
    getattr(logger, level, logger.info)(msg)

LINK_MAP_FILE = LOG_DIR / "link_map.log"

def write_link_map(records, reason=""):
    """把【文件名 | 本地链 | 图床链】成表写入 link_map.log（覆盖式快照）。
    records: [{file, local, heliar, url}]"""
    import datetime as _dt
    lines = []
    lines.append("=" * 80)
    lines.append(f"链接对照表快照 | {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {reason}")
    lines.append(f"共 {len(records)} 条")
    lines.append("-" * 80)
    lines.append(f"{'文件名':<32} {'本地链':<8} {'图床链(Heliar)':<10} 原始URL")
    lines.append("-" * 80)
    n_local = n_heliar = n_neither = 0
    for r in records:
        f = (r.get("file") or "(无)")[:30]
        has_local = "✓" if r.get("file") else "✗"
        heliar = r.get("heliar") or ""
        has_heliar = "✓永久" if heliar else "✗缺失"
        if r.get("file"): n_local += 1
        if heliar: n_heliar += 1
        if not r.get("file") and not heliar: n_neither += 1
        url = (r.get("url") or "")[:50]
        lines.append(f"{f:<32} {has_local:<8} {has_heliar:<10} {url}")
        if heliar:
            lines.append(f"    └─ 图床: {heliar}")
    lines.append("-" * 80)
    lines.append(f"统计: 有本地文件 {n_local} | 有图床永久链 {n_heliar} | 两者都缺(危险) {n_neither}")
    lines.append("=" * 80)
    text = "\n".join(lines) + "\n"
    try:
        LINK_MAP_FILE.write_text(text, encoding="utf-8")
        log(f"  🗺️ 链接对照表已写入 {LINK_MAP_FILE.name} | {len(records)}条 | 本地{n_local}/图床{n_heliar}/双缺{n_neither} | {reason}")
    except Exception as e:
        log(f"  ✗ 写链接对照表失败: {e}", "warning")
    return {"total": len(records), "local": n_local, "heliar": n_heliar, "neither": n_neither}

def append_link_map(rec, reason=""):
    """追加单行到 link_map.log（增量，生成新图/换绑单张时）"""
    import datetime as _dt
    f = (rec.get("file") or "(无)")[:30]
    heliar = rec.get("heliar") or ""
    line = f"{_dt.datetime.now().strftime('%H:%M:%S')} [{reason}] 文件={f} 本地={'✓' if rec.get('file') else '✗'} 图床={heliar or '✗缺失'}\n"
    try:
        with open(LINK_MAP_FILE, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception as e:
        log(f"  ✗ 追加链接对照表失败: {e}", "warning")

# ════════════════════════════════════════════════════════════
# 配置 (一般不用改)
# ════════════════════════════════════════════════════════════

PORT = 7788
BASE = "https://api.wuyinkeji.com"
SUBMIT_PATH = "/api/async/image_gpt"
DETAIL_PATH = "/api/async/detail"
POLL_INTERVAL = 2
HELIAR_BASE = "https://img.heliar.top"
HELIAR_UPLOAD = HELIAR_BASE + "/upload?uploadChannel=telegram&uploadNameType=default&autoRetry=true&uploadFolder="
POLL_TIMEOUT = 7200  # 2 小时. 生图可能很慢, 不再卡死在 6 分钟; 这只是防服务器僵死的兜底

# ── 异步任务表: 浏览器提交即返回 task_id, 后台线程轮询写状态, 前端短轮询查 ──
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL = 3600  # 任务状态保留 1 小时后清理, 防内存堆积

def task_set(task_id, **kw):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if t is None:
            t = {"task_id": task_id, "status": None, "progress": None,
                 "poll_count": 0, "elapsed": 0, "done": False,
                 "error": None, "result": None, "ts": time.time()}
            TASKS[task_id] = t
        t.update(kw)
        t["ts"] = time.time()

def task_get(task_id):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        return dict(t) if t else None

def task_cleanup():
    now = time.time()
    with TASKS_LOCK:
        for k in [k for k, v in list(TASKS.items()) if now - v.get("ts", now) > TASK_TTL]:
            TASKS.pop(k, None)

# ════════════════════════════════════════════════════════════
# 加载 .env 文件
# ════════════════════════════════════════════════════════════

def load_env_file():
    """从同目录的 .env 文件读取 KEY=VALUE 格式的配置"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

load_env_file()
API_KEY = os.environ.get("SUKE_API_KEY", "").strip() or "0sh88ejThn3MFe3ekCZlgTm2pv"
GLM_API_KEY = os.environ.get("GLM_API_KEY", "").strip() or "c35311ff12ab4c759699385691c22ba8.O8fQhpCwWbzzNwcm"
DS_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip() or "sk-42a6372816e34fbea98b9035ffdaaacc"
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DS_URL = "https://api.deepseek.com/chat/completions"

# ════════════════════════════════════════════════════════════

class ProxyHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ─── /task-status?id=xxx  前端短轮询查任务状态(替代长连接) ───
        if path == "/task-status":
            from urllib.parse import parse_qs
            tid = (parse_qs(parsed.query).get("id", [""])[0]).strip()
            snap = task_get(tid)
            if snap is None:
                self._send_json(404, {"error": "未知任务", "task_id": tid})
            else:
                self._send_json(200, snap)
            return

        # ─── /local-image?file=xxx  读取本地已保存的图片回前端 ───
        # 画廊用它显示本地文件，不再依赖会失效的远程链接
        if path == "/local-image":
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            fname = (qs.get("file", [""])[0]).strip()
            is_ref = (qs.get("ref", ["0"])[0]) == "1"
            base = (gallery_dir() / ("refs" if is_ref else "")).resolve()
            # 防目录穿越：只允许纯文件名，且解析后必须仍在画廊目录内
            if (not fname) or ("/" in fname) or ("\\" in fname) or fname.startswith("."):
                self._send_json(400, {"error": "非法文件名"})
                return
            target = (base / fname).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                self._send_json(403, {"error": "越界访问被拒绝"})
                return
            if not target.is_file():
                self._send_json(404, {"error": "本地文件不存在"})
                return
            import mimetypes
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            try:
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /gallery-index  返回磁盘 index.json（用于把老记录换绑本地文件）───
        if path == "/gallery-index":
            idx_path = gallery_dir() / "index.json"
            try:
                if idx_path.is_file():
                    data = json.loads(idx_path.read_text(encoding="utf-8"))
                else:
                    data = []
                self._send_json(200, {"index": data})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /get-gallery-dir  返回当前输出目录（前端显示用）───
        if path == "/get-gallery-dir":
            try:
                self._send_json(200, {"dir": str(gallery_dir().resolve()), "default": str(DEFAULT_GALLERY.resolve())})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /log-urls  解析 proxy.log，返回所有成功出图的 URL（含被 [RAW] 兜底的）───
        # 用于找回那些前端记录丢失、但日志里有 URL 的图（最后的救命稻草）
        if path == "/log-urls":
            import re as _re
            results = []
            seen = set()
            for lf in (LOG_FILE, LOG_DIR / ".proxy.log", gallery_dir() / "proxy.log"):
                try:
                    if not lf.is_file():
                        continue
                    for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
                        # 时间戳(行首 yyyy-mm-dd HH:MM:SS)
                        tsm = _re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                        ts = tsm.group(1) if tsm else ""
                        # 抓 URL= 或 [RAW] 行里、或任意位置的图片直链
                        for m in _re.finditer(r"https?://[^\s\"'\\]+\.(?:png|jpg|jpeg|webp)", line, _re.I):
                            u = m.group(0)
                            if u in seen:
                                continue
                            seen.add(u)
                            results.append({"url": u, "time": ts})
                except Exception:
                    continue
            self._send_json(200, {"urls": results})
            return

        # ─── /gallery/list  云端共享画廊 ───
        if path == "/gallery/list":
            self._send_json(200, {"records": gj_list()})
            return

        # ─── /log/tail  最近N行日志 ───
        if path.startswith("/log/tail"):
            try:
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(self.path).query)
                n = int(qs.get("n", ["100"])[0])
            except Exception:
                n = 100
            lines = []
            try:
                if LOG_FILE.exists():
                    lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
            except Exception:
                pass
            self._send_json(200, {"lines": lines})
            return

        # ─── /get-provider  返回当前 provider ───
        if path == "/get-provider":
            self._send_json(200, {"provider": PROVIDER, "valid": list(_VALID_PROVIDERS)})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        # ─── /set-gallery-dir  前端设置输出目录 ───
        if path == "/set-gallery-dir":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                new_dir = (body.get("dir") or "").strip()
                if not new_dir:
                    self._send_json(400, {"error": "目录为空"})
                    return
                resolved = set_gallery_dir(new_dir)
                log(f"  📁 输出目录已切换 → {resolved}")
                self._send_json(200, {"ok": True, "dir": resolved})
            except Exception as e:
                log(f"  ✗ 设置输出目录失败: {e}")
                self._send_json(500, {"error": f"无法使用该目录: {e}"})
            return

        # ─── /pick-folder  弹系统原生「选择文件夹」窗口，选完直接设为保存目录 ───
        # 弹不出（没 tkinter / 无图形界面 / 线程限制）→ 返回 available:false，前端退回手填
        if path == "/pick-folder":
            try:
                chosen = pick_folder_dialog(initial=gallery_dir())
            except Exception as e:
                log(f"  ⚠ 选择窗口弹不出，退回手填: {e}")
                self._send_json(200, {"available": False, "reason": str(e)})
                return
            if not chosen:
                # 用户点了取消
                self._send_json(200, {"available": True, "cancelled": True})
                return
            try:
                resolved = set_gallery_dir(chosen)
                log(f"  📁 输出目录已切换（窗口选择）→ {resolved}")
                self._send_json(200, {"available": True, "ok": True, "dir": resolved})
            except Exception as e:
                self._send_json(500, {"error": f"无法使用该目录: {e}"})
            return

        # ─── /describe-image  调 GLM-4.6V 看图,生成中文描述 ───
        if path == "/describe-image":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                img_url = body.get("url", "")
                log(f"  📥 GLM 看图请求 | 图片URL: {img_url}")
                description = self._call_glm_vision(img_url)
                self._send_json(200, {"description": description})
                # 完整记录 GLM 返回的视觉描述全文（便于追溯下游 DS 的输入）
                log(f"  📤 GLM 描述全文({len(description)}字):\n{description}")
            except Exception as e:
                log(f"  ✗ GLM 调用失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /generate-prompt  调 DeepSeek 生成英文 prompt ───
        if path == "/generate-prompt":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                desc_in = body.get("description", "")
                intent_in = body.get("user_intent", "")
                # 完整记录喂给 DS 的两个输入（这是排查"DS 打架/不听话"的关键）
                log(f"  📥 DS 生成prompt请求\n     [视觉描述输入 {len(desc_in)}字]: {desc_in}\n     [用户意图输入]: {intent_in}")
                prompt = self._call_deepseek_prompt(desc_in, intent_in)
                self._send_json(200, {"prompt": prompt})
                # 完整记录 DS 吐出的最终 prompt 全文
                log(f"  📤 DS 生成prompt全文({len(prompt)}字):\n{prompt}")
            except Exception as e:
                log(f"  ✗ DS 调用失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /link-map  前端汇总全库记录，写【文件名|本地链|图床链】对照表 ───
        if path == "/link-map":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                records = body.get("records") or []
                reason = body.get("reason") or "手动"
                mode = body.get("mode") or "snapshot"  # snapshot=覆盖全表 / append=追加单条
                if mode == "append" and records:
                    append_link_map(records[0], reason)
                    self._send_json(200, {"ok": True, "mode": "append"})
                else:
                    stat = write_link_map(records, reason)
                    self._send_json(200, {"ok": True, "mode": "snapshot", **stat})
            except Exception as e:
                log(f"  ✗ /link-map 失败: {e}", "warning")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /client-log  前端关键操作落盘到 proxy.log（换绑/再用/解析等）───
        if path == "/client-log":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                tag = (body.get("tag") or "client").upper()
                msg = body.get("msg") or ""
                lvl = body.get("level") or "info"
                log(f"  [前端·{tag}] {msg}", lvl if lvl in ("info", "warning", "error") else "info")
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /gallery/save  新增或更新画廊记录 ───
        if path == "/gallery/save":
            length = int(self.headers.get("Content-Length", 0))
            try:
                record = json.loads(self.rfile.read(length).decode("utf-8"))
                gj_save(record)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /gallery/delete  删除画廊记录 ───
        if path == "/gallery/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                gj_delete(body.get("id"))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /gallery/update  局部更新（如标签）───
        if path == "/gallery/update":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                gj_update(body.get("id"), body)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ─── /set-provider  切换生图 provider ───
        if path == "/set-provider":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                name = (body.get("provider") or "").strip().lower()
                set_provider(name)
                self._send_json(200, {"ok": True, "provider": PROVIDER})
            except Exception as e:
                log(f"  ✗ 切换 provider 失败: {e}")
                self._send_json(400, {"error": str(e)})
            return

        # ─── /generate-prompt-stream  流式生成（思考+正文逐块 SSE 推送）───
        if path == "/generate-prompt-stream":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"请求体解析失败: {e}"})
                return
            # 这里不走 _send_json，由 _stream_deepseek_prompt 自己写 SSE 头和数据
            self._stream_deepseek_prompt(body)
            return

        # ─── /upload-image  转发 heliar 上传 ───
        if path == "/upload-image":
            length = int(self.headers.get("Content-Length", 0))
            content_type = self.headers.get("Content-Type", "")
            raw_body = self.rfile.read(length)
            try:
                url = self._upload_to_heliar(raw_body, content_type)
                self._send_json(200, {"url": url})
                log(f"  ✓ 已上传到 heliar: {url}")
            except Exception as e:
                log(f"  ✗ heliar 上传失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /save-image  自动保存图片到本地 ~/Documents/速创画廊/ ───
        if path == "/save-image":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                result = self._save_image_locally(body)
                self._send_json(200, result)
            except Exception as e:
                log(f"  ✗ 保存图片失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /reupload-heliar  把某张本地图重新上传 Heliar，拿永久链接 ───
        # 用于：临时链接失效后，「换绑本地图」时顺便把本地文件重传图床
        if path == "/reupload-heliar":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                fname = (body.get("file") or "").strip()
                is_ref = bool(body.get("ref"))
                if (not fname) or ("/" in fname) or ("\\" in fname) or fname.startswith("."):
                    self._send_json(400, {"error": "非法文件名"})
                    return
                base = (gallery_dir() / ("refs" if is_ref else "")).resolve()
                target = (base / fname).resolve()
                target.relative_to(base)  # 防越界，越界会抛 ValueError
                if not target.is_file():
                    self._send_json(404, {"error": "本地文件不存在，无法重传"})
                    return
                new_url = self._upload_path_to_heliar(target)
                log(f"  ☁️ 重传图床成功 | {fname} → {new_url}")
                # 同步更新 index.json 里这条的 heliar_url
                try:
                    idx_path = gallery_dir() / "index.json"
                    if idx_path.is_file():
                        arr = json.loads(idx_path.read_text(encoding="utf-8"))
                        for it in arr:
                            if it.get("file") == fname:
                                it["heliar_url"] = new_url
                        idx_path.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    log(f"  ⚠ 重传后更新 index.json 失败: {e}")
                self._send_json(200, {"ok": True, "heliar_url": new_url})
            except ValueError:
                self._send_json(403, {"error": "越界访问被拒绝"})
            except Exception as e:
                log(f"  ✗ 重传图床失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /save-ref  参考图落盘到 ~/Documents/速创画廊/refs/ ───
        if path == "/save-ref":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                result = self._save_ref_locally(body)
                self._send_json(200, result)
            except Exception as e:
                log(f"  ✗ 保存参考图失败: {e}")
                self._send_json(500, {"error": str(e)})
            return

        # ─── /list-refs  读 refs_index.json,返回磁盘上所有参考图 ───
        if path == "/list-refs":
            try:
                self._send_json(200, {"refs": self._list_refs()})
            except Exception as e:
                log(f"  ✗ 读取参考库失败: {e}")
                self._send_json(500, {"error": str(e), "refs": []})
            return

        if path == "/generate-async":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"请求体不是 JSON: {e}"})
                return
            task_cleanup()
            prompt_preview = body.get("prompt") or ""
            if isinstance(prompt_preview, str):
                prompt_preview = prompt_preview.replace("\n", " ").strip()[:60]
            else:
                prompt_preview = ""
            n_refs = len(body.get("urls") or [])
            log(f"  🎨 开始生成(async) | 尺寸 {body.get('size','?')} | 参考图 {n_refs} 张 | {prompt_preview}")
            try:
                task_id, _, used_key = self._submit_task(body)
                log(f"  ✓ 已提交 task_id={task_id}")
            except Exception as e:
                log(f"  ✗ 提交失败: {e}")
                self._send_json(500, {"error": f"提交失败: {e}"})
                return
            task_set(task_id, status=0, done=False)

            def _bg(handler, tid, key):
                def emit(event_type, payload):
                    if event_type == "poll":
                        task_set(tid, status=payload.get("status"),
                                 progress=payload.get("progress"),
                                 poll_count=payload.get("poll_count"),
                                 elapsed=payload.get("elapsed"))
                    elif event_type == "status":
                        task_set(tid, phase=payload.get("phase"))
                    elif event_type == "done":
                        task_set(tid, done=True, result=payload)
                    elif event_type == "error":
                        task_set(tid, done=True, error=payload.get("msg") or "失败")
                try:
                    handler._stream_poll(tid, emit, poll_key=key)
                except Exception as e:
                    task_set(tid, done=True, error=str(e))

            threading.Thread(target=_bg, args=(self, task_id, used_key), daemon=True).start()
            self._send_json(200, {"task_id": task_id})
            return

        if path != "/generate":
            self._send_json(404, {"error": "未知路径"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": f"请求体不是 JSON: {e}"})
            return

        # === SSE 流式响应 ===
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # 防止某些代理缓冲
        self._send_cors_headers()
        self.end_headers()

        def emit(event_type, payload):
            """发送一个 SSE 事件给浏览器"""
            line = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        # 1. 提交
        emit("status", {"phase": "submitting", "msg": "提交任务到速创..."})
        prompt_preview = body.get("prompt") or ""
        if isinstance(prompt_preview, str):
            prompt_preview = prompt_preview.replace("\n", " ").strip()[:60]
        else:
            prompt_preview = ""
        n_refs = len(body.get("urls") or [])
        # 实验参数（官方文档没列，赌透传）——记进日志方便判断是否被接受
        _exp = []
        for k in ("moderation", "quality", "n"):
            if k in body:
                _exp.append(f"{k}={body[k]}")
        _exp_str = (" | 🧪 " + " ".join(_exp)) if _exp else ""
        log(f"  🎨 开始生成 | 尺寸 {body.get('size','?')} | 参考图 {n_refs} 张{_exp_str} | {prompt_preview}")
        try:
            task_id, _, used_key = self._submit_task(body)
            log(f"  ✓ 已提交 task_id={task_id}")
            emit("status", {"phase": "submitted", "msg": "已提交,开始轮询", "task_id": task_id})
        except Exception as e:
            log(f"  ✗ 提交失败: {e}")
            emit("error", {"msg": f"提交失败: {e}"})
            return

        # 2. 流式轮询（用提交时同一个 key 查任务）
        try:
            self._stream_poll(task_id, emit, poll_key=used_key)
        except Exception as e:
            log(f"  ✗ 轮询失败: {e}")
            emit("error", {"msg": str(e), "task_id": task_id})

    def _save_image_locally(self, body):
        """下载图片并保存到 ~/Documents/速创画廊/"""
        import re
        from pathlib import Path

        image_url = body.get("url", "")
        prompt = body.get("prompt", "untitled")
        timestamp = body.get("time", int(time.time() * 1000))

        if not image_url:
            raise RuntimeError("没有图片 URL")

        save_dir = gallery_dir()
        save_dir.mkdir(parents=True, exist_ok=True)

        dt = time.strftime("%Y%m%d_%H%M%S", time.localtime(timestamp / 1000))
        safe_prompt = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', prompt[:30]).strip('_')[:30]
        filename = f"{dt}_{safe_prompt}.png"

        filepath = save_dir / filename
        counter = 1
        while filepath.exists():
            filepath = save_dir / f"{dt}_{safe_prompt}_{counter}.png"
            counter += 1

        ctx = SSL_CTX
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        _t0 = time.time()
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            data = resp.read()
        _dl = time.time() - _t0

        filepath.write_bytes(data)
        log(f"  💾 已保存本地 | {filename} | {len(data)//1024}KB | 下载耗时 {_dl:.1f}s")

        # 存完本地，自动传一份到 Heliar（永久图床），拿到永久链接。
        # 失败不影响本地保存——本地文件已落盘，永久链接以后可用「重传图床」补。
        heliar_url = ""
        try:
            _h0 = time.time()
            heliar_url = self._upload_path_to_heliar(filepath)
            log(f"  ☁️ 已传图床 | {filename} | {heliar_url} | 耗时 {time.time()-_h0:.1f}s")
        except Exception as e:
            log(f"  ⚠ 图床上传失败（本地已存，可稍后重传）: {e}")

        # 追加元数据到 index.json
        meta_path = save_dir / "index.json"
        meta_list = []
        if meta_path.exists():
            try:
                meta_list = json.loads(meta_path.read_text(encoding="utf-8"))
            except:
                meta_list = []
        meta_list.append({
            "file": filename, "url": image_url, "heliar_url": heliar_url, "prompt": prompt,
            "time": timestamp, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        meta_path.write_text(json.dumps(meta_list, ensure_ascii=False, indent=2), encoding="utf-8")

        return {"saved": str(filepath), "size": len(data), "heliar_url": heliar_url, "file": filename}

    def _refs_dir(self):
        d = gallery_dir() / "refs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _refs_index_path(self):
        return self._refs_dir() / "refs_index.json"

    def _read_refs_index(self):
        p = self._refs_index_path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_ref_locally(self, body):
        """下载参考图存到 ~/Documents/速创画廊/refs/, 并记 refs_index.json (按 url 去重)"""
        import re
        url = (body.get("url") or "").strip()
        source = body.get("source") or "upload"
        if not url:
            raise RuntimeError("没有参考图 URL")

        refs_dir = self._refs_dir()
        index = self._read_refs_index()

        # 已记录过同一 url 且文件还在 -> 跳过, 保证幂等
        for item in index:
            if item.get("url") == url:
                fp = refs_dir / item.get("file", "")
                if item.get("file") and fp.exists():
                    return {"saved": str(fp), "skipped": True}

        ext = ".png"
        m = re.search(r"\.(png|jpe?g|webp|gif)(?:\?|$)", url, re.I)
        if m:
            ext = "." + m.group(1).lower().replace("jpeg", "jpg")

        dt = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        filename = f"ref_{dt}{ext}"
        filepath = refs_dir / filename
        counter = 1
        while filepath.exists():
            filepath = refs_dir / f"ref_{dt}_{counter}{ext}"
            counter += 1

        ctx = SSL_CTX
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = resp.read()
        filepath.write_bytes(data)
        log(f"  🖼️ 参考图已存盘: {filepath.name} ({len(data)//1024}KB) [{source}]")

        # 再次去重后追加 (并发兜底)
        index = self._read_refs_index()
        if not any(it.get("url") == url for it in index):
            index.append({
                "url": url, "file": filepath.name, "source": source,
                "time": int(time.time() * 1000),
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._refs_index_path().write_text(
                json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        return {"saved": str(filepath), "size": len(data)}

    def _list_refs(self):
        """返回磁盘上所有参考图记录, 按时间倒序; 自动剔除文件已不存在的条目"""
        refs_dir = self._refs_dir()
        index = self._read_refs_index()
        alive = [it for it in index if it.get("file") and (refs_dir / it["file"]).exists()]
        alive.sort(key=lambda x: x.get("time", 0), reverse=True)
        return alive

    def _call_glm_vision(self, image_url):
        """用 GLM 看图,返回中文视觉描述. 先试 4.6v, 429 时 fallback 到 flash"""
        if not image_url:
            raise RuntimeError("没传图片 URL")

        # 按优先级 fallback: 4.6v-flash → 4.1v-thinking-flash → 4v-flash (老的免费版,基本必通)
        models_to_try = [
            "glm-4.6v-flash",
            "glm-4.1v-thinking-flash",
            "glm-4v-flash",
            "glm-4.6v",  # 付费版最后试
        ]
        last_err = None
        for model_name in models_to_try:
            try:
                result = self._glm_call(image_url, model_name)
                log(f"  ✓ GLM 用 {model_name} 成功")
                return result
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                err = f"HTTP {e.code}: {body}"
                log(f"  ⚠ GLM {model_name} 失败: {err}")
                last_err = err
                if e.code == 429:
                    time.sleep(2)
                    continue
                if e.code in (401, 403):
                    raise RuntimeError(f"GLM 鉴权失败 ({model_name}): {err}. 请去 bigmodel.cn 确认 API key 有效且账号已实名")
                # 其它错误也试下一个模型
                continue

        raise RuntimeError(f"GLM 所有模型都失败. 最后错误: {last_err}")

    def _glm_call(self, image_url, model_name):
        """实际调 GLM API"""
        payload = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "请用中文详细描述这张图片的视觉元素，作为后续 AI 生图的参考材料。\n"
                        "按重要性顺序重点描述：\n"
                        "1. 构图与视觉动线（整体构图类型如对角线/居中/三分；景别与视角/机位；画面的视觉动线怎么走；主体在画面什么位置；若有多个主体，它们之间靠什么连接、谁是视觉焦点/支点）——这一项最重要，请放在最前面详细写\n"
                        "2. 人物姿态（身体朝向、弯曲、四肢方向、脸朝哪、表情、动作的力学感）\n"
                        "3. 人物特征（发型、发色、眼睛、肤色）\n"
                        "4. 服装与配饰（材质、颜色、款式细节）\n"
                        "5. 场景与背景（环境、光线类型与方向、氛围）\n"
                        "6. 色调与画风（整体色彩、艺术风格）\n\n"
                        "不要主观评价，只描述你看到的视觉事实。"
                        "不要用列表格式,用连贯的描述性段落，但要把构图和动线写在段落开头。"
                    )},
                ],
            }],
        }

        req = urllib.request.Request(
            GLM_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GLM_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # GLM 标准响应: choices[0].message.content
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"GLM 响应异常: {data}")
        msg = choices[0].get("message", {})
        content = msg.get("content")
        # 有时 content 是 string, 有时是 list
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return (content or "").strip()

    def _ds_system_prompt(self):
        """DeepSeek 生成 prompt 用的 system prompt（流式/非流式共用）"""
        return (
            "你是一个 AI 生图提示词工程师,专门为 GPT-Image-2 模型生成高质量英文 prompt。\n\n"
            "【最重要的原则——构图骨架前置】\n"
            "GPT-Image-2 顺序处理语言,prompt 前 30% 的 token 决定构图,后 70% 决定细节。\n"
            "所以必须按这个顺序分层组织,绝不能把构图埋在中间或结尾:\n"
            "  第1层(开头第一句): 风格 + 构图骨架。先点明具体风格词(如 colored pencil illustration / anime cover art / photorealistic / cinematic movie-poster),紧接着用一句话锁死整体构图(如 extreme diagonal composition / centered symmetrical portrait / wide establishing shot),这一句权重最高。\n"
            "  第2层: 力学/空间关系。画面的视觉动线、主体之间怎么被一条线或一个支点连接、谁在画面什么位置(左上/右下/正中)。如果是有张力的动作场景,明确指出'视觉锚点'是什么,并可重复强调一次。\n"
            "  第3层: 主体姿态。身体朝向、弯曲、四肢方向、脸朝哪、表情。\n"
            "  第4层: 世界观/场景细节。背景、环境、服装大件。\n"
            "  第5层(结尾): 材质/光线/质量后缀。光要分两层写(类型+方向,如 side light / golden hour)。\n"
            "    【禁止否定约束】绝对不要在 prompt 里写任何否定词/否定约束(如 no humans, no text, no watermark, no extra limbs, no distortion 等)。GPT-Image-2 对否定词不稳定,经常反而把被排除的元素带回画面。要规避某元素,一律用正面描述代替(如想要干净单人就写 'a single clean figure',而不是 'no extra limbs')。\n\n"
            "【其它要点】\n"
            "1. 风格词必须放在最前面,不要埋在结尾(埋在结尾会削弱其影响)。\n"
            "2. 单一主导风格:一个 prompt 只保留一个主导风格词。绝不要同时写两个冲突风格(如 photorealistic + Pixar 3D),模型会随机只选一个;次要风格放进 'Style:' 标签或 'inspired by' 从句。\n"
            "3. 复刻某张参考图的构图时,用 'composition inspired by [对那张图构图的简述]' 从句很有效,能让模型往那个视觉方向靠;同时把力线方向、主体位置写准(参考图里主体在哪就写在哪,别凭印象写反)。\n"
            "   【保留引用逻辑】如果[用户意图]里明确说了'引用/参考/仿照某张图的构图'(例:'引用飞机救援海报的构图'),你必须:(a)把这个引用保留下来,在最终 prompt 里写成 'composition inspired by XX' 从句;(b)不要只写图名,要把那张图构图的核心力学特征拆出来写进从句(例:不是只写 'inspired by airplane poster',而是写 'composition inspired by vintage aviation rescue posters — a single grasped hand connecting two figures across a tension line, the gripped point at the exact center as the anchor, the rescued figure swung horizontally');(c)这条引用从句放在 prompt 开头构图层或结尾强调,不要省略、不要弱化。\n"
            "4. 多主体/复杂动作构图,务必把'谁在哪、被什么连接'写在前面,否则模型会先画好主角再勉强塞配角。\n"
            "5. 避免敏感词组合(性化少女、被观察的实验对象、过度暴露描述)——会被内容政策拦截;改用含蓄、艺术化的措辞,尺度交给参考图承载。\n"
            "6. 把 prompt 当'视觉设计 Brief',不是形容词堆砌。\n\n"
            "【多参考图】如果收到多张参考图(每张带各自的视觉描述和各自意图),要综合所有图:理解每张图各自负责提供什么(如图1负责构图、图2负责角色形象、图3负责画风),按各自意图把它们融进同一个 prompt;再叠加[总意图]做统领。不要只用第一张、不要平均混淆。\n\n"
            "你将收到 [视觉参考] 和 [用户意图](单图),或多段 [参考图N] + [总意图](多图)。\n"
            "你的任务: 生成一段英文 prompt,严格按上面的分层顺序组织。"
            "输出格式: **只输出最终英文 prompt 文本,不要任何前言、解释、标题、引号或 markdown**。"
        )

    def _ds_build_user_content(self, payload_in):
        """构造给 DS 的 user content。
        兼容两种入参:
        - 单图旧格式: {description, user_intent}
        - 多图新格式: {refs:[{description,intent},...], overall_intent}
        """
        refs = payload_in.get("refs")
        if isinstance(refs, list) and refs:
            parts = []
            for i, r in enumerate(refs, 1):
                d = (r.get("description") or "").strip()
                it = (r.get("intent") or "").strip()
                parts.append(f"[参考图{i}]\n视觉描述: {d}\n这张图的意图: {it or '(未单独说明)'}")
            overall = (payload_in.get("overall_intent") or "").strip()
            parts.append(f"[总意图]\n{overall or '综合以上参考图,生成一张统一的作品'}")
            return "\n\n".join(parts)
        # 单图旧格式
        description = (payload_in.get("description") or "").strip()
        user_intent = (payload_in.get("user_intent") or "").strip()
        return (
            f"[视觉参考]\n{description}\n\n"
            f"[用户意图]\n{user_intent or '保持参考图风格,生成同类作品'}"
        )

    def _call_deepseek_prompt(self, description, user_intent):
        """用 DeepSeek V4 Pro 根据视觉描述 + 用户意图,生成英文 GPT-Image-2 prompt（非流式，保留兼容）"""
        if not description and not user_intent:
            raise RuntimeError("需要至少描述或意图其中之一")

        system_prompt = self._ds_system_prompt()
        user_content = self._ds_build_user_content({"description": description, "user_intent": user_intent})

        payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "stream": False,
        }

        req = urllib.request.Request(
            DS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {DS_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"DS 响应异常: {data}")
        msg = choices[0].get("message", {})
        raw_content = msg.get("content") or ""
        # 记录 DS 原始返回全文（strip 前）——排查它有没有夹带思考过程/markdown/前言/引号
        log(f"  🔬 DS 原始返回({len(raw_content)}字):\n{raw_content}")
        return raw_content.strip()

    def _stream_deepseek_prompt(self, payload_in):
        """流式生成：逐块把 DS 的思考(reasoning)和正文(content)以 SSE 推给前端。
        前端按 type 分流：think → 🧠思考区，text → 最终prompt框。
        SSE 事件格式：data: {"type":"think"|"text"|"done"|"error","delta":"..."}"""
        system_prompt = self._ds_system_prompt()
        user_content = self._ds_build_user_content(payload_in)

        # 记录输入（追溯）
        log(f"  📥 DS流式 生成prompt请求:\n{user_content}")

        payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "stream": True,   # ← 流式
        }
        req = urllib.request.Request(
            DS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {DS_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        # 先给前端发 SSE 头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.end_headers()

        def push(obj):
            try:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        think_buf = []   # 累积思考全文，用于最后写 log
        text_buf = []    # 累积正文全文
        try:
            with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # 思考字段全兼容：reasoning_content / reasoning / thinking
                    think_piece = (delta.get("reasoning_content")
                                   or delta.get("reasoning")
                                   or delta.get("thinking") or "")
                    text_piece = delta.get("content") or ""
                    if think_piece:
                        think_buf.append(think_piece)
                        push({"type": "think", "delta": think_piece})
                    if text_piece:
                        text_buf.append(text_piece)
                        push({"type": "text", "delta": text_piece})
            full_think = "".join(think_buf)
            full_text = "".join(text_buf)
            # 思考和正文都写 log（可追溯）
            if full_think:
                log(f"  🧠 DS流式 思考全文({len(full_think)}字):\n{full_think}")
            log(f"  📤 DS流式 prompt正文全文({len(full_text)}字):\n{full_text}")
            push({"type": "done"})
        except Exception as e:
            log(f"  ✗ DS流式 失败: {e}")
            push({"type": "error", "delta": str(e)})

    def _upload_to_heliar(self, raw_body, content_type):
        """把原样 multipart 请求转发到 heliar (模拟一个浏览器)"""
        req = urllib.request.Request(
            HELIAR_UPLOAD,
            data=raw_body,
            headers={
                "Content-Type": content_type,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://img.heliar.top",
                "Referer": "https://img.heliar.top/",
                "Accept": "application/json, text/plain, */*",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        item = data[0] if isinstance(data, list) else data
        src = item.get("src") if isinstance(item, dict) else None
        if not src:
            raise RuntimeError(f"heliar 返回异常: {data}")
        return src if src.startswith("http") else HELIAR_BASE + src

    def _upload_path_to_heliar(self, filepath):
        """读本地文件，自己拼 multipart/form-data 传到 heliar，返回永久 url。
        和浏览器 FormData(file=...) 等价。失败抛异常。"""
        import uuid, mimetypes
        from pathlib import Path
        fp = Path(filepath)
        if not fp.is_file():
            raise RuntimeError(f"本地文件不存在: {filepath}")
        file_bytes = fp.read_bytes()
        ctype = mimetypes.guess_type(str(fp))[0] or "image/png"
        boundary = "----SuChuangBoundary" + uuid.uuid4().hex
        CRLF = "\r\n"
        head = (
            f"--{boundary}{CRLF}"
            f'Content-Disposition: form-data; name="file"; filename="{fp.name}"{CRLF}'
            f"Content-Type: {ctype}{CRLF}{CRLF}"
        ).encode("utf-8")
        tail = f"{CRLF}--{boundary}--{CRLF}".encode("utf-8")
        body = head + file_bytes + tail
        # 复用 _upload_to_heliar 的转发逻辑（同样的 header/解析）
        return self._upload_to_heliar(body, f"multipart/form-data; boundary={boundary}")

    def _submit_task(self, body):
        # 前端可能带 user_key（朋友填的自己的 key）；带了就优先用它，没带用预置 API_KEY。
        # user_key 是本工具自定义字段，必须从转发给上游的 body 里剔除。
        body = dict(body)  # 不改动调用方的原 body
        use_key = (body.pop("user_key", "") or "").strip() or API_KEY
        url = f"{BASE}{SUBMIT_PATH}?key={urllib.parse.quote(use_key)}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": use_key,  # 文档同时提到 Header Authorization, 双保险
            },
            method="POST",
        )
        # 提交可能撞限流/风控 (418 I'm a teapot / 429 Too Many Requests / 503)，
        # 退避重试最多 4 次，错峰后通常能过。
        data = None
        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (418, 429, 503, 502, 500):
                    wait = 2 * (attempt + 1)  # 2s,4s,6s,8s 递增退避
                    log(f"  ⚠ 提交被限流 (HTTP {e.code}, 尝试 {attempt+1}/4)，等 {wait}s 重试")
                    time.sleep(wait)
                    continue
                raise  # 其它 HTTP 错误直接抛
        if data is None:
            raise RuntimeError(f"提交多次被限流仍失败 (HTTP {getattr(last_err,'code','?')})，请降低并发或稍后再试")

        if data.get("code") != 200:
            raise RuntimeError(data.get("msg") or f"提交返回: {data}")
        task_id = data.get("data", {}).get("id")
        if not task_id:
            raise RuntimeError(f"响应无 task id: {data}")
        return task_id, data, use_key

    def _stream_poll(self, task_id, emit, poll_key=None):
        """流式轮询: 每次轮询都推一个 SSE 事件给前端,直到成功/失败/超时"""
        poll_key = (poll_key or API_KEY)
        start = time.time()
        poll_count = 0

        while time.time() - start < POLL_TIMEOUT:
            poll_count += 1
            elapsed = time.time() - start
            url = (
                f"{BASE}{DETAIL_PATH}"
                f"?key={urllib.parse.quote(poll_key)}"
                f"&id={urllib.parse.quote(task_id)}"
            )
            try:
                req = urllib.request.Request(url, method="GET")
                # SSL 握手偶发 hostname mismatch, 重试最多 3 次
                last_err = None
                resp_data = None
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
                            resp_data = json.loads(resp.read().decode("utf-8"))
                        break
                    except (ssl.SSLError, urllib.error.URLError) as e:
                        err_str = str(e)
                        if "Hostname mismatch" in err_str or "CERTIFICATE_VERIFY" in err_str:
                            last_err = e
                            log(f"  ⚠ SSL 错误 (尝试 {attempt+1}/3), 等 2s 重试: {err_str[:80]}")
                            time.sleep(2)
                            continue
                        raise
                if resp_data is None:
                    raise last_err or RuntimeError("SSL 重试 3 次仍失败")
                data = resp_data

                inner = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
                # 速创官方状态码: 0=初始化 1=进行中 2=成功 3=失败
                status_value = inner.get("status")
                if status_value is None:
                    status_value = inner.get("state") or data.get("status")
                progress = inner.get("progress") or inner.get("percent") or data.get("progress")
                log(f"  轮询#{poll_count} {elapsed:.1f}s status={status_value} progress={progress}", "debug")

                # 推送一次轮询心跳
                emit("poll", {
                    "elapsed": round(elapsed, 1),
                    "poll_count": poll_count,
                    "status": status_value,
                    "progress": progress,
                    "raw": inner if inner else data,
                })

                # 优先检查速创官方状态码 (0/1/2/3)
                if status_value == 2 or status_value == "2":
                    img_url = self._extract_image_url(inner) or self._extract_image_url(data) or ""
                    if img_url:
                        log(f"  ✅ 生成完成 | 耗时 {elapsed:.1f}s | 轮询 {poll_count} 次 | URL={img_url}")
                    else:
                        # 没提取到 URL：把原始返回完整写进日志，确保 URL 永不丢失(可人工/脚本找回)
                        log(f"  ✅ 生成完成 | 耗时 {elapsed:.1f}s | 轮询 {poll_count} 次 | ⚠未解析到URL,原始返回↓")
                        log(f"  [RAW] {json.dumps(inner if inner else data, ensure_ascii=False)}")
                    emit("done", data)
                    return
                if status_value == 3 or status_value == "3":
                    err_msg = inner.get("message") or inner.get("msg") or inner.get("error") or "任务失败"
                    raise RuntimeError(f"速创任务失败 (status=3): {err_msg}")

                # 兜底: 如果某些接口直接返回图片字段
                if self._has_image(inner) or self._has_image(data):
                    img_url = self._extract_image_url(inner) or self._extract_image_url(data) or ""
                    if img_url:
                        log(f"  ✅ 生成完成 | 耗时 {elapsed:.1f}s | 轮询 {poll_count} 次 | URL={img_url}")
                    else:
                        log(f"  ✅ 生成完成 | 耗时 {elapsed:.1f}s | 轮询 {poll_count} 次 | ⚠未解析到URL,原始返回↓")
                        log(f"  [RAW] {json.dumps(inner if inner else data, ensure_ascii=False)}")
                    emit("done", data)
                    return

                # 其他文本状态值的失败兜底
                if status_value and isinstance(status_value, str) and self._is_failure(status_value):
                    raise RuntimeError(
                        inner.get("message") or inner.get("msg") or inner.get("error")
                        or data.get("msg") or f"任务失败: {data}"
                    )

            except urllib.error.HTTPError as e:
                if e.code != 404:
                    body = ""
                    try:
                        body = e.read().decode("utf-8")
                    except Exception:
                        pass
                    raise RuntimeError(f"HTTP {e.code}: {body}")

            time.sleep(POLL_INTERVAL)

        raise RuntimeError(f"轮询超时 ({POLL_TIMEOUT}s)")

    @staticmethod
    def _extract_image_url(d):
        """从响应里抠出第一个图片直链, 用于日志记录(数据持久化兜底)"""
        if not isinstance(d, dict):
            return None
        # result 可能是字符串，也可能是数组(速创实际返回数组) — 都要支持
        for k in ("url", "image_url", "image", "result"):
            v = d.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.startswith("http"):
                        return x
        for k in ("urls", "images", "results"):
            v = d.get(k)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.startswith("http"):
                        return x
        # 深度兜底：递归找任意 http 直链(防止字段名意外)
        try:
            import re as _re
            blob = json.dumps(d, ensure_ascii=False)
            m = _re.search(r'https?://[^\s"\\]+\.(?:png|jpg|jpeg|webp)', blob, _re.I)
            if m:
                return m.group(0)
        except Exception:
            pass
        return None

    @staticmethod
    def _has_image(d):
        if not isinstance(d, dict):
            return False
        for k in ("url", "image_url", "image", "result"):
            v = d.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return True
        for k in ("urls", "images", "results"):
            v = d.get(k)
            if isinstance(v, list) and v and any(isinstance(x, str) and x.startswith("http") for x in v):
                return True
        return False

    @staticmethod
    def _is_failure(s):
        s = str(s).lower()
        return any(w in s for w in ("fail", "error", "失败", "错误"))

    def log_message(self, fmt, *args):
        log(f"  HTTP {fmt % args}", "debug")


def main():
    if not API_KEY:
        log("⚠️  没找到 API key")
        log()
        log("两种方式之一:")
        log()
        log("  1. 同目录创建 .env 文件 (推荐):")
        log("     复制 .env.example -> .env, 改里面的 SUKE_API_KEY")
        log()
        log("  2. 临时设环境变量:")
        log("     SUKE_API_KEY=你的key python3 proxy.py")
        sys.exit(1)

    masked = API_KEY[:4] + "•" * (len(API_KEY) - 8) + API_KEY[-4:]
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    log("━" * 50)
    log("  速创代理服务已启动")
    log(f"  本地: http://127.0.0.1:{PORT}")
    log(f"  公网: http://<你的IP>:{PORT}")
    log(f"  Key: {masked}")
    log(f"  📝 日志: {LOG_FILE}")
    log(f"  💾 图片: {LOG_DIR}")
    log("  Ctrl+C 退出")
    log("━" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n已停止")


if __name__ == "__main__":
    main()
