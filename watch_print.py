"""
监控打印接收目录：有新文件时先问本地 Ollama 是否需要打印，
需要则调用 mock_print，否则忽略。

依赖（推荐）：
    pip install watchdog

无 watchdog 时会自动改用轮询，功能相同。
Ollama 需在本机 11434 端口运行。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("watch_print")

DEFAULT_WATCH_DIR = Path(r"C:\打印接收")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

IGNORE_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part"}
IGNORE_PREFIXES = ("~$", ".")
TEXT_PREVIEW_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log"}
MAX_PREVIEW_CHARS = 800
ALLOWED_COLORS = {"color", "mono"}
ALLOWED_SIDES = {"single", "double"}
MAX_COPIES = 100
OLLAMA_RETRY_CAP = 60.0


@dataclass(frozen=True)
class Config:
    watch_dir: Path
    ollama_host: str
    ollama_model: str
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


@dataclass(frozen=True)
class PrintIntent:
    need_print: bool
    copies: int
    color: str
    sides: str


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def normalize_path(path: Path) -> str:
    raw = os.path.abspath(str(path))
    try:
        raw = str(Path(raw).resolve())
    except OSError:
        pass
    return os.path.normcase(raw)


def fingerprint_of(path: Path) -> FileFingerprint | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


class FileTracker:
    """pending 防重入；processed 按指纹去重，失败不永久忽略，并定期清理。"""

    def __init__(self, ttl: float, max_entries: int) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._pending: dict[str, float] = {}
        self._processed: dict[str, tuple[FileFingerprint, float]] = {}
        self._retry_after: dict[str, float] = {}
        self._fail_counts: dict[str, int] = {}

    def seed_processed(self, key: str, fingerprint: FileFingerprint) -> None:
        with self._lock:
            self._processed[key] = (fingerprint, time.time())

    def try_claim(self, key: str, fingerprint: FileFingerprint) -> bool:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            if key in self._pending:
                return False
            if now < self._retry_after.get(key, 0):
                return False
            previous = self._processed.get(key)
            if previous is not None and previous[0] == fingerprint:
                return False
            self._pending[key] = now
            return True

    def complete(self, key: str, fingerprint: FileFingerprint) -> None:
        with self._lock:
            self._pending.pop(key, None)
            self._retry_after.pop(key, None)
            self._fail_counts.pop(key, None)
            self._processed[key] = (fingerprint, time.time())
            self._evict_locked()

    def release_for_retry(self, key: str) -> float:
        with self._lock:
            self._pending.pop(key, None)
            fails = self._fail_counts.get(key, 0) + 1
            self._fail_counts[key] = fails
            delay = min(2.0 * (2 ** (fails - 1)), OLLAMA_RETRY_CAP)
            self._retry_after[key] = time.time() + delay
            return delay

    def release(self, key: str) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, (_, done_at) in self._processed.items()
            if now - done_at >= self._ttl or not os.path.exists(key)
        ]
        for key in expired:
            self._processed.pop(key, None)
        stale_retry = [key for key, until in self._retry_after.items() if now >= until]
        for key in stale_retry:
            if key not in self._pending:
                self._retry_after.pop(key, None)

    def _evict_locked(self) -> None:
        overflow = len(self._processed) - self._max_entries
        if overflow <= 0:
            return
        oldest = sorted(self._processed.items(), key=lambda item: item[1][1])
        for key, _ in oldest[:overflow]:
            self._processed.pop(key, None)


def _read_text_preview(path: Path) -> str:
    if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
        return ""
    try:
        data = path.read_bytes()[: MAX_PREVIEW_CHARS * 4]
        text = data.decode("utf-8", errors="ignore").strip()
        return text[:MAX_PREVIEW_CHARS]
    except OSError:
        log.debug("读取预览失败：%s", path)
        return ""


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型原文中取出 JSON 对象并用 json 库解析。"""
    raw = text.strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"非法 need_print：{value!r}")


def _coerce_copies(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"非法 copies：{value!r}")
    if isinstance(value, int):
        copies = value
    elif isinstance(value, str) and value.strip().isdigit():
        copies = int(value.strip())
    else:
        raise ValueError(f"非法 copies：{value!r}")
    if copies < 1 or copies > MAX_COPIES:
        raise ValueError(f"copies 超出范围 1-{MAX_COPIES}：{copies}")
    return copies


def _coerce_choice(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"非法 {field}：{value!r}")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"非法 {field}：{value!r}，允许 {sorted(allowed)}")
    return normalized


def validate_intent(data: dict[str, Any]) -> PrintIntent:
    missing = [key for key in ("need_print", "copies", "color", "sides") if key not in data]
    if missing:
        raise ValueError(f"缺少字段：{', '.join(missing)}")
    return PrintIntent(
        need_print=_coerce_bool(data["need_print"]),
        copies=_coerce_copies(data["copies"]),
        color=_coerce_choice(data["color"], ALLOWED_COLORS, "color"),
        sides=_coerce_choice(data["sides"], ALLOWED_SIDES, "sides"),
    )


def _ollama_request(host: str, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    url = f"{host.rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Ollama {url}: {exc.reason}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama 返回非 JSON：{body[:200]}") from exc


def fetch_ollama_models(host: str, timeout: float = 5.0) -> list[str] | None:
    try:
        payload = _ollama_request(host, "GET", "/api/tags", None, timeout)
    except RuntimeError as exc:
        log.warning("无法连接 Ollama（%s）：%s", host, exc)
        return None
    names: list[str] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        for field in ("name", "model"):
            value = item.get(field)
            if isinstance(value, str) and value:
                names.append(value)
    return names


def model_installed(model: str, names: list[str]) -> bool:
    target = model.lower()
    for name in names:
        candidate = name.lower()
        if candidate == target or candidate.startswith(f"{target}-"):
            return True
    return False


def build_intent_prompt(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    prompt = (
        "你是打印店的意图识别助手。根据文件名、扩展名和可选文本，推断打印参数。\n"
        "只输出一个 JSON 对象，不要输出任何解释、Markdown 或其它文字。\n"
        "JSON 格式必须是：\n"
        '{"need_print": true, "copies": 2, "color": "color", "sides": "double"}\n'
        "字段说明：\n"
        "- need_print: 是否需要打印。文档、试卷、作业、合同、照片、设计稿、"
        "PDF/Word/图片等为 true；压缩包、安装包、可执行文件、系统临时文件、"
        "明显不是给人看的数据文件为 false。\n"
        "- copies: 打印份数，整数。文件名里有“2份/两份/x copies”等就用该数字，"
        "未说明则填 1。必须是 1 到 100 的整数。\n"
        "- color: 色彩模式，只能是 \"color\"（彩印）或 \"mono\"（黑白）。"
        "文件名含彩印/彩色/color 用 color，含黑白/mono 用 mono，未说明默认 mono。\n"
        "- sides: 单双面，只能是 \"single\" 或 \"double\"。"
        "文件名含双面/double 用 double，含单面/single 用 single，未说明默认 single。\n"
        f"文件名：{path.name}\n"
        f"扩展名：{path.suffix or '(无)'}\n"
        f"大小：{size} 字节\n"
    )
    preview = _read_text_preview(path)
    if preview:
        prompt += f"文本预览：\n{preview}\n"
    return prompt


def _call_ollama_once(path: Path, config: Config) -> PrintIntent:
    body = _ollama_request(
        config.ollama_host,
        "POST",
        "/api/generate",
        {
            "model": config.ollama_model,
            "prompt": build_intent_prompt(path),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        config.ollama_timeout,
    )
    answer = str(body.get("response", "")).strip()
    data = extract_json_object(answer)
    if data is None:
        raise ValueError(f"未能解析 JSON，原文：{answer[:200]}")
    return validate_intent(data)


def analyze_print_intent(path: Path, config: Config) -> PrintIntent | None:
    """调用本地 Ollama，校验打印意图 JSON。失败返回 None 以便稍后重试。"""
    last_error: Exception | None = None
    attempts = max(1, config.ollama_retries)
    for attempt in range(1, attempts + 1):
        try:
            intent = _call_ollama_once(path, config)
            log.info("解析结果 copies=%s color=%s", intent.copies, intent.color)
            return intent
        except (RuntimeError, ValueError, TypeError) as exc:
            last_error = exc
            log.warning(
                "Ollama 意图识别失败（%s/%s）%s：%s",
                attempt,
                attempts,
                path.name,
                exc,
            )
            if attempt < attempts:
                time.sleep(min(2.0 * attempt, 8.0))
    log.error("Ollama 多次失败，稍后重试：%s | %s", path.name, last_error)
    return None


def mock_print(file_path: str) -> None:
    """模拟打印动作。后续可替换为真实打印调用。"""
    path = Path(file_path)
    size = path.stat().st_size if path.exists() else 0
    log.info("模拟打印：%s（大小 %s 字节）", path.name, size)
    time.sleep(0.3)
    log.info("模拟打印完成：%s", path.name)


def should_ignore(path: Path) -> bool:
    name = path.name
    try:
        if not path.is_file():
            return True
    except OSError:
        return True
    if name.startswith(IGNORE_PREFIXES):
        return True
    if path.suffix.lower() in IGNORE_SUFFIXES:
        return True
    return False


def wait_until_stable(path: Path, stable_seconds: float, timeout: float) -> bool:
    """等待文件写入完成。超时或文件消失时返回 False。"""
    deadline = time.time() + timeout
    last_size = -1
    last_mtime = -1
    stable_since: float | None = None

    while time.time() < deadline:
        if not path.exists():
            return False
        try:
            stat = path.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
        except OSError:
            time.sleep(0.2)
            continue

        now = time.time()
        unchanged = size == last_size and mtime_ns == last_mtime and size > 0
        if unchanged:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= stable_seconds:
                return True
        else:
            last_size = size
            last_mtime = mtime_ns
            stable_since = None
        time.sleep(0.2)

    log.warning("等待文件稳定超时：%s", path.name)
    return False


class WatchService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tracker = FileTracker(config.processed_ttl, config.processed_max)
        self.queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    def start_workers(self) -> None:
        for index in range(self.config.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"print-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)
        log.info("已启动 %s 个处理线程", self.config.workers)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            self.queue.put(None)
        for thread in self._workers:
            thread.join(timeout=5)

    def seed_existing(self, watch_dir: Path) -> int:
        count = 0
        if not watch_dir.exists():
            return 0
        for item in watch_dir.iterdir():
            if should_ignore(item):
                continue
            fingerprint = fingerprint_of(item)
            if fingerprint is None:
                continue
            self.tracker.seed_processed(normalize_path(item), fingerprint)
            count += 1
        return count

    def submit(self, path: Path) -> None:
        if self._stop.is_set():
            return
        try:
            if should_ignore(path):
                return
            fingerprint = fingerprint_of(path)
            if fingerprint is None:
                return
            key = normalize_path(path)
            if not self.tracker.try_claim(key, fingerprint):
                return
            self.queue.put(key)
        except OSError as exc:
            log.debug("提交跳过 %s：%s", path, exc)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self.queue.task_done()
                break
            try:
                self._handle_claimed(item)
            except Exception:
                log.exception("处理文件异常：%s", item)
                self.tracker.release_for_retry(item)
            finally:
                self.queue.task_done()

    def _handle_claimed(self, key: str) -> None:
        path = Path(key)
        if should_ignore(path):
            self.tracker.release(key)
            return
        if not wait_until_stable(
            path,
            self.config.stable_seconds,
            self.config.stable_timeout,
        ):
            delay = self.tracker.release_for_retry(key)
            log.info("文件未就绪，%.1f 秒后可重试：%s", delay, path.name)
            return

        fingerprint = fingerprint_of(path)
        if fingerprint is None:
            self.tracker.release(key)
            return

        log.info("检测到新文件：%s", path.name)
        intent = analyze_print_intent(path, self.config)
        if intent is None:
            delay = self.tracker.release_for_retry(key)
            log.info("意图识别失败，%.1f 秒后重试：%s", delay, path.name)
            return
        if not intent.need_print:
            log.info("忽略文件：%s", path.name)
            self.tracker.complete(key, fingerprint)
            return
        try:
            mock_print(str(path))
        except OSError:
            log.exception("模拟打印失败：%s", path.name)
            self.tracker.release_for_retry(key)
            return
        self.tracker.complete(key, fingerprint)

    def run_with_watchdog(self, watch_dir: Path) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        service = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    return
                service.submit(Path(event.src_path))

            def on_moved(self, event):
                if event.is_directory:
                    return
                service.submit(Path(event.dest_path))

            def on_closed(self, event):
                if getattr(event, "is_directory", False):
                    return
                service.submit(Path(event.src_path))

        observer = Observer()
        observer.schedule(Handler(), str(watch_dir), recursive=False)
        observer.start()
        log.info("开始监控（watchdog）：%s", watch_dir)
        try:
            while not self._stop.is_set():
                self._scan_once(watch_dir)
                self._stop.wait(self.config.poll_interval)
        finally:
            observer.stop()
            observer.join(timeout=5)

    def _scan_once(self, watch_dir: Path) -> None:
        try:
            for item in watch_dir.iterdir():
                if self._stop.is_set():
                    break
                if item.is_file():
                    self.submit(item)
        except FileNotFoundError:
            watch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("扫描目录失败：%s", exc)

    def run_with_polling(self, watch_dir: Path) -> None:
        log.info("开始监控（轮询）：%s", watch_dir)
        while not self._stop.is_set():
            self._scan_once(watch_dir)
            self._stop.wait(self.config.poll_interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控打印接收目录并模拟打印")
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_WATCH_DIR),
        help=r"监控目录，默认 C:\打印接收",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="启动时也对目录里已有文件执行模拟打印",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OLLAMA_MODEL,
        help="Ollama 模型名，默认 qwen2.5:3b，也可用环境变量 OLLAMA_MODEL",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_OLLAMA_HOST,
        help="Ollama 地址，默认 http://127.0.0.1:11434",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="后台处理线程数，默认 2",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="单次意图识别的 Ollama 重试次数，默认 3",
    )
    parser.add_argument(
        "--ollama-timeout",
        type=float,
        default=60.0,
        help="Ollama 请求超时秒数，默认 60",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=0.8,
        help="文件大小与 mtime 保持不变多久视为写完，默认 0.8",
    )
    parser.add_argument(
        "--stable-timeout",
        type=float,
        default=60.0,
        help="等待文件稳定的最长时间，超时不打印并稍后重试，默认 60",
    )
    parser.add_argument(
        "--processed-ttl",
        type=float,
        default=86400.0,
        help="已处理记录保留秒数，默认 86400",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="日志级别，默认 INFO",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    workers = max(1, args.workers)
    retries = max(1, args.retries)
    return Config(
        watch_dir=Path(args.dir),
        ollama_host=str(args.host).rstrip("/"),
        ollama_model=args.model,
        ollama_timeout=max(1.0, args.ollama_timeout),
        ollama_retries=retries,
        stable_seconds=max(0.2, args.stable_seconds),
        stable_timeout=max(1.0, args.stable_timeout),
        poll_interval=1.0,
        workers=workers,
        include_existing=args.include_existing,
        processed_ttl=max(60.0, args.processed_ttl),
        processed_max=20000,
        log_level=args.log_level,
    )


def check_ollama(config: Config) -> None:
    names = fetch_ollama_models(config.ollama_host)
    if names is None:
        log.warning(
            "无法连接 Ollama（%s）。请先启动 ollama serve，意图识别失败时将稍后重试。",
            config.ollama_host,
        )
        return
    if model_installed(config.ollama_model, names):
        log.info("已连接 Ollama：%s ，模型：%s", config.ollama_host, config.ollama_model)
        return
    sample = ", ".join(sorted(set(names))[:8]) or "(无)"
    log.error(
        "Ollama 中未找到模型 %s。已安装：%s。可用 ollama pull %s",
        config.ollama_model,
        sample,
        config.ollama_model,
    )


def main() -> int:
    args = parse_args()
    config = build_config(args)
    setup_logging(config.log_level)

    watch_dir = config.watch_dir
    try:
        watch_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("无法创建监控目录：%s", watch_dir)
        return 1

    check_ollama(config)
    service = WatchService(config)
    service.start_workers()

    if config.include_existing:
        log.info("将对已有文件执行模拟打印")
        try:
            for item in sorted(watch_dir.iterdir()):
                service.submit(item)
        except OSError:
            log.exception("读取已有文件失败：%s", watch_dir)
    else:
        skipped = service.seed_existing(watch_dir)
        log.info("忽略启动前已存在的 %s 个文件", skipped)

    try:
        try:
            service.run_with_watchdog(watch_dir)
        except ImportError:
            log.warning("未安装 watchdog，改用轮询。可执行：pip install watchdog")
            service.run_with_polling(watch_dir)
    except KeyboardInterrupt:
        log.info("已停止监控")
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
