import sys
import re
import time
import logging
import pythoncom
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, 
    QPushButton, QMenu, QAction, QSystemTrayIcon, QGraphicsDropShadowEffect, QMessageBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor

# ==========================================
# 依赖导入与全局配置
# ==========================================
from watch_print import (
    Config, PrintLogDB, DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, 
    DEFAULT_VISION_MODEL, DEFAULT_DB_PATH, parse_filename_intent, analyze_print_intent
)
from print_engine import PrintEngine

# 【全局单例】整个应用生命周期只实例化一次打印引擎，避免重复探测环境。
global_print_engine = PrintEngine()

# 初始化全局配置对象
config = Config(
    watch_dir=Path(r"C:\打印接收"), ollama_host=DEFAULT_OLLAMA_HOST,
    ollama_model=DEFAULT_OLLAMA_MODEL, ollama_vision_model=DEFAULT_VISION_MODEL,
    ollama_timeout=60.0, ollama_retries=3, stable_seconds=0.8, stable_timeout=60.0,
    poll_interval=1.0, workers=2, include_existing=False,
    processed_ttl=86400.0, processed_max=20000, log_level="INFO", db_path=Path(DEFAULT_DB_PATH)
)

# 意图关键词正则：用于过滤剪贴板里的垃圾信息
INTENT_KEYWORDS = re.compile(r"(打印|彩|黑白|单面|双面|\d+份|[一二两三四五六七八九十]+份)")

# 时间窗口（秒）：如果剪贴板变动超过这个时间，就认为是历史脏数据，不再与拖拽的文件绑定
CLIPBOARD_TIMEOUT = 15.0


# ==========================================
# 核心类 1：后台工作线程 (防止 UI 卡死)
# ==========================================
class ParseWorker(QThread):
    """
    【作用】后台异步处理任务。
    【为什么用 QThread】解析图片、调用大模型、打印文件等操作极其耗时。
    如果在主线程直接跑，整个悬浮窗会变成“未响应”状态。必须丢给子线程处理。
    """
    # 自定义信号：参数分别为：结果字典, 提示信息, 状态码 (success/failed/ignored/fallback)
    finished = pyqtSignal(dict, str, str) 

    def __init__(self, file_path: str, requirement_text: str = None):
        super().__init__()
        self.file_path = file_path
        self.requirement_text = requirement_text 

    def run(self):
        """线程启动后实际执行的逻辑"""
        # 【核心修复】在子线程启动时初始化 COM 组件
        # 否则 print_engine 唤醒 WPS/Office 时会报 (-2147221008, '尚未调用 CoInitialize。') 错误
        pythoncom.CoInitialize()
        
        path = Path(self.file_path)
        db = None
        try:
            # 独立开启数据库连接（防止多线程共享连接报错）
            db = PrintLogDB(config.db_path)
            intent = None
            source = ""
            
            # 【优先级 1】文字需求优先：如果店长复制了文字，先伪装成文件名让正则去解析
            if self.requirement_text:
                fake_filename = f"req_{self.requirement_text}.txt"
                intent = parse_filename_intent(fake_filename)
                if intent:
                    source = "clipboard_text"
                else:
                    # 文字里没有明确参数，直接报错，绝不瞎猜
                    self.finished.emit({}, "复制文字未包含明确参数", "failed")
                    return
            
            # 【优先级 2】模型兜底：没有文字，或者正则没命中，降级去调用大模型看图/看文本
            if intent is None:
                intent, source = analyze_print_intent(path, config)
            
            if intent is None:
                self.finished.emit({}, "意图解析失败", "failed")
                return
            
            if not intent.need_print:
                db.log(path, intent=intent, source=source, status="ignored")
                self.finished.emit({}, "无需打印", "ignored")
                return

            # 【执行打印】调用全局打印引擎
            print_result = global_print_engine.execute(str(path))
            
            # 明确区分 failed 和 fallback 状态
            if print_result["status"] == "failed":
                db.log(path, intent=intent, source=source, status="failed", error=print_result["msg"])
                self.finished.emit({}, print_result["msg"], "failed")
                return
            elif print_result["status"] == "fallback":
                # 降级状态：打印机坏了但文件存桌面了，记录为 fallback
                db.log(path, intent=intent, source=source, status="fallback", error=print_result["msg"])
                res = {
                    "file": path.name, "copies": intent.copies, "color": intent.color, 
                    "sides": intent.sides, "source": source, "print_msg": print_result["msg"]
                }
                self.finished.emit(res, print_result["msg"], "fallback")
                return

            # 完全成功状态
            db.log(path, intent=intent, source=source, status="success")
            res = {
                "file": path.name, "copies": intent.copies, "color": intent.color, 
                "sides": intent.sides, "source": source, "print_msg": print_result["msg"]
            }
            self.finished.emit(res, "完成", "success")
            
        except Exception as e:
            self.finished.emit({}, f"系统异常: {str(e)}", "failed")
        finally:
            if db: db.close()
            # 【核心修复】线程结束前必须注销 COM，释放资源，防止内存泄漏
            pythoncom.CoUninitialize()


# ==========================================
# 核心类 2：悬浮窗主界面
# ==========================================
class FloatingWindow(QWidget):
    """
    【作用】构建整个桌面悬浮挂件的 UI，并处理拖拽、剪贴板监听等交互。
    """
    def __init__(self):
        super().__init__()
        self.current_file = None   
        self.is_quitting = False   
        
        # 维护一个 worker 集合，防止快速拖拽时旧线程被垃圾回收(GC)导致崩溃
        self._workers = set()      
        
        # 【核心属性】记录最后一次剪贴板发生变动的时间戳
        self.last_clipboard_time = 0.0
        
        self.initUI()
        self.init_clipboard()
        self.init_tray()

    def initUI(self):
        """【UI 初始化】搭建窗口骨架、样式表与各个控件"""
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
        shadow.setOffset(0, 0)
        self.main_widget.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self.main_widget)
        layout.setContentsMargins(15, 15, 15, 15)

        # 顶部状态栏
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

        # 拖拽接收区
        self.drop_area = QLabel("📥 请将文件拖拽至此\n然后去微信复制需求文字")
        self.drop_area.setAlignment(Qt.AlignCenter)
        self.reset_drop_area()
        layout.addWidget(self.drop_area)

        # 手动兜底读取按钮
        self.manual_btn = QPushButton("📋 手动读取剪贴板文字")
        self.manual_btn.setStyleSheet("""
            QPushButton { background-color: #333; color: white; border-radius: 5px; padding: 5px; font-size: 12px; }
            QPushButton:hover { background-color: #444; }
        """)
        self.manual_btn.clicked.connect(self.manual_read_clipboard)
        layout.addWidget(self.manual_btn)

        # 历史任务列表
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
        """【初始化剪贴板监听】一旦系统剪贴板发生改变，立刻触发"""
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

    def init_tray(self):
        """【初始化系统托盘】在右下角任务栏生成图标"""
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
        """【安全退出】遍历正在跑的任务，弹窗警告"""
        self.is_quitting = True
        
        # 检查集合中是否还有存活的线程
        running_workers = [w for w in self._workers if w.isRunning()]
        if running_workers:
            reply = QMessageBox.warning(
                self, "正在处理", "有任务正在识别或打印中。\n强行退出可能导致任务中断！\n确定退出吗？", 
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.No: 
                self.is_quitting = False
                return
            
            # 强制结束所有存活线程
            for w in running_workers:
                w.terminate()
                w.wait()
        
        self.tray_icon.hide()
        QApplication.quit()

    # ==========================================
    # 核心事件监听（剪贴板与拖拽）
    # ==========================================
    def on_clipboard_changed(self):
        """【自动监听】剪贴板变化触发，如果有文字且有文件，立刻绑定"""
        text = self.clipboard.text().strip()
        if not text: return

        # 【极其重要】只要剪贴板变动，立刻打上新鲜的时间戳
        self.last_clipboard_time = time.time()

        if INTENT_KEYWORDS.search(text) and self.current_file:
            self.trigger_processing(self.current_file, text)
            self.current_file = None 
            self.update_status("⚙️ 正在解析...", "#ffaa00")

    def manual_read_clipboard(self):
        """【手动兜底】防止被输入法拦截信号"""
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
        """【拖拽进入】让拖拽区变绿高亮"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_area.setStyleSheet("QLabel { color: #00ff00; border: 2px solid #00ff00; background-color: rgba(0, 50, 0, 100); border-radius: 10px; padding: 20px; }")

    def dragLeaveEvent(self, event):
        """【拖拽离开】恢复默认样式"""
        self.reset_drop_area()

    def dropEvent(self, event):
        """【拖拽释放】鼠标松手，真正拿到了文件路径"""
        self.reset_drop_area()
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            file_path = url.toLocalFile()
            if file_path:
                self.current_file = file_path
                
                # 【修复 P1-3 + 时间戳防呆】
                # 先检查剪贴板里有没有关键词，以及这个复制动作是否发生在 15 秒以内
                clip_text = self.clipboard.text().strip()
                current_time = time.time()
                
                if INTENT_KEYWORDS.search(clip_text) and (current_time - self.last_clipboard_time < CLIPBOARD_TIMEOUT):
                    self.task_list.addItem(f"📋 自动结合刚复制的需求：{clip_text}")
                    self.trigger_processing(file_path, clip_text)
                    self.current_file = None
                    self.update_status("⚙️ 正在解析...", "#ffaa00")
                else:
                    # 如果没有关键词，或者复制时间超过 15 秒（历史脏数据），统统走黄灯等待
                    self.update_status("🟡 等待需求文字", "#ffff00") 
                    self.task_list.addItem(f"📥 已接收：{Path(file_path).name}")
                
                event.acceptProposedAction()

    # ==========================================
    # 线程调度与 UI 更新
    # ==========================================
    def trigger_processing(self, file_path, text=None):
        """【启动后厨】把任务丢进集合，并启动它"""
        worker = ParseWorker(file_path, text)
        worker.finished.connect(self.on_parse_finished)
        # 完成后自动从集合中移除自己
        worker.finished.connect(lambda *_: self._workers.discard(worker))
        self._workers.add(worker)
        worker.start()

    def on_parse_finished(self, res, status_msg, status_code):
        """【回调主线程】子线程干完活了，通知 UI 更新"""
        if status_code == "failed":
            self.update_status(f"🔴 {status_msg}", "#ff3333")
            self.task_list.insertItem(0, f"❌ 失败：{status_msg}")
            return
        elif status_code == "ignored":
            self.update_status("🟢 等待下一单", "#00ff00")
            self.task_list.insertItem(0, f"⏭️ {status_msg}")
            return
        elif status_code == "fallback":
            self.update_status(f"🟡 {status_msg}", "#ffaa00")
            item_text = (
                f"⚠️ {res['file']}\n"
                f"   ↳ {res['copies']}份 | {res['color']} | {res['sides']} | {res['source']}\n"
                f"   ↳ 输出: {res['print_msg']}"
            )
            self.task_list.insertItem(0, item_text)
            return
        
        # 成功状态（绿灯）
        self.update_status("🟢 等待下一单", "#00ff00")
        item_text = (
            f"✅ {res['file']}\n"
            f"   ↳ {res['copies']}份 | {res['color']} | {res['sides']} | {res['source']}\n"
            f"   ↳ 输出: {res['print_msg']}"
        )
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

    # ==========================================
    # 窗口拖动与快捷键
    # ==========================================
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


# ==========================================
# 主程序入口
# ==========================================
if __name__ == '__main__':
    # 统一初始化日志，防止无控制台环境下报错
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False) 
    
    window = FloatingWindow()
    window.show()
    sys.exit(app.exec_())