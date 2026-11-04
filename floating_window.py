import sys
import re
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, 
    QListWidgetItem, QPushButton, QMenu, QAction, QSystemTrayIcon, QGraphicsDropShadowEffect, QMessageBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor

from watch_print import (
    Config, analyze_print_intent, mock_print, PrintLogDB,
    DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, DEFAULT_VISION_MODEL, DEFAULT_DB_PATH,
    parse_filename_intent
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
    finished = pyqtSignal(dict, str, str)

    def __init__(self, file_path: str, requirement_text: str = None):
        super().__init__()
        self.file_path = file_path
        self.requirement_text = requirement_text

    def run(self):
        path = Path(self.file_path)
        db = None
        try:
            db = PrintLogDB(config.db_path)
            intent = None
            source = ""
            
            # 1. 如果传入了文字需求，必须优先解析
            if self.requirement_text:
                fake_filename = f"req_{self.requirement_text}.txt"
                intent = parse_filename_intent(fake_filename)
                if intent:
                    source = "clipboard_text"
                else:
                    # 【关键修改】文字解析失败，直接报错，绝不降级去调模型！
                    self.finished.emit({}, "复制文字未包含明确参数", "failed")
                    return
            
            # 2. 如果根本没有文字需求，才去调用模型看图/看文本
            if intent is None:
                intent, source = analyze_print_intent(path, config)
            
            if intent is None:
                self.finished.emit({}, "解析失败", "failed")
                return
            
            if not intent.need_print:
                db.log(path, intent=intent, source=source, status="ignored")
                self.finished.emit({}, "无需打印", "ignored")
                return

            mock_print(str(path))
            db.log(path, intent=intent, source=source, status="success")
            
            res = {
                "file": path.name, "copies": intent.copies,
                "color": intent.color, "sides": intent.sides, "source": source
            }
            self.finished.emit(res, "完成", "success")
            
        except Exception as e:
            self.finished.emit({}, f"错误: {str(e)}", "failed")
        finally:
            if db: db.close()


class FloatingWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.current_file = None
        self.is_quitting = False
        self.worker = None
        self.initUI()
        self.init_clipboard()
        self.init_tray()

    def initUI(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(340, 480)
        self.setFocusPolicy(Qt.StrongFocus)

        self.main_widget = QWidget(self)
        self.main_widget.setGeometry(10, 10, 320, 460)
        self.main_widget.setStyleSheet("QWidget { background-color: rgba(25, 25, 30, 240); border-radius: 15px; }")
        
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.main_widget.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self.main_widget)
        layout.setContentsMargins(15, 15, 15, 15)

        top_layout = QHBoxLayout()
        self.status_label = QLabel("🟢 等待文件")
        self.status_label.setStyleSheet("color: #00ff00; font-weight: bold; font-size: 14px;")
        
        self.close_btn = QPushButton("✖")
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setStyleSheet("QPushButton { color: #888; font-weight: bold; border: none; } QPushButton:hover { color: white; }")
        self.close_btn.clicked.connect(self.hide) 
        
        top_layout.addWidget(self.status_label)
        top_layout.addStretch()
        top_layout.addWidget(self.close_btn)
        layout.addLayout(top_layout)

        self.drop_area = QLabel("📥 请将文件拖拽至此\n然后去微信复制需求文字")
        self.drop_area.setAlignment(Qt.AlignCenter)
        self.reset_drop_area()
        layout.addWidget(self.drop_area)

        # 【新增】手动读取剪贴板的兜底按钮
        self.manual_btn = QPushButton("📋 手动读取剪贴板文字")
        self.manual_btn.setStyleSheet("""
            QPushButton { background-color: #333; color: white; border-radius: 5px; padding: 5px; font-size: 12px; }
            QPushButton:hover { background-color: #444; }
        """)
        self.manual_btn.clicked.connect(self.manual_read_clipboard)
        layout.addWidget(self.manual_btn)

        self.task_list = QListWidget()
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setWordWrap(True)
        self.task_list.setStyleSheet("""
            QListWidget { background-color: transparent; border: none; color: #ddd; font-family: Consolas; font-size: 12px; }
            QListWidget::item { border-bottom: 1px solid #333; padding: 8px 0px; }
            QScrollBar:vertical { background: transparent; width: 6px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,50); min-height: 20px; border-radius: 3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        layout.addWidget(self.task_list)

        self.setAcceptDrops(True)
        self.drag_position = None

    def init_clipboard(self):
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

    def init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        self.tray_icon.setToolTip("AI 打印管家")
        
        tray_menu = QMenu()
        show_action = QAction("显示面板", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("彻底退出", self)
        quit_action.triggered.connect(self.safe_quit)
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def safe_quit(self):
        self.is_quitting = True
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.warning(self, "正在处理", "有任务正在处理中，强行退出可能导致中断！\n确定退出吗？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No: return
            self.worker.terminate()
            self.worker.wait()
        self.tray_icon.hide()
        QApplication.quit()

    def on_clipboard_changed(self):
        text = self.clipboard.text().strip()
        if not text: return

        if INTENT_KEYWORDS.search(text) and self.current_file:
            self.trigger_processing(self.current_file, text)
            self.current_file = None
            self.update_status("⚙️ 正在解析...", "#ffaa00")

    # 【新增】手动读取剪贴板逻辑
    def manual_read_clipboard(self):
        if not self.current_file:
            QMessageBox.warning(self, "提示", "请先拖入一个文件！")
            return
        
        text = self.clipboard.text().strip()
        if not text:
            QMessageBox.warning(self, "提示", "剪贴板是空的，请去微信复制客户的文字要求。")
            return
            
        self.task_list.addItem(f"📋 手动捕获：{text}")
        self.trigger_processing(self.current_file, text)
        self.current_file = None
        self.update_status("⚙️ 正在解析...", "#ffaa00")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_area.setStyleSheet("QLabel { color: #00ff00; border: 2px solid #00ff00; background-color: rgba(0, 50, 0, 100); border-radius: 10px; padding: 20px; }")

    def dragLeaveEvent(self, event):
        self.reset_drop_area()

    def dropEvent(self, event):
        self.reset_drop_area()
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            file_path = url.toLocalFile()
            if file_path:
                self.current_file = file_path
                self.update_status("🟡 等待需求文字", "#ffff00")
                self.task_list.addItem(f"📥 已接收：{Path(file_path).name}")
                
                # 【关键修改】删除自动读取剪贴板的逻辑！
                # 强制要求：必须重新复制一次，或者手动点按钮
                event.acceptProposedAction()

    def trigger_processing(self, file_path, text=None):
        self.worker = ParseWorker(file_path, text)
        self.worker.finished.connect(self.on_parse_finished)
        self.worker.start()

    def on_parse_finished(self, res, status_msg, status_code):
        if status_code == "failed":
            self.update_status(f"🔴 {status_msg}", "#ff3333")
            self.task_list.insertItem(0, f"❌ 失败：{status_msg}")
            # 失败后保留原文件，防止店长还要重新拖一次
            if not self.current_file:
                self.current_file = res.get("file_path", None) 
            return
        elif status_code == "ignored":
            self.update_status("🟢 等待下一单", "#00ff00")
            self.task_list.insertItem(0, f"⏭️ {status_msg}")
            return
        
        self.update_status("🟢 等待下一单", "#00ff00")
        item_text = f"✅ {res['file']}\n   ↳ {res['copies']}份 | {res['color']} | {res['sides']} | {res['source']}"
        self.task_list.insertItem(0, item_text)

    def update_status(self, text, color):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px;")

    def reset_drop_area(self):
        self.drop_area.setStyleSheet("""
            QLabel { color: #aaa; font-size: 13px; font-weight: bold;
                border: 2px dashed #555; border-radius: 10px; padding: 20px;
                background-color: rgba(40, 40, 45, 150); }
        """)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
            
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPos() - self.drag_position)
            event.accept()
            
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape: 
            self.hide()
            
    def closeEvent(self, event):
        if not self.is_quitting:
            event.ignore()
            self.hide()

if __name__ == '__main__':
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False) 
    window = FloatingWindow()
    window.show()
    sys.exit(app.exec_())