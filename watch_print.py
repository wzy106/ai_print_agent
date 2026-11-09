"""
监控打印接收目录：有新文件时先问本地 Ollama 是否需要打印，
需要则调用 PrintEngine 进行真实打印（或静默降级），否则忽略。

核心特性：
1. 生产级打印引擎接入（支持实体机静默出纸，无实体机降级存 PDF）。
2. SQLite WAL 模式并发安全落库，全链路日志追踪。
3. 多模态大模型（文本/视觉）意图提取，结合正则防呆校验。
4. 高级防抖、去重、指数退避重试与死信熔断机制。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 导入真实打印引擎
from print_engine import PrintEngine

# ==========================================
# 全局配置与常量
# ==========================================
# 获取当前模块的日志记录器
log = logging.getLogger("watch_print")

# 默认配置参数
DEFAULT_WATCH_DIR = Path(r"C:\打印接收")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
DEFAULT_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")
DEFAULT_DB_PATH = Path("print_logs.db")

# 忽略的系统临时文件后缀（如下载未完成的文件）
IGNORE_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part"}
# 忽略的隐藏文件或临时锁定文件前缀
IGNORE_PREFIXES = ("~$", ".")
# 允许读取文本预览的后缀
TEXT_PREVIEW_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log"}
# 支持的图片后缀
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tiff", ".tif"}

MAX_PREVIEW_CHARS = 800
MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_COLORS = {"color", "mono"}
ALLOWED_SIDES = {"single", "double"}
MAX_COPIES = 100
OLLAMA_RETRY_CAP = 60.0
MAX_FAIL_RETRIES = 10  # 最大重试次数，超过则熔断（死信）

# 日志 source 取值
SOURCE_FILENAME = "filename"
SOURCE_OLLAMA_TEXT = "ollama_text"
SOURCE_OLLAMA_VISION = "ollama_vision"


# ==========================================
# 数据模型定义 (Data Classes)
# ==========================================
@dataclass(frozen=True)
class Config:
    """【配置类】保存全局运行参数，实例化后不可更改（frozen=True）。"""
    watch_dir: Path
    ollama_host: str
    ollama_model: str
    ollama_vision_model: str
    ollama_timeout: float
    ollama_retries: int
    stable_seconds: float
    stable_timeout: float
    poll_interval: float
    workers: int
    include_existing: bool
    processed_ttl: float
    processed_max: int
    log_level: str
    db_path: Path


@dataclass(frozen=True)
class PrintIntent:
    """【意图类】保存解析出来的打印需求（是否打印、份数、颜色、单双面）。"""
    need_print: bool
    copies: int
    color: str
    sides: str
    inferred: bool = False


@dataclass(frozen=True)
class FileFingerprint:
    """【文件指纹类】用于去重，由文件大小和修改时间组成。"""
    size: int
    mtime_ns: int


# ==========================================
# 日志格式化扩展
# ==========================================
class _ShortLevelFormatter(logging.Formatter):
    """【日志格式化工具】把 WARNING 缩写成 WARN，让控制台日志视觉上对齐。"""
    _LEVEL_ALIASES = {"WARNING": "WARN"}

    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        record.levelname = self._LEVEL_ALIASES.get(original, original)
        try:
            return super().format(record)
        finally:
            record.levelname = original


def setup_logging(level: str) -> None:
    """【初始化日志系统】配置控制台输出格式和日志级别。"""
    handler = logging.StreamHandler()
    handler.setFormatter(
        _ShortLevelFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# ==========================================
# 文件指纹与去重追踪器
# ==========================================
def normalize_path(path: Path) -> str:
    """
    【路径规范化】
    将路径转为绝对路径并统一小写，用于底层去重 Key。
    注意：因为转了小写，此函数返回值绝不能用于UI展示。
    """
    raw = os.path.abspath(str(path))
    try:
        raw = str(Path(raw).resolve())
    except OSError:
        pass
    return os.path.normcase(raw)


def fingerprint_of(path: Path) -> FileFingerprint | None:
    """【获取文件指纹】读取文件大小和修改时间，读不到返回 None。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


class FileTracker:
    """
    【文件状态追踪器】
    负责管理文件的 pending（处理中）、processed（已处理）、retry_after（重试冷却）状态。
    核心职责：防抖、防重入、指数退避重试、死信熔断。
    """
    def __init__(self, ttl: float, max_entries: int) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._pending: dict[str, float] = {}
        self._processed: dict[str, tuple[FileFingerprint, float]] = {}
        self._retry_after: dict[str, float] = {}
        self._fail_counts: dict[str, int] = {}

    def seed_processed(self, key: str, fingerprint: FileFingerprint) -> None:
        """【初始化标记】启动时调用，将已存在的文件标记为已处理。"""
        with self._lock:
            self._processed[key] = (fingerprint, time.time())

    def try_claim(self, key: str, fingerprint: FileFingerprint) -> bool:
        """【抢占处理权】判断文件是否允许处理（不在处理中、不在冷却期、指纹有变化）。"""
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            if key in self._pending: return False
            if now < self._retry_after.get(key, 0): return False
            previous = self._processed.get(key)
            if previous is not None and previous[0] == fingerprint: return False
            self._pending[key] = now
            return True

    def complete(self, key: str, fingerprint: FileFingerprint) -> None:
        """【标记完成】处理成功后调用，记录指纹，清理失败计数。"""
        with self._lock:
            self._pending.pop(key, None)
            self._retry_after.pop(key, None)
            self._fail_counts.pop(key, None)
            self._processed[key] = (fingerprint, time.time())
            self._evict_locked()

    def release_for_retry(self, key: str) -> float:
        """
        【释放处理权并计算退避时间】
        返回需要等待的秒数。如果超过最大重试次数，返回 -1.0（触发死信熔断）。
        """
        with self._lock:
            self._pending.pop(key, None)
            fails = self._fail_counts.get(key, 0) + 1
            self._fail_counts[key] = fails
            
            # 熔断机制：防止坏文件引发无限重试死循环
            if fails >= MAX_FAIL_RETRIES:
                log.warning("文件连续失败 %s 次，触发熔断，已加入死信队列：%s", MAX_FAIL_RETRIES, key)
                fp = fingerprint_of(Path(key))
                if fp is not None:
                    self._processed[key] = (fp, time.time())
                self._retry_after.pop(key, None)
                return -1.0 
                
            # 指数退避：2s, 4s, 8s ... 最大 60s
            delay = min(2.0 * (2 ** (fails - 1)), OLLAMA_RETRY_CAP)
            self._retry_after[key] = time.time() + delay
            return delay

    def release(self, key: str) -> None:
        """【释放处理权】仅释放，不增加失败计数，不设置冷却。"""
        with self._lock:
            self._pending.pop(key, None)

    def _prune_locked(self, now: float) -> None:
        """【内部清理】清理过期的已处理记录和到期的重试冷却记录。"""
        expired = [k for k, (_, done_at) in self._processed.items() if now - done_at >= self._ttl or not os.path.exists(k)]
        for key in expired: self._processed.pop(key, None)
        stale_retry = [k for k, until in self._retry_after.items() if now >= until]
        for key in stale_retry:
            if key not in self._pending: self._retry_after.pop(key, None)

    def _evict_locked(self) -> None:
        """【LRU 淘汰】如果已处理记录超过上限，踢出最老的，防止内存泄漏。"""
        overflow = len(self._processed) - self._max_entries
        if overflow <= 0: return
        oldest = sorted(self._processed.items(), key=lambda item: item[1][1])
        for key, _ in oldest[:overflow]: self._processed.pop(key, None)


# ==========================================
# SQLite 数据库操作
# ==========================================
class PrintLogDB:
    """
    【SQLite 日志库】
    使用 WAL 模式支持多线程并发读写。所有写操作由 threading.Lock 串行化保证安全。
    """
    def __init__(self, db_path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS print_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL, file_path TEXT NOT NULL,
                    file_size INTEGER, suffix TEXT, need_print INTEGER NOT NULL, copies INTEGER,
                    color TEXT, sides TEXT, inferred INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_print_logs_created ON print_logs(created_at)")
            self._conn.commit()

    def log(self, path: Path, *, intent: PrintIntent | None, source: str, status: str, error: str | None = None) -> None:
        """【写入日志】将一次处理结果写入 SQLite 数据库。"""
        try: size = path.stat().st_size
        except OSError: size = None
        
        row = (
            path.name, str(path), size, path.suffix.lower(),
            1 if intent and intent.need_print else 0,
            intent.copies if intent else None, intent.color if intent else None, intent.sides if intent else None,
            1 if intent and intent.inferred else 0, source, status, error,
            datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            try:
                self._conn.execute("""
                    INSERT INTO print_logs
                    (file_name, file_path, file_size, suffix, need_print, copies, color, sides, inferred, source, status, error, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                self._conn.commit()
            except sqlite3.Error:
                log.exception("写日志失败：%s", path.name)

    def close(self) -> None:
        """【关闭连接】安全关闭数据库。"""
        with self._lock:
            try: self._conn.close()
            except sqlite3.Error: pass


# ==========================================
# 意图解析（正则 + 大模型）
# ==========================================
def _read_text_preview(path: Path) -> str:
    """【读取文本预览】仅读取文件前 800 个字符，防止大文件撑爆内存。"""
    if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES: return ""
    try:
        with path.open("rb") as f:
            data = f.read(MAX_PREVIEW_CHARS * 4) 
        return data.decode("utf-8", errors="ignore").strip()[:MAX_PREVIEW_CHARS]
    except OSError:
        return ""


def extract_json_object(text: str) -> dict[str, Any] | None:
    """【JSON 提取器】从大模型杂乱的输出中提取并解析标准 JSON 对象。"""
    raw = text.strip()
    if not raw: return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced: raw = fenced.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start: return None
        raw = raw[start : end + 1]
    try: data = json.loads(raw)
    except json.JSONDecodeError: return None
    return data if isinstance(data, dict) else None


def _coerce_bool(value: Any) -> bool:
    """【类型强转】将各种字符串/数字强转为 bool，失败则抛异常。"""
    if isinstance(value, bool): return value
    if isinstance(value, int) and value in (0, 1): return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}: return True
        if normalized in {"false", "0", "no"}: return False
    raise ValueError(f"非法 need_print：{value!r}")


def _coerce_copies(value: Any) -> int:
    """【类型强转】将份数强转为 1-100 的整数，超范围抛异常。"""
    if isinstance(value, bool) or isinstance(value, float): raise ValueError(f"非法 copies：{value!r}")
    if isinstance(value, int): copies = value
    elif isinstance(value, str) and value.strip().isdigit(): copies = int(value.strip())
    else: raise ValueError(f"非法 copies：{value!r}")
    if copies < 1 or copies > MAX_COPIES: raise ValueError(f"copies 超出范围：{copies}")
    return copies


def _coerce_choice(value: Any, allowed: set[str], field: str) -> str:
    """【枚举校验】确保字段值在允许的枚举集合内（如 color 必须是 color/mono）。"""
    if not isinstance(value, str): raise ValueError(f"非法 {field}：{value!r}")
    normalized = value.strip().lower()
    if normalized not in allowed: raise ValueError(f"非法 {field}：{value!r}")
    return normalized


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_COPIES_ARABIC_RE = re.compile(r"(\d{1,3})\s*(?:份|copies?)", re.IGNORECASE)
_COPIES_CN_RE = re.compile(r"([一二两三四五六七八九十百零]+)\s*份")
_EXPLICIT_FILENAME_RE = re.compile(
    r"(?:\d{1,3}\s*(?:份|copies?))|(?:[一二两三四五六七八九十百零]+\s*份)|"
    r"(?:彩印|彩色|彩打|黑白|单色|不打印|勿打印|无需打印)|(?:双面|单面)|"
    r"(?:\b(?:color|mono|double|single|bw)\b)", re.IGNORECASE,
)


def _parse_cn_number(text: str) -> int | None:
    """【中文数字解析】把“十五”、“二十”等中文数字转为阿拉伯数字。"""
    if text == "十": return 10
    if text == "百": return 100
    if text.startswith("十"):
        ones = _CN_DIGITS.get(text[1], 0) if len(text) > 1 else 0
        return 10 + ones
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if text in _CN_DIGITS: return _CN_DIGITS[text]
    return None


def parse_filename_intent(filename: str) -> PrintIntent | None:
    """【正则解析】第一优先级：从文件名直接提取意图（如“合同_2份_彩印.pdf”）。"""
    stem = Path(filename).stem
    if not _EXPLICIT_FILENAME_RE.search(stem): return None

    copies = 1
    arabic = _COPIES_ARABIC_RE.search(stem)
    if arabic: copies = int(arabic.group(1))
    else:
        chinese = _COPIES_CN_RE.search(stem)
        if chinese:
            parsed = _parse_cn_number(chinese.group(1))
            if parsed is not None: copies = parsed
    copies = max(1, min(copies, MAX_COPIES))

    color = "color" if re.search(r"彩印|彩色|彩打|\bcolor\b", stem, re.IGNORECASE) else "mono"
    sides = "double" if re.search(r"双面|\bdouble\b", stem, re.IGNORECASE) else "single"
    need_print = not re.search(r"不打印|勿打印|无需打印", stem)
    return PrintIntent(need_print=need_print, copies=copies, color=color, sides=sides, inferred=False)


def validate_intent(data: dict[str, Any]) -> PrintIntent:
    """【数据校验】校验大模型返回的字典，转换为严格的 PrintIntent 对象。"""
    missing = [key for key in ("need_print", "copies", "color", "sides") if key not in data]
    if missing: raise ValueError(f"缺少字段：{', '.join(missing)}")
    return PrintIntent(
        need_print=_coerce_bool(data["need_print"]), copies=_coerce_copies(data["copies"]),
        color=_coerce_choice(data["color"], ALLOWED_COLORS, "color"),
        sides=_coerce_choice(data["sides"], ALLOWED_SIDES, "sides"),
    )


# ==========================================
# Ollama API 交互
# ==========================================
def _ollama_request(host: str, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    """【HTTP 请求封装】向本地 Ollama 服务发送 REST 请求。"""
    url = f"{host.rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Ollama: {exc.reason}") from exc
    try: return json.loads(body) if body else {}
    except json.JSONDecodeError as exc: raise RuntimeError(f"返回非 JSON：{body[:200]}") from exc


def fetch_ollama_models(host: str, timeout: float = 5.0) -> list[str] | None:
    """【探测模型】获取本地 Ollama 已安装的模型列表。"""
    try: payload = _ollama_request(host, "GET", "/api/tags", None, timeout)
    except RuntimeError as exc:
        log.warning("无法连接 Ollama：%s", exc); return None
    names: list[str] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict): continue
        for field in ("name", "model"):
            val = item.get(field)
            if isinstance(val, str) and val: names.append(val)
    return names


def model_installed(model: str, names: list[str]) -> bool:
    """【模型校验】检查指定模型是否在已安装列表中。"""
    target = model.lower()
    return any(name.lower() == target or name.lower().startswith(f"{target}-") for name in names)


def build_intent_prompt(path: Path) -> str:
    """【提示词构建】拼装发送给大模型的 Prompt，包含规则、文件信息和预览。"""
    try: size = path.stat().st_size
    except OSError: size = 0
    
    prompt = (
        "你是打印店的意图识别助手。请根据文件名、扩展名和文件内容提取打印需求。\n"
        "只输出一个 JSON 对象，不要输出解释或 Markdown。\n"
        "JSON 格式：\n"
        '{"need_print": true, "copies": 2, "color": "color", "sides": "double"}\n'
        "字段说明：\n"
        "- need_print: 文档/图片等为 true；压缩包/可执行文件为 false。\n"
        "- copies: 1 到 100 的整数，未说明默认 1。\n"
        "- color: \"color\" 或 \"mono\"。\n"
        "- sides: \"single\" 或 \"double\"。\n"
        "猜测规则：文字稿默认 mono/single；海报/照片默认 color/single；无法判断默认 1/mono/single。\n"
        f"文件名：{path.name}\n扩展名：{path.suffix or '(无)'}\n大小：{size} 字节\n"
    )
    preview = _read_text_preview(path)
    if preview: prompt += f"文本预览：\n{preview}\n"
        
    if path.suffix.lower() in IMAGE_SUFFIXES:
        prompt += (
            "这是一张图片。请先仔细阅读图片中是否包含文字指令。\n"
            "【最高优先级】：如果图片文字中明确写了打印要求（如“彩印”、“双面”），必须无条件照做。\n"
            "如果无文字要求，再按视觉内容猜测。\n"
        )
    return prompt


def _call_ollama_once(path: Path, config: Config) -> tuple[PrintIntent, str]:
    """【单次模型调用】根据文件类型自动选择文本或视觉模型，发起请求。"""
    is_image = path.suffix.lower() in IMAGE_SUFFIXES
    used_vision = False
    payload = {
        "model": config.ollama_model, "prompt": build_intent_prompt(path),
        "stream": False, "format": "json", "options": {"temperature": 0},
    }

    if is_image:
        try: raw = path.read_bytes()
        except OSError as exc: raise RuntimeError(f"读取图片失败：{path.name}") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            log.warning("图片过大降级文本模型：%s", path.name)
        else:
            payload["images"] = [base64.b64encode(raw).decode("ascii")]
            payload["model"] = config.ollama_vision_model
            used_vision = True

    body = _ollama_request(config.ollama_host, "POST", "/api/generate", payload, config.ollama_timeout)
    answer = str(body.get("response", "")).strip()
    data = extract_json_object(answer)
    if data is None: raise ValueError(f"未能解析 JSON：{answer[:200]}")
    return validate_intent(data), SOURCE_OLLAMA_VISION if used_vision else SOURCE_OLLAMA_TEXT


def analyze_print_intent(path: Path, config: Config) -> tuple[PrintIntent | None, str]:
    """【意图解析路由】优先正则，失败则带重试调用大模型兜底。"""
    from_name = parse_filename_intent(path.name)
    if from_name is not None:
        log.info("从文件名解析 copies=%s color=%s sides=%s", from_name.copies, from_name.color, from_name.sides)
        return from_name, SOURCE_FILENAME

    is_image = path.suffix.lower() in IMAGE_SUFFIXES
    fallback_source = SOURCE_OLLAMA_VISION if is_image else SOURCE_OLLAMA_TEXT
    last_error: Exception | None = None
    
    for attempt in range(1, max(1, config.ollama_retries) + 1):
        try:
            intent, source = _call_ollama_once(path, config)
            guessed = PrintIntent(need_print=intent.need_print, copies=intent.copies, color=intent.color, sides=intent.sides, inferred=True)
            log.info("解析结果（%s）copies=%s color=%s sides=%s", source, guessed.copies, guessed.color, guessed.sides)
            return guessed, source
        except Exception as exc:
            last_error = exc
            log.warning("大模型识别失败（%s/%s）%s：%s", attempt, config.ollama_retries, path.name, exc)
            if attempt < config.ollama_retries: time.sleep(min(2.0 * attempt, 8.0))
            
    log.error("大模型多次失败：%s | %s", path.name, last_error)
    return None, fallback_source


# ==========================================
# 文件系统监控与处理服务
# ==========================================
def should_ignore(path: Path) -> bool:
    """【文件过滤】判断文件是否为需忽略的临时文件、隐藏文件或文件夹。"""
    name = path.name
    try:
        if not path.is_file(): return True
    except OSError: return True
    if name.startswith(IGNORE_PREFIXES) or path.suffix.lower() in IGNORE_SUFFIXES: return True
    return False


def wait_until_stable(path: Path, stable_seconds: float, timeout: float) -> bool:
    """【文件防抖】等待文件写入完成（大小和修改时间连续 N 秒不变）。"""
    deadline = time.time() + timeout
    last_size, last_mtime = -1, -1
    stable_since: float | None = None

    while time.time() < deadline:
        if not path.exists(): return False
        try:
            stat = path.stat()
            size, mtime_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            time.sleep(0.2); continue

        now = time.time()
        if size == last_size and mtime_ns == last_mtime and size > 0:
            if stable_since is None: stable_since = now
            elif now - stable_since >= stable_seconds: return True
        else:
            last_size, last_mtime = size, mtime_ns
            stable_since = None
        time.sleep(0.2)

    log.warning("等待文件稳定超时：%s", path.name)
    return False


class WatchService:
    """【监控服务核心类】负责线程池调度、文件监控和任务分配。"""
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tracker = FileTracker(config.processed_ttl, config.processed_max)
        self.db = PrintLogDB(config.db_path)
        self.print_engine = PrintEngine()  # 实例化打印引擎
        
        # 队列中存 (去重Key, 原始Path)，兼顾去重和展示
        self.queue: queue.Queue[tuple[str, Path] | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    def start_workers(self) -> None:
        """【启动工作线程】根据配置数量启动消费者线程。"""
        for index in range(self.config.workers):
            thread = threading.Thread(target=self._worker_loop, name=f"print-worker-{index + 1}", daemon=True)
            thread.start()
            self._workers.append(thread)
        log.info("已启动 %s 个处理线程", self.config.workers)

    def stop(self) -> None:
        """【停止服务】向队列发送毒丸信号，等待所有线程安全退出。"""
        self._stop.set()
        for _ in self._workers: self.queue.put(None)
        for thread in self._workers: thread.join(timeout=5)

    def close(self) -> None:
        """【关闭服务】停止线程并关闭数据库。"""
        self.stop()
        self.db.close()

    def seed_existing(self, watch_dir: Path) -> int:
        """【预热扫描】启动时把目录里已有的文件标记为已处理，防止重新打一遍。"""
        count = 0
        if not watch_dir.exists(): return 0
        for item in watch_dir.iterdir():
            if should_ignore(item): continue
            fp = fingerprint_of(item)
            if fp: 
                self.tracker.seed_processed(normalize_path(item), fp)
                count += 1
        return count

    def submit(self, path: Path) -> None:
        """【提交任务】将文件推入处理队列，自带防重入机制。"""
        if self._stop.is_set(): return
        try:
            if should_ignore(path): return
            fp = fingerprint_of(path)
            if not fp: return
            key = normalize_path(path)
            if self.tracker.try_claim(key, fp): # 抢占成功才入队
                self.queue.put((key, path))
        except OSError as exc:
            log.debug("提交跳过 %s：%s", path, exc)

    def _worker_loop(self) -> None:
        """【消费者循环】工作线程不断从队列获取任务并处理。"""
        while not self._stop.is_set():
            try: item = self.queue.get(timeout=0.5)
            except queue.Empty: continue
            
            if item is None:
                self.queue.task_done(); break
                
            key, path = item
            try:
                self._handle_claimed(key, path)
            except Exception:
                log.exception("处理文件异常：%s", path.name)
                delay = self.tracker.release_for_retry(key)
                if delay == -1.0:
                    self.db.log(path, intent=None, source="system", status="dead", error="max retries exceeded")
            finally:
                self.queue.task_done()

    def _handle_claimed(self, key: str, path: Path) -> None:
        """【核心处理闭环】等待稳定 -> 识别意图 -> 打印 -> 落库记录。"""
        if should_ignore(path):
            self.tracker.release(key); return
            
        if not wait_until_stable(path, self.config.stable_seconds, self.config.stable_timeout):
            delay = self.tracker.release_for_retry(key)
            if delay > 0: log.info("文件未就绪，%.1f 秒后可重试：%s", delay, path.name)
            return

        fp = fingerprint_of(path)
        if not fp:
            self.tracker.release(key); return

        log.info("检测到新文件：%s", path.name)
        intent, source = analyze_print_intent(path, self.config)

        if intent is None:
            self.db.log(path, intent=None, source=source, status="failed", error="intent recognition failed")
            delay = self.tracker.release_for_retry(key)
            if delay > 0: log.info("意图识别失败，%.1f 秒后重试", delay)
            else: self.db.log(path, intent=None, source=source, status="dead", error="max retries exceeded")
            return

        if not intent.need_print:
            self.db.log(path, intent=intent, source=source, status="ignored")
            log.info("忽略文件：%s", path.name)
            self.tracker.complete(key, fp); return

        if intent.inferred:
            log.warning("文件名未包含明确需求，按推断执行。文件=%s", path.name)
        
        try:
            # 【核心修改】把解析出来的 intent 传给引擎，让底层能够根据份数/颜色/单双面去构建打印参数
            print_result = self.print_engine.execute(str(path), intent)
            
            if print_result["status"] == "failed":
                self.db.log(path, intent=intent, source=source, status="failed", error=print_result["msg"])
                delay = self.tracker.release_for_retry(key)
                if delay > 0: log.info("%.1f 秒后重试：%s", delay, path.name)
                else: self.db.log(path, intent=intent, source=source, status="dead", error="max retries exceeded")
                return
            
            elif print_result["status"] == "fallback":
                self.db.log(path, intent=intent, source=source, status="fallback", error=print_result["msg"])
                log.warning("打印降级 (Fallback)：%s", print_result["msg"])
                self.tracker.complete(key, fp); return
                
            log.info("打印执行结果：%s", print_result["msg"])
            
        except Exception as exc:
            self.db.log(path, intent=intent, source=source, status="failed", error=str(exc))
            self.tracker.release_for_retry(key)
            return

        self.db.log(path, intent=intent, source=source, status="success")
        self.tracker.complete(key, fp)

    def run_with_watchdog(self, watch_dir: Path) -> None:
        """【事件驱动监控】使用 watchdog 监听文件系统操作。"""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        service = self
        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory: service.submit(Path(event.src_path))
            def on_moved(self, event):
                if not event.is_directory: service.submit(Path(event.dest_path))
            def on_closed(self, event):
                if not getattr(event, "is_directory", False): service.submit(Path(event.src_path))

        observer = Observer()
        observer.schedule(Handler(), str(watch_dir), recursive=False)
        observer.start()
        log.info("开始监控（watchdog）：%s", watch_dir)
        try:
            while not self._stop.is_set():
                self._scan_once(watch_dir)
                self._stop.wait(self.config.poll_interval)
        finally:
            observer.stop(); observer.join(timeout=5)

    def _scan_once(self, watch_dir: Path) -> None:
        """【全量扫描】定期扫一遍目录，防止 watchdog 丢事件。"""
        try:
            for item in watch_dir.iterdir():
                if self._stop.is_set(): break
                if item.is_file(): self.submit(item)
        except FileNotFoundError: watch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc: log.warning("扫描目录失败：%s", exc)

    def run_with_polling(self, watch_dir: Path) -> None:
        """【轮询监控降级】如果没装 watchdog，退化为死循环定时扫描。"""
        log.info("开始监控（轮询）：%s", watch_dir)
        while not self._stop.is_set():
            self._scan_once(watch_dir)
            self._stop.wait(self.config.poll_interval)


# ==========================================
# 命令行参数与主函数入口
# ==========================================
def parse_args() -> argparse.Namespace:
    """【参数解析】接收并解析命令行传入的参数。"""
    parser = argparse.ArgumentParser(description="监控打印接收目录并自动打印")
    parser.add_argument("--dir", default=str(DEFAULT_WATCH_DIR), help=r"监控目录")
    parser.add_argument("--include-existing", action="store_true", help="启动时对已有文件也执行打印")
    parser.add_argument("--model", default=DEFAULT_OLLAMA_MODEL, help="Ollama 文本模型名")
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL, help="Ollama 视觉模型名")
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama 地址")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite 日志路径")
    parser.add_argument("--workers", type=int, default=2, help="并发线程数")
    parser.add_argument("--retries", type=int, default=3, help="模型调用重试次数")
    parser.add_argument("--ollama-timeout", type=float, default=60.0, help="请求超时")
    parser.add_argument("--stable-seconds", type=float, default=0.8, help="文件稳定时间")
    parser.add_argument("--stable-timeout", type=float, default=60.0, help="等待稳定超时")
    parser.add_argument("--processed-ttl", type=float, default=86400.0, help="已处理记录TLL")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    """【构建配置】将命令行参数转换为只读的 Config 对象。"""
    return Config(
        watch_dir=Path(args.dir), ollama_host=str(args.host).rstrip("/"),
        ollama_model=args.model, ollama_vision_model=args.vision_model,
        ollama_timeout=max(1.0, args.ollama_timeout), ollama_retries=max(1, args.retries),
        stable_seconds=max(0.2, args.stable_seconds), stable_timeout=max(1.0, args.stable_timeout),
        poll_interval=1.0, workers=max(1, args.workers), include_existing=args.include_existing,
        processed_ttl=max(60.0, args.processed_ttl), processed_max=20000,
        log_level=args.log_level, db_path=Path(args.db_path),
    )


def check_ollama(config: Config) -> None:
    """【环境检查】启动前检查 Ollama 是否连通，模型是否就绪。"""
    names = fetch_ollama_models(config.ollama_host)
    if names is None:
        log.warning("无法连接 Ollama。"); return
    if model_installed(config.ollama_model, names):
        log.info("文本模型已就绪：%s", config.ollama_model)
    else:
        log.error("未找到文本模型 %s，请执行 ollama pull %s", config.ollama_model, config.ollama_model)
    if model_installed(config.ollama_vision_model, names):
        log.info("视觉模型已就绪：%s", config.ollama_vision_model)
    else:
        log.warning("未找到视觉模型 %s，图片将降级处理。", config.ollama_vision_model)


def main() -> int:
    """【程序主入口】负责初始化、拉起监控服务并阻塞运行。"""
    args = parse_args()
    config = build_config(args)
    setup_logging(config.log_level)

    try: config.watch_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.exception("无法创建监控目录：%s", e); return 1

    check_ollama(config)
    try: service = WatchService(config)
    except sqlite3.Error as e:
        log.exception("数据库初始化失败：%s", e); return 1
    
    log.info("日志数据库：%s", config.db_path)
    service.start_workers()

    if config.include_existing:
        try:
            for item in sorted(config.watch_dir.iterdir()): service.submit(item)
        except OSError: pass
    else:
        skipped = service.seed_existing(config.watch_dir)
        log.info("忽略启动前已存在的 %s 个文件", skipped)

    try:
        try:
            service.run_with_watchdog(config.watch_dir)
        except ImportError:
            log.warning("未安装 watchdog，改用轮询。")
            service.run_with_polling(config.watch_dir)
    except KeyboardInterrupt:
        log.info("已停止监控")
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ==============================================================================
# 【全局架构流程图】
# ==============================================================================
# 
#  [启动 main()]
#        │
#        ├── 解析命令行参数 (parse_args)
#        ├── 构建配置对象 (build_config)
#        ├── 初始化日志系统 (setup_logging)
#        ├── 检查 Ollama 及模型 (check_ollama)
#        └── 实例化 WatchService
#              ├── 初始化 SQLite 日志库 (PrintLogDB, WAL模式)
#              ├── 启动 N 个后台工作线程 (_worker_loop)
#              └── 标记历史文件 (seed_existing) 
#        │
#        ▼
#  [监听文件系统 run_with_watchdog / run_with_polling]
#        │
#        ├── (Watchdog事件 / 定时扫描) 发现新文件
#        └── submit(文件)
#              ├── should_ignore? (过滤临时文件)
#              ├── fingerprint_of? (获取大小+修改时间指纹)
#              ├── FileTracker.try_claim? (防抖去重，锁定处理权)
#              └── 放入 Queue 队列
#        │
#        ▼
#  [Worker 线程消费队列 _worker_loop]
#        │
#        └── _handle_claimed(去重Key, 原始Path)
#              ├── wait_until_stable? (等待文件写入完成防抖)
#              │
#              ├── analyze_print_intent? (核心意图解析)
#              │     ├── parse_filename_intent? (正则直接提取)
#              │     ├── _call_ollama_once? (视觉/文本大模型兜底)
#              │     └── validate_intent? (防呆校验枚举与范围)
#              │
#              ├── (如果不打印) -> PrintLogDB.log(ignored)
#              │
#              ├── print_engine.execute(path, intent)? (真实打印引擎)
#              │     ├── _normalize (格式转 PDF)
#              │     ├── 模式 A (无实体机) -> 存桌面 (success)
#              │     ├── 模式 B (有实体机) -> Sumatra静默打印 (success)
#              │     └── 模式 B 失败 -> 降级存桌面 (fallback)
#              │
#              └── 记录结果到数据库 (PrintLogDB.log)
#                    ├── 成功 -> tracker.complete
#                    ├── 失败 -> tracker.release_for_retry (指数退避)
#                    └── 死循环 -> (>=10次熔断) -> 记录 dead 状态
#
# ==============================================================================