import sys
import re
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
# 引入核心解析逻辑、数据库与配置
from watch_print import (
    Config, PrintLogDB, DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, 
    DEFAULT_VISION_MODEL, DEFAULT_DB_PATH, parse_filename_intent
)
# 引入刚封装好的真实打印引擎
from print_engine import PrintEngine

# 初始化全局配置对象（复用 watch_print.py 里的数据结构）
config = Config(
    watch_dir=Path(r"C:\打印接收"), ollama_host=DEFAULT_OLLAMA_HOST,
    ollama_model=DEFAULT_OLLAMA_MODEL, ollama_vision_model=DEFAULT_VISION_MODEL,
    ollama_timeout=60.0, ollama_retries=3, stable_seconds=0.8, stable_timeout=60.0,
    poll_interval=1.0, workers=2, include_existing=False,
    processed_ttl=86400.0, processed_max=20000, log_level="INFO", db_path=Path(DEFAULT_DB_PATH)
)

# 意图关键词正则：用于过滤剪贴板里的垃圾信息，只有包含这些词才认为是“打印需求”
INTENT_KEYWORDS = re.compile(r"(打印|彩|黑白|单面|双面|\d+份|[一二两三四五六七八九十]+份)")


# ==========================================
# 核心类 1：后台工作线程 (防止 UI 卡死)
# ==========================================
class ParseWorker(QThread):
    """
    【作用】后台异步处理任务。
    【为什么用 QThread】解析图片、调用大模型、打印文件等操作极其耗时（几秒到几十秒）。
    如果在主线程（UI线程）直接跑，整个悬浮窗会变成“未响应”状态。必须丢给子线程处理。
    """
    # 自定义信号：处理完成后，通知主线程更新 UI。参数分别为：结果字典, 提示信息, 状态码
    finished = pyqtSignal(dict, str, str) 

    def __init__(self, file_path: str, requirement_text: str = None):
        super().__init__()
        self.file_path = file_path
        self.requirement_text = requirement_text # 店长复制的文字需求（可能为空）

    def run(self):
        """线程启动后实际执行的逻辑（绝不能在外部直接调用这个函数）"""
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
                    # 文字里没有明确参数（比如只复制了“帮我搞一下”），直接报错，绝不瞎猜
                    self.finished.emit({}, "复制文字未包含明确参数", "failed")
                    return
            
            # 【优先级 2】模型兜底：没有文字，或者正则没命中，只能降级去调用大模型看图/看文本
            if intent is None:
                from watch_print import analyze_print_intent
                intent, source = analyze_print_intent(path, config)
            
            if intent is None:
                self.finished.emit({}, "意图解析失败", "failed")
                return
            
            if not intent.need_print:
                db.log(path, intent=intent, source=source, status="ignored")
                self.finished.emit({}, "无需打印", "ignored")
                return

            # 【执行打印】调用打印引擎（内部自带无实体机降级、无弹窗打印等逻辑）
            engine = PrintEngine()
            print_result = engine.execute(str(path))
            
            if print_result["status"] == "failed":
                db.log(path, intent=intent, source=source, status="failed", error=print_result["msg"])
                self.finished.emit({}, print_result["msg"], "failed")
                return

            # 写入成功日志
            db.log(path, intent=intent, source=source, status="success")
            
            # 打包结果，准备发回主线程更新界面
            res = {
                "file": path.name, 
                "copies": intent.copies,
                "color": intent.color, 
                "sides": intent.sides, 
                "source": source,
                "print_msg": print_result["msg"] 
            }
            self.finished.emit(res, "完成", "success")
            
        except Exception as e:
            self.finished.emit({}, f"系统异常: {str(e)}", "failed")
        finally:
            if db: db.close()


# ==========================================
# 核心类 2：悬浮窗主界面
# ==========================================
class FloatingWindow(QWidget):
    """
    【作用】构建整个桌面悬浮挂件的 UI，并处理拖拽、剪贴板监听等交互。
    """
    def __init__(self):
        super().__init__()
        self.current_file = None   # 暂存店长刚拖进来的文件路径
        self.is_quitting = False   # 标记是否执行“彻底退出”（防止误杀正在打印的任务）
        self.worker = None         # 持有线程对象的引用，防止被 Python 垃圾回收器清掉
        self.initUI()
        self.init_clipboard()
        self.init_tray()

    def initUI(self):
        """【UI 初始化】搭建窗口骨架、样式表与各个控件"""
        # 1. 设置无边框、置顶工具窗口
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 2. 开启背景透明
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(340, 480)
        self.setFocusPolicy(Qt.StrongFocus) # 必须设置，否则键盘 Esc 键监听不到

        # 3. 主容器（留出边距给阴影效果）
        self.main_widget = QWidget(self)
        self.main_widget.setGeometry(10, 10, 320, 460)
        self.main_widget.setStyleSheet("QWidget { background-color: rgba(25, 25, 30, 240); border-radius: 15px; }")
        
        # 4. 给主容器添加一圈漂亮的阴影特效
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 0)
        self.main_widget.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self.main_widget)
        layout.setContentsMargins(15, 15, 15, 15)

        # 5. 顶部状态栏（信号灯 + 关闭按钮）
        top_layout = QHBoxLayout()
        self.status_label = QLabel("🟢 等待文件")
        self.status_label.setStyleSheet("color: #00ff00; font-weight: bold; font-size: 14px;")
        
        self.close_btn = QPushButton("✖")
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setStyleSheet("QPushButton { color: #888; font-weight: bold; border: none; } QPushButton:hover { color: white; }")
        self.close_btn.clicked.connect(self.hide) # 点击 X 只是隐藏到托盘，不杀进程
        
        top_layout.addWidget(self.status_label)
        top_layout.addStretch()
        top_layout.addWidget(self.close_btn)
        layout.addLayout(top_layout)

        # 6. 拖拽接收区
        self.drop_area = QLabel("📥 请将文件拖拽至此\n然后去微信复制需求文字")
        self.drop_area.setAlignment(Qt.AlignCenter)
        self.reset_drop_area()
        layout.addWidget(self.drop_area)

        # 7. 手动兜底读取按钮（防止剪贴板监听信号被系统拦截）
        self.manual_btn = QPushButton("📋 手动读取剪贴板文字")
        self.manual_btn.setStyleSheet("""
            QPushButton { background-color: #333; color: white; border-radius: 5px; padding: 5px; font-size: 12px; }
            QPushButton:hover { background-color: #444; }
        """)
        self.manual_btn.clicked.connect(self.manual_read_clipboard)
        layout.addWidget(self.manual_btn)

        # 8. 历史任务列表（展示记录，取代以前的滚动日志）
        self.task_list = QListWidget()
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff) # 关闭横向滚动条
        self.task_list.setWordWrap(True) # 允许自动换行
        self.task_list.setStyleSheet("""
            QListWidget { background-color: transparent; border: none; color: #ddd; font-family: Consolas; font-size: 12px; }
            QListWidget::item { border-bottom: 1px solid #333; padding: 8px 0px; }
            QScrollBar:vertical { background: transparent; width: 6px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,50); min-height: 20px; border-radius: 3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        layout.addWidget(self.task_list)

        self.setAcceptDrops(True) # 开启拖拽接收
        self.drag_position = None # 用于记录窗口拖动坐标

    def init_clipboard(self):
        """【初始化剪贴板监听】一旦系统剪贴板发生改变，立刻触发 on_clipboard_changed"""
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

    def init_tray(self):
        """【初始化系统托盘】在右下角任务栏生成一个图标，右键可退出"""
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        self.tray_icon.setToolTip("AI 打印管家")
        
        tray_menu = QMenu()
        show_action = QAction("显示面板", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("彻底退出", self)
        quit_action.triggered.connect(self.safe_quit) # 绑定安全退出逻辑
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def safe_quit(self):
        """【安全退出】如果有任务正在跑，必须弹窗警告，防止“半截打印”或数据损坏"""
        self.is_quitting = True
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.warning(
                self, "正在处理", "有任务正在识别或打印中。\n强行退出可能导致任务中断！\n确定退出吗？", 
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.No: 
                self.is_quitting = False
                return
            # 强制结束线程（极度危险，相当于拔电源）
            self.worker.terminate()
            self.worker.wait()
        
        self.tray_icon.hide()
        QApplication.quit()

    # ==========================================
    # 核心事件监听（剪贴板与拖拽）
    # ==========================================
    def on_clipboard_changed(self):
        """【核心交互】剪贴板变化触发。如果是文字需求，且手里有文件，立刻绑定解析"""
        text = self.clipboard.text().strip()
        if not text: return

        # 只有包含关键词（打印、彩、份），且之前拖入了文件，才触发
        if INTENT_KEYWORDS.search(text) and self.current_file:
            self.trigger_processing(self.current_file, text)
            self.current_file = None # 消耗掉文件，防止下次复制又触发一遍
            self.update_status("⚙️ 正在解析...", "#ffaa00")

    def manual_read_clipboard(self):
        """【兜底交互】如果自动监听失效（被其他软件拦截），手动点击按钮强制读取"""
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

    # ==========================================
    # 拖拽事件处理逻辑
    # ==========================================
    def dragEnterEvent(self, event):
        """【拖拽进入】鼠标拖着文件刚进入悬浮窗边界时触发（让拖拽区变绿高亮）"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_area.setStyleSheet("QLabel { color: #00ff00; border: 2px solid #00ff00; background-color: rgba(0, 50, 0, 100); border-radius: 10px; padding: 20px; }")

    def dragLeaveEvent(self, event):
        """【拖拽离开】鼠标带着文件离开悬浮窗，恢复默认样式"""
        self.reset_drop_area()

    def dropEvent(self, event):
        """【拖拽释放】鼠标松手，真正拿到了文件路径"""
        self.reset_drop_area()
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            file_path = url.toLocalFile()
            if file_path:
                self.current_file = file_path
                self.update_status("🟡 等待需求文字", "#ffff00") # 黄灯告警，等待后续操作
                self.task_list.addItem(f"📥 已接收：{Path(file_path).name}")
                event.acceptProposedAction()

    # ==========================================
    # 线程调度与 UI 更新
    # ==========================================
    def trigger_processing(self, file_path, text=None):
        """【启动后厨】实例化后台线程并启动它"""
        self.worker = ParseWorker(file_path, text)
        self.worker.finished.connect(self.on_parse_finished)
        self.worker.start()

    def on_parse_finished(self, res, status_msg, status_code):
        """【回调主线程】子线程干完活了，通知 UI 更新颜色和列表"""
        if status_code == "failed":
            self.update_status(f"🔴 {status_msg}", "#ff3333")
            self.task_list.insertItem(0, f"❌ 失败：{status_msg}")
            return
        elif status_code == "ignored":
            self.update_status("🟢 等待下一单", "#00ff00")
            self.task_list.insertItem(0, f"⏭️ {status_msg}")
            return
        
        # 成功状态（绿灯）
        self.update_status("🟢 等待下一单", "#00ff00")
        item_text = (
            f"✅ {res['file']}\n"
            f"   ↳ {res['copies']}份 | {res['color']} | {res['sides']} | {res['source']}\n"
            f"   ↳ 输出: {res['print_msg']}"
        )
        self.task_list.insertItem(0, item_text) # 新记录插在最前面

    def update_status(self, text, color):
        """【工具方法】更新左上角状态灯的文字和颜色"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px;")

    def reset_drop_area(self):
        """【工具方法】恢复拖拽区的默认灰色虚线样式"""
        self.drop_area.setStyleSheet("""
            QLabel { color: #aaa; font-size: 13px; font-weight: bold;
                border: 2px dashed #555; border-radius: 10px; padding: 20px;
                background-color: rgba(40, 40, 45, 150); }
        """)

    # ==========================================
    # 窗口拖动与快捷键
    # ==========================================
    def mousePressEvent(self, event):
        """记录鼠标点击时的相对窗口位置，为移动窗口做准备"""
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
            
    def mouseMoveEvent(self, event):
        """按住左键移动时，利用相对坐标动态移动整个窗口"""
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPos() - self.drag_position)
            event.accept()
            
    def keyPressEvent(self, event):
        """按 Esc 键时隐藏窗口到托盘（不直接关闭）"""
        if event.key() == Qt.Key_Escape: 
            self.hide()
            
    def closeEvent(self, event):
        """拦截系统的 Alt+F4 等关闭指令，改为隐藏到托盘"""
        if not self.is_quitting:
            event.ignore()
            self.hide()


# ==========================================
# 主程序入口
# ==========================================
if __name__ == '__main__':
    # 兼容 Win10/Win11 各种分辨率和缩放
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    
    app = QApplication(sys.argv)
    
    # 关键设置：主窗口全关闭时，程序不退出（因为还有托盘图标在运行）
    app.setQuitOnLastWindowClosed(False) 
    
    window = FloatingWindow()
    window.show()
    sys.exit(app.exec_())