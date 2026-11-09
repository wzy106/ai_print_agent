import os
import uuid
import shutil
import logging
import subprocess
import win32print
import win32com.client
from pathlib import Path
from datetime import datetime
from PIL import Image

# ==========================================
# 日志与全局配置
# ==========================================
log = logging.getLogger("print_engine")

def _get_real_desktop_path() -> Path:
    """
    【作用】获取真实的桌面路径。
    【为什么】Windows 如果开启了 OneDrive 同步，桌面路径会变成 C:\Users\xxx\OneDrive\Desktop。
    直接写死 ~/Desktop 会导致文件复制失败，必须查注册表。
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(value))
    except Exception:
        # 兜底方案：读注册表失败则返回默认路径
        return Path(os.path.expanduser("~")) / "Desktop"


class PrintEngine:
    """
    【核心类】双轨制打印引擎。
    核心职责：统一格式转 PDF -> 环境探测 -> 模式 A（降级存桌面）/ 模式 B（真实打印）。
    """
    def __init__(self):
        # 1. 初始化真实桌面路径
        self.desktop_path = _get_real_desktop_path()
        
        # 2. 初始化临时文件夹（存放转换后的中间 PDF）
        self.temp_dir = Path(os.environ.get("TEMP", "C:\\Temp")) / "AI_Print_Temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 3. 虚拟打印机黑名单（用于区分实体机和虚拟打印机）
        self.virtual_printer_keywords = ["PDF", "XPS", "OneNote", "Fax", "虚拟"]

        # 4. 寻找 SumatraPDF 的安装路径（模式 B 静默打印的必备工具）
        self.sumatra_path = self._find_sumatra()

    # ==========================================
    # 第一层：环境探测
    # ==========================================
    def _probe_physical_printer(self) -> str | None:
        """【探测实体打印机】枚举系统打印机，过滤掉黑名单里的虚拟打印机，返回第一个实体机名字。"""
        try:
            printers = win32print.EnumPrinters(2)
            for p in printers:
                printer_name = p[2]
                # 如果名字里不包含虚拟打印机的关键字，认为是实体机
                if not any(keyword.lower() in printer_name.lower() for keyword in self.virtual_printer_keywords):
                    log.info(f"[Engine] 探测到可用实体打印机: {printer_name}")
                    return printer_name
        except Exception as e:
            log.error(f"[Engine] 枚举打印机失败: {e}")
        return None

    def _find_sumatra(self) -> str | None:
        """【寻找 SumatraPDF】在常见的安装路径下寻找这个极简的 PDF 阅读器（支持静默打印）。"""
        common_paths = [
            r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
            r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe")
        ]
        for path in common_paths:
            if os.path.exists(path):
                return path
        return None

    # ==========================================
    # 第二层：统一格式归一化（全部转为 PDF）
    # ==========================================
    def _img_to_pdf(self, img_path: str, out_pdf_path: str):
        """【图片转 PDF】利用 Pillow 库，将图片静默渲染为 PDF 格式。"""
        log.info(f"[Normalize] 正在将图片转换为 PDF: {Path(img_path).name}")
        with Image.open(img_path) as img:
            # PDF 不支持透明通道（RGBA），必须转为 RGB
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out_pdf_path, "PDF", resolution=100.0)

    def _office_to_pdf(self, office_path: str, out_pdf_path: str):
        """
        【Office 转 PDF】利用 win32com 无头唤醒 WPS/Office 进行格式转换。
        针对 Word、Excel、PPT 分别使用正确的底层 API。
        """
        log.info(f"[Normalize] 正在将 Office 转换为 PDF: {Path(office_path).name}")
        ext = Path(office_path).suffix.lower()
        abs_in = os.path.abspath(office_path)
        abs_out = os.path.abspath(out_pdf_path)
        app = None

        try:
            if ext in [".docx", ".doc"]:
                # Word 专属接口
                app = win32com.client.Dispatch("Word.Application")
                app.Visible = False
                app.DisplayAlerts = False
                doc = app.Documents.Open(abs_in)
                doc.SaveAs(abs_out, FileFormat=17) # 17 代表 PDF 格式
                doc.Close(False)

            elif ext in [".xlsx", ".xls"]:
                # Excel 专属接口
                app = win32com.client.Dispatch("Excel.Application")
                app.Visible = False
                app.DisplayAlerts = False
                wb = app.Workbooks.Open(abs_in)
                
                # 【排版优化】自动将所有列缩放到一页宽，防止打印出来被切断
                sheet = wb.ActiveSheet
                sheet.PageSetup.Zoom = False
                sheet.PageSetup.FitToPagesWide = 1
                sheet.PageSetup.FitToPagesTall = False
                
                wb.ExportAsFixedFormat(0, abs_out) # 0 代表 PDF 格式
                wb.Close(False)

            elif ext in [".pptx", ".ppt"]:
                # PPT 专属接口
                app = win32com.client.Dispatch("PowerPoint.Application")
                # PPT 坑爹点：不能用 Visible=False，必须用 WithWindow=False
                pres = app.Presentations.Open(abs_in, WithWindow=False)
                pres.SaveAs(abs_out, 32) # 32 代表 PDF 格式
                pres.Close()

            else:
                raise ValueError(f"不支持的 Office 格式: {ext}")

            log.info(f"[Normalize] Office 转 PDF 成功")
        except Exception as e:
            log.error(f"[Normalize] Office 转换失败: {e}")
            raise RuntimeError(f"Office 转换失败 ({ext}): {e}")
        finally:
            # 必须确保后台进程被彻底杀掉，防止僵尸进程占用文件或内存
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass

    def _normalize(self, file_path: str) -> str:
        """【统一路由】将所有支持的文件，变成临时目录里的一个 PDF 文件。"""
        ext = Path(file_path).suffix.lower()
        
        # 使用 uuid 生成临时文件名，防止多线程同一秒处理时产生覆盖冲突
        temp_pdf_path = str(self.temp_dir / f"temp_{uuid.uuid4().hex[:8]}.pdf")

        if ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
            self._img_to_pdf(file_path, temp_pdf_path)
        elif ext in [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"]:
            self._office_to_pdf(file_path, temp_pdf_path)
        elif ext == ".pdf":
            # PDF 本身就是最终格式，直接复制透传
            shutil.copy2(file_path, temp_pdf_path)
        else:
            raise ValueError(f"不支持的未知格式: {ext}")

        return temp_pdf_path

    # ==========================================
    # 第三层：双轨执行与降级兜底
    # ==========================================
    def _fallback_to_desktop(self, temp_pdf: str, is_fallback: bool = False) -> str:
        """【模式 A】将 PDF 复制到桌面保存，文件名带时间戳。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        desktop_pdf = str(self.desktop_path / f"print_{timestamp}.pdf")
        shutil.copy2(temp_pdf, desktop_pdf)
        
        if is_fallback:
            log.warning(f"[Fallback] ⚠️ 真实打印失败，已触发降级保存至桌面: {desktop_pdf}")
        else:
            log.info(f"[Mode A] ✅ 无实体打印机，已降级保存至桌面: {desktop_pdf}")
        return desktop_pdf

    def execute(self, file_path: str, intent=None) -> dict:
        """
        【核心主入口】由外部的 ParseWorker 线程调用。
        :param file_path: 待打印/处理的文件路径
        :param intent: 解析出的打印意图对象 (包含 copies, color, sides)
        :return: dict 包含状态 (status)、模式 (mode)、路径 (path) 和提示信息 (msg)
        """
        log.info(f"========== 开始处理: {Path(file_path).name} ==========")
        temp_pdf = None

        try:
            # 1. 格式归一化（万物皆可转 PDF）
            temp_pdf = self._normalize(file_path)

            # 2. 探测实体机
            physical_printer = self._probe_physical_printer()

            # 3. 策略分轨：如果没有实体机，或者没装 SumatraPDF，直接走模式 A
            if not physical_printer or not self.sumatra_path:
                if physical_printer and not self.sumatra_path:
                    log.warning(f"[Engine] 发现实体机，但未安装 SumatraPDF，无法静默打印，降级到模式 A。")
                
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=False)
                return {"status": "success", "mode": "A", "path": final_path, "msg": "已降级保存到桌面"}

            # 4. 模式 B：真实静默打印
            try:
                log.info(f"[Mode B] 正在发送 PDF 到实体打印机 ({physical_printer})...")
                
                # 【核心修改】解析 intent 中的参数，拼装 SumatraPDF 支持的打印指令
                if intent:
                    copies = intent.copies
                    # 颜色：SumatraPDF 识别的参数是 color 或 monochrome
                    color_arg = "color" if intent.color == "color" else "monochrome"
                    # 单双面：SumatraPDF 识别的参数是 duplex(双面) 或 simplex(单面)
                    sides_arg = "duplex" if intent.sides == "double" else "simplex"
                    
                    # 组合参数，例如："2x, color, duplex"
                    print_settings = f"{copies}x, {color_arg}, {sides_arg}"
                else:
                    # 如果没有传 intent，给一个默认的安全参数
                    print_settings = "1x, monochrome, simplex"

                cmd = [
                    self.sumatra_path,
                    "-print-to", physical_printer,
                    "-print-settings", print_settings,  # 将份数、颜色、单双面下发给底层
                    "-silent",
                    temp_pdf
                ]
                
                # 调用命令行执行打印，设置 30 秒超时，防止打印机卡死导致程序挂起
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode != 0:
                    raise RuntimeError(f"SumatraPDF 返回错误码: {result.returncode}, 信息: {result.stderr}")

                log.info(f"[Mode B] ✅ 成功发送到打印机出纸！")
                return {"status": "success", "mode": "B", "path": None, "msg": "已发送至打印机出纸"}

            except Exception as print_error:
                # 如果模式 B 报错（如卡纸、离线、硬件不支持），立即触发降级兜底
                log.error(f"[Mode B Error] 打印失败: {print_error}")
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=True)
                return {"status": "fallback", "mode": "B->A", "path": final_path, "msg": "打印机异常，已降级保存到桌面"}

        except Exception as e:
            log.error(f"[Error] 处理严重失败: {e}")
            return {"status": "failed", "msg": str(e)}
            
        finally:
            # 5. 无论成功、失败还是降级，最后必须清理掉临时文件
            if temp_pdf and os.path.exists(temp_pdf):
                try:
                    os.remove(temp_pdf)
                    log.info(f"[Cleanup] 临时文件已清理")
                except OSError:
                    pass