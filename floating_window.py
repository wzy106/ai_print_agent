import sys
import re
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QTextEdit, QMenu, QAction
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from watch_print import (
    Config, analyze_print_intent, mock_print, PrintLogDB, PrintIntent,
    DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, DEFAULT_VISION_MODEL, DEFAULT_DB_PATH,
    parse_filename_intent, SOURCE_FILENAME
)

config = Config(
    watch_dir=Path(r"C:\打印接收"), ollama_host=DEFAULT_OLLAMA_HOST,
    ollama_model=DEFAULT_OLLAMA_MODEL, ollama_vision_model=DEFAULT_VISION_MODEL,
    ollama_timeout=60.0, ollama_retries=3, stable_seconds=0.8, stable_timeout=60.0,
    poll_interval=1.0, workers=2, include_existing=False,
    processed_ttl=86400.0, processed_max=20000, log_level="INFO", db_path=Path(DEFAULT_DB_PATH)
)

INTENT_KEYWORDS = re.compile(r"(打印|彩|黑白|单面|双面|\d+份|[一二两三四五六七八九十]+份)")

class ParseWorker(QThread):
    finished = pyqtSignal(str) 

    def __init__(self, file_path: str, requirement_text: str = None):
        super().__init__()
        self.file_path = file_path
        self.requirement_text = requirement_text # 【新增】接收文字需求

    def run(self):
        path = Path(self.file_path)
        db = None
        try:
            db = PrintLogDB(config.db_path)
            
            intent = None
            source = ""

            # 【核心改动】如果店长复制了文字需求，优先用正则解析文字！
            # 我们把复制的文字加个 ".txt" 后缀，直接复用写好的文件名解析逻辑
            if self.requirement_text:
                fake_filename = f"req_{self.requirement_text}.txt"
                intent = parse_filename_intent(fake_filename)
                if intent:
                    source = "clipboard_text"
            
            # 如果文字解析失败（比如文字没写明确参数），或者没有文字，再走原来的文件解析
            if intent is None:
                intent, source = analyze_print_intent(path, config)
            
            if intent is None:
                self.finished.emit(f"❌ 解析失败：{path.name}")
                return
            
            if not intent.need_print:
                db.log(path, intent=intent, source=source, status="ignored")
                self.finished.emit(f"⏭️ 无需打印：{path.name}")
                return

            mock_print(str(path))
            db.log(path, intent=intent, source=source, status="success")
            
            self.finished.emit(
                f"✅ 打印成功：{path.name}\n"
                f"来源: {source} | {intent.copies}份 | {intent.color} | {intent.sides}\n"
                f"{'-'*30}"
            )
        except Exception as e:
            self.finished.emit(f"❌ 错误：{str(e)}")
        finally:
            if db: db.close()


class FloatingWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.current_file = None
        self.initUI()
        self.init_clipboard()

    def initUI(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(320, 450)
        self.setFocusPolicy(Qt.StrongFocus)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.title_label = QLabel("✨ AI 打印管家\n1.拖入文件\n2.在微信复制需求文字\n(按 Esc 退出)", self)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("""
            QLabel { color: white; font-size: 14px; font-weight: bold;
                background-color: rgba(30, 30, 30, 200); border-radius: 10px;
                padding: 15px; border: 2px dashed #00aaff; }
        """)
        layout.addWidget(self.title_label)

        self.log_text = QTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit { background-color: rgba(50, 50, 50, 200); color: #00ff00;
                border: none; border-radius: 10px; font-family: Consolas; padding: 5px; }
        """)
        layout.addWidget(self.log_text)

        self.setAcceptDrops(True)
        self.drag_position = None

    def init_clipboard(self):
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

    def on_clipboard_changed(self):
        text = self.clipboard.text().strip()
        if not text:
            return

        # 只有在已拖入文件，且文字包含打印意图时，才触发
        if INTENT_KEYWORDS.search(text) and self.current_file:
            self.log_text.append(f"📋 捕获文字需求：{text}")
            self.log_text.append(f"🔗 正在与文件绑定并解析...")
            
            # 【改动】把文字和文件路径一起传进去
            self.trigger_processing(self.current_file, text)
            
            self.current_file = None 
            self.title_label.setText("✨ 等待下一单...\n(拖文件或复制要求)")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.title_label.setStyleSheet(self.title_label.styleSheet().replace("#00aaff", "#00ff00"))

    def dragLeaveEvent(self, event):
        self.title_label.setStyleSheet(self.title_label.styleSheet().replace("#00ff00", "#00aaff"))

    def dropEvent(self, event):
        self.title_label.setStyleSheet(self.title_label.styleSheet().replace("#00ff00", "#00aaff"))
        mime_data = event.mimeData()
        
        if mime_data.hasUrls():
            url = mime_data.urls()[0]
            file_path = url.toLocalFile()
            if file_path:
                self.current_file = file_path
                self.log_text.append(f"📥 已捕获文件：{Path(file_path).name}")
                self.log_text.append(f"👉 请去微信复制客户的文字要求...")
                self.title_label.setText("✨ 已收到文件\n等待复制需求文字...")
                event.acceptProposedAction()

    # 【改动】接收 text 参数
    def trigger_processing(self, file_path, text=None):
        self.worker = ParseWorker(file_path, text) 
        self.worker.finished.connect(lambda res: self.log_text.append(res))
        self.worker.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPos() - self.drag_position)
            event.accept()
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape: QApplication.quit()
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #333; color: white; }")
        quit_action = QAction("退出管家", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        menu.exec_(event.globalPos())

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = FloatingWindow()
    window.show()
    sys.exit(app.exec_())