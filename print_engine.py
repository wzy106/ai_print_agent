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

# 修复 P1-4：统一使用 logging，防止打包 exe 后 print 报错闪退
log = logging.getLogger("print_engine")

def _get_real_desktop_path() -> Path:
    """
    修复 P1-5：获取真实的桌面路径。
    考虑到用户可能启用了 OneDrive 桌面同步，通过读取 Windows 注册表来获取准确路径。
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
        # 兜底方案：如果读注册表失败，返回默认路径
        return Path(os.path.expanduser("~")) / "Desktop"

# ==========================================
# 核心类：打印引擎 (Print Engine)
# ==========================================
class PrintEngine:
    def __init__(self):
        # 1. 修复 P1-5：使用真实的桌面路径
        self.desktop_path = _get_real_desktop_path()
        
        # 2. 临时文件夹 (用于存放归一化后的中间 PDF)
        self.temp_dir = Path(os.environ.get("TEMP", "C:\\Temp")) / "AI_Print_Temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 3. 已知的虚拟打印机名单
        self.virtual_printer_keywords = ["PDF", "XPS", "OneNote", "Fax", "虚拟"]

        # 4. 尝试寻找 SumatraPDF 的安装路径 (模式 B 专用)
        self.sumatra_path = self._find_sumatra()

    # ==========================================
    # 第一层：环境探测 (Environment Probing)
    # ==========================================
    def _probe_physical_printer(self) -> str | None:
        """探测系统中是否有实体打印机。"""
        try:
            printers = win32print.EnumPrinters(2)
            for p in printers:
                printer_name = p[2]
                if not any(keyword.lower() in printer_name.lower() for keyword in self.virtual_printer_keywords):
                    log.info(f"[Engine] 探测到可用实体打印机: {printer_name}")
                    return printer_name
        except Exception as e:
            log.error(f"[Engine] 枚举打印机失败: {e}")
        return None

    def _find_sumatra(self) -> str | None:
        """探测 SumatraPDF 是否存在。"""
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
    # 第二层：统一归一化 (Normalization)
    # ==========================================
    def _img_to_pdf(self, img_path: str, out_pdf_path: str):
        """图片 -> PDF：用 Pillow 库直接转换"""
        log.info(f"[Normalize] 正在将图片转换为 PDF: {Path(img_path).name}")
        with Image.open(img_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out_pdf_path, "PDF", resolution=100.0)

    def _office_to_pdf(self, office_path: str, out_pdf_path: str):
        """
        Office -> PDF：用 win32com 后台静默唤醒 WPS/Office 另存为。
        修复 P0-1：针对 Word/Excel/PPT 分别使用正确的 COM API 和参数。
        """
        log.info(f"[Normalize] 正在将 Office 转换为 PDF: {Path(office_path).name}")
        ext = Path(office_path).suffix.lower()
        abs_in = os.path.abspath(office_path)
        abs_out = os.path.abspath(out_pdf_path)
        app = None

        try:
            if ext in [".docx", ".doc"]:
                app = win32com.client.Dispatch("Word.Application")
                app.Visible = False
                app.DisplayAlerts = False
                doc = app.Documents.Open(abs_in)
                # 17 = wdFormatPDF
                doc.SaveAs(abs_out, FileFormat=17)
                doc.Close(False)

            elif ext in [".xlsx", ".xls"]:
                app = win32com.client.Dispatch("Excel.Application")
                app.Visible = False
                app.DisplayAlerts = False
                wb = app.Workbooks.Open(abs_in)
                # 0 = xlTypePDF
                wb.ExportAsFixedFormat(0, abs_out)
                wb.Close(False)

            elif ext in [".pptx", ".ppt"]:
                app = win32com.client.Dispatch("PowerPoint.Application")
                # 坑：PPT 不允许 app.Visible = False，会直接抛异常
                # 必须用 WithWindow=False
                pres = app.Presentations.Open(abs_in, WithWindow=False)
                # 32 = ppSaveAsPDF
                pres.SaveAs(abs_out, 32)
                pres.Close()

            else:
                raise ValueError(f"不支持的 Office 格式: {ext}")

            log.info(f"[Normalize] Office 转 PDF 成功")
        except Exception as e:
            log.error(f"[Normalize] Office 转换失败: {e}")
            raise RuntimeError(f"Office 转换失败 ({ext}): {e}")
        finally:
            # 必须保证后台进程被彻底杀掉，防止僵尸进程占用文件
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass

    def _normalize(self, file_path: str) -> str:
        """【核心路由】把任何支持的文件，变成临时目录里的一个 PDF 文件。"""
        ext = Path(file_path).suffix.lower()
        
        # 修复 P0-3：使用 uuid 防止并发覆盖
        temp_pdf_path = str(self.temp_dir / f"temp_{uuid.uuid4().hex[:8]}.pdf")

        if ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
            self._img_to_pdf(file_path, temp_pdf_path)
        elif ext in [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"]:
            self._office_to_pdf(file_path, temp_pdf_path)
        elif ext == ".pdf":
            shutil.copy2(file_path, temp_pdf_path)
        else:
            raise ValueError(f"不支持的未知格式: {ext}")

        return temp_pdf_path

    # ==========================================
    # 第三层：双轨执行与降级兜底 (Execution & Fallback)
    # ==========================================
    def _fallback_to_desktop(self, temp_pdf: str, is_fallback: bool = False) -> str:
        """模式 A 动作：把 PDF 复制到桌面，带时间戳"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        desktop_pdf = str(self.desktop_path / f"print_{timestamp}.pdf")
        shutil.copy2(temp_pdf, desktop_pdf)
        
        if is_fallback:
            log.warning(f"[Fallback] ⚠️ 打印失败，已触发降级！文件保存至桌面: {desktop_pdf}")
        else:
            log.info(f"[Mode A] ✅ 模拟打印完成，文件已保存至桌面: {desktop_pdf}")
        return desktop_pdf

    def execute(self, file_path: str) -> dict:
        """【主入口】外部业务层只调用这个方法。"""
        log.info(f"========== 开始处理: {Path(file_path).name} ==========")
        temp_pdf = None

        try:
            # 1. 归一化
            temp_pdf = self._normalize(file_path)

            # 2. 环境探测
            physical_printer = self._probe_physical_printer()

            # 3. 路径分轨
            if not physical_printer or not self.sumatra_path:
                if physical_printer and not self.sumatra_path:
                    log.warning(f"[Engine] 发现实体机，但未安装 SumatraPDF，无法静默打印，降级到模式 A。")
                
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=False)
                return {"status": "success", "mode": "A", "path": final_path, "msg": "已降级保存到桌面"}

            # 进入模式 B
            try:
                log.info(f"[Mode B] 正在发送 PDF 到实体打印机 ({physical_printer})...")
                cmd = [
                    self.sumatra_path,
                    "-print-to", physical_printer,
                    "-silent",
                    temp_pdf
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode != 0:
                    raise RuntimeError(f"SumatraPDF 返回错误码: {result.returncode}, 信息: {result.stderr}")

                log.info(f"[Mode B] ✅ 成功发送到打印机！")
                return {"status": "success", "mode": "B", "path": None, "msg": "已发送至打印机出纸"}

            except Exception as print_error:
                log.error(f"[Mode B Error] 打印失败: {print_error}")
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=True)
                return {"status": "fallback", "mode": "B->A", "path": final_path, "msg": "打印机异常，已保存到桌面"}

        except Exception as e:
            log.error(f"[Error] 处理严重失败: {e}")
            return {"status": "failed", "msg": str(e)}
            
        finally:
            # 4. 清理临时文件
            if temp_pdf and os.path.exists(temp_pdf):
                try:
                    os.remove(temp_pdf)
                    log.info(f"[Cleanup] 临时文件已清理")
                except OSError:
                    pass