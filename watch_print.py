"""
监控打印接收目录：有新文件时打印日志并调用 mock_print。

依赖（推荐）：
    pip install watchdog

无 watchdog 时会自动改用轮询，功能相同。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

WATCH_DIR = Path(r"C:\打印接收")
STABLE_SECONDS = 0.8
POLL_INTERVAL = 1.0
IGNORE_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part"}
IGNORE_PREFIXES = ("~$", ".",)

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("watch_print")


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    watch_dir = Path(args.dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

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
