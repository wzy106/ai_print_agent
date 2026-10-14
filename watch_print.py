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
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WATCH_DIR = Path(r"C:\打印接收")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = 60.0
STABLE_SECONDS = 0.8
POLL_INTERVAL = 1.0
IGNORE_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part"}
IGNORE_PREFIXES = ("~$", ".",)
TEXT_PREVIEW_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log"}
MAX_PREVIEW_CHARS = 800

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("watch_print")


def _read_text_preview(path: Path) -> str:
    if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
        return ""
    try:
        data = path.read_bytes()[: MAX_PREVIEW_CHARS * 4]
        text = data.decode("utf-8", errors="ignore").strip()
        return text[:MAX_PREVIEW_CHARS]
    except OSError:
        return ""


def extract_json_object(text: str) -> dict | None:
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


def analyze_print_intent(path: Path, model: str) -> dict | None:
    """调用本地 Ollama，解析打印意图 JSON。失败返回 None。"""
    preview = _read_text_preview(path)
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
        "未说明则填 1。\n"
        "- color: 色彩模式，只能是 \"color\"（彩印）或 \"mono\"（黑白）。"
        "文件名含彩印/彩色/color 用 color，含黑白/mono 用 mono，未说明默认 mono。\n"
        "- sides: 单双面，只能是 \"single\" 或 \"double\"。"
        "文件名含双面/double 用 double，含单面/single 用 single，未说明默认 single。\n"
        f"文件名：{path.name}\n"
        f"扩展名：{path.suffix or '(无)'}\n"
        f"大小：{size} 字节\n"
    )
    if preview:
        prompt += f"文本预览：\n{preview}\n"

    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        log.warning("Ollama 调用失败，按忽略处理：%s", exc)
        return None
    except json.JSONDecodeError:
        log.warning("Ollama 返回无法解析，按忽略处理：%s", path.name)
        return None

    answer = str(body.get("response", "")).strip()
    data = extract_json_object(answer)
    if data is None:
        log.warning("未能解析 JSON：%s | 原文：%s", path.name, answer[:200])
        return None

    copies = data.get("copies", 1)
    color = data.get("color", "mono")
    log.info("解析结果 copies=%s color=%s", copies, color)
    return data


def mock_print(file_path: str) -> None:
    """模拟打印动作。后续可替换为真实打印调用。"""
    path = Path(file_path)
    size = path.stat().st_size if path.exists() else 0
    log.info("模拟打印：%s（大小 %s 字节）", path.name, size)
    time.sleep(0.3)
    log.info("模拟打印完成：%s", path.name)


def should_ignore(path: Path) -> bool:
    name = path.name
    if not path.is_file():
        return True
    if name.startswith(IGNORE_PREFIXES):
        return True
    if path.suffix.lower() in IGNORE_SUFFIXES:
        return True
    return False


def wait_until_stable(path: Path, timeout: float = 60.0) -> bool:
    """等待文件写入完成（大小在一段时间内不再变化）。"""
    deadline = time.time() + timeout
    last_size = -1
    stable_since: float | None = None

    while time.time() < deadline:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.2)
            continue

        now = time.time()
        if size == last_size and size > 0:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= STABLE_SECONDS:
                return True
        else:
            last_size = size
            stable_since = now if size > 0 else None
        time.sleep(0.2)

    return path.exists()


def handle_new_file(path: Path, processed: set[str]) -> None:
    resolved = str(path.resolve())
    if resolved in processed:
        return
    if should_ignore(path):
        return
    if not wait_until_stable(path):
        log.warning("文件未就绪或已消失：%s", path.name)
        return
    if resolved in processed:
        return

    processed.add(resolved)
    log.info("检测到新文件：%s", path.name)
    intent = analyze_print_intent(path, OLLAMA_MODEL)
    if not intent or not intent.get("need_print"):
        log.info("忽略文件：%s", path.name)
        return
    try:
        mock_print(str(path))
    except Exception:
        log.exception("模拟打印失败：%s", path.name)


def scan_existing(watch_dir: Path) -> set[str]:
    found: set[str] = set()
    if not watch_dir.exists():
        return found
    for item in watch_dir.iterdir():
        if item.is_file() and not should_ignore(item):
            found.add(str(item.resolve()))
    return found


def run_with_watchdog(watch_dir: Path, processed: set[str]) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            handle_new_file(Path(event.src_path), processed)

        def on_moved(self, event):
            if event.is_directory:
                return
            handle_new_file(Path(event.dest_path), processed)

        def on_closed(self, event):
            if getattr(event, "is_directory", False):
                return
            handle_new_file(Path(event.src_path), processed)

    observer = Observer()
    observer.schedule(Handler(), str(watch_dir), recursive=False)
    observer.start()
    log.info("开始监控（watchdog）：%s", watch_dir)
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


def run_with_polling(watch_dir: Path, processed: set[str]) -> None:
    log.info("开始监控（轮询）：%s", watch_dir)
    while True:
        try:
            for item in watch_dir.iterdir():
                if item.is_file():
                    handle_new_file(item, processed)
        except FileNotFoundError:
            watch_dir.mkdir(parents=True, exist_ok=True)
        time.sleep(POLL_INTERVAL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控打印接收目录并模拟打印")
    parser.add_argument(
        "--dir",
        default=str(WATCH_DIR),
        help=r"监控目录，默认 C:\打印接收",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="启动时也对目录里已有文件执行模拟打印",
    )
    parser.add_argument(
        "--model",
        default=OLLAMA_MODEL,
        help="Ollama 模型名，默认 qwen2.5:3b，也可用环境变量 OLLAMA_MODEL",
    )
    return parser.parse_args()


def ping_ollama() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError:
        return False


def main() -> int:
    global OLLAMA_MODEL
    args = parse_args()
    OLLAMA_MODEL = args.model
    watch_dir = Path(args.dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    if ping_ollama():
        log.info("已连接 Ollama：%s ，模型：%s", OLLAMA_HOST, OLLAMA_MODEL)
    else:
        log.warning(
            "无法连接 Ollama（%s）。请先启动 ollama serve，意图识别失败时将忽略文件。",
            OLLAMA_HOST,
        )

    processed = set() if args.include_existing else scan_existing(watch_dir)
    if args.include_existing:
        log.info("将对已有文件执行模拟打印")
        for item in sorted(watch_dir.iterdir()):
            handle_new_file(item, processed)
    else:
        log.info("忽略启动前已存在的 %s 个文件", len(processed))

    try:
        try:
            run_with_watchdog(watch_dir, processed)
        except ImportError:
            log.warning("未安装 watchdog，改用轮询。可执行：pip install watchdog")
            run_with_polling(watch_dir, processed)
    except KeyboardInterrupt:
        log.info("已停止监控")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
