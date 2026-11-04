import os
import sys
import time
import shutil
import subprocess
import win32print
import win32com.client
from pathlib import Path
from datetime import datetime
from PIL import Image

# ==========================================
# 核心类：打印引擎 (Print Engine)
# ==========================================
class PrintEngine:
    def __init__(self):
        # 1. 获取桌面路径 (用于模式 A 的降级输出)
        self.desktop_path = Path(os.path.expanduser("~")) / "Desktop"
        
        # 2. 临时文件夹 (用于存放归一化后的中间 PDF)
        self.temp_dir = Path(os.environ.get("TEMP", "C:\\Temp")) / "AI_Print_Temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 3. 已知的虚拟打印机名单 (黑名单机制，防止把 PDF 虚拟打印机当成实体机)
        self.virtual_printer_keywords = ["PDF", "XPS", "OneNote", "Fax", "虚拟"]

        # 4. 尝试寻找 SumatraPDF 的安装路径 (模式 B 专用)
        self.sumatra_path = self._find_sumatra()

    # ==========================================
    # 第一层：环境探测 (Environment Probing)
    # ==========================================
    def _probe_physical_printer(self) -> str | None:
        """
        探测系统中是否有实体打印机。
        策略：枚举所有打印机 -> 过滤掉黑名单内的虚拟打印机 -> 返回第一个可用实体机名。
        """
        try:
            # EnumPrinters(2) 获取本地打印机列表
            printers = win32print.EnumPrinters(2)
            for p in printers:
                printer_name = p[2]
                # 如果名字里不包含虚拟打印机的关键字，认为是实体机
                if not any(keyword.lower() in printer_name.lower() for keyword in self.virtual_printer_keywords):
                    print(f"[Engine] 探测到可用实体打印机: {printer_name}")
                    return printer_name
        except Exception as e:
            print(f"[Engine] 枚举打印机失败: {e}")
        return None

    def _find_sumatra(self) -> str | None:
        """
        探测 SumatraPDF 是否存在。这是模式 B (静默打纸) 的必备工具。
        """
        # 常见安装路径
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
        print(f"[Normalize] 正在将图片转换为 PDF: {Path(img_path).name}")
        with Image.open(img_path) as img:
            # PDF 不支持 RGBA，必须转为 RGB
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out_pdf_path, "PDF", resolution=100.0)

    def _office_to_pdf(self, office_path: str, out_pdf_path: str):
        """Office -> PDF：用 win32com 后台静默唤醒 WPS/Office 另存为"""
        print(f"[Normalize] 正在将 Office 转换为 PDF: {Path(office_path).name}")
        ext = Path(office_path).suffix.lower()
        
        # 根据后缀选择不同的 COM 应用
        if ext in [".docx", ".doc"]:
            app_name = "Word.Application"
        elif ext in [".xlsx", ".xls"]:
            app_name = "Excel.Application"
        elif ext in [".pptx", ".ppt"]:
            app_name = "PowerPoint.Application"
        else:
            raise ValueError(f"不支持的 Office 格式: {ext}")

        try:
            # 启动后台应用 (不显示界面)
            app = win32com.client.Dispatch(app_name)
            app.Visible = False
            app.DisplayAlerts = False # 禁止一切弹窗

            # 打开文件并另存为 PDF (17 是 Office 中代表 PDF 的常量)
            doc = app.Documents.Open(os.path.abspath(office_path))
            doc.SaveAs(os.path.abspath(out_pdf_path), FileFormat=17)
            
            doc.Close(False)
            app.Quit()
            print(f"[Normalize] Office 转 PDF 成功")
        except Exception as e:
            # 如果失败，强制杀掉后台进程，防止僵尸进程卡死系统
            try:
                app.Quit()
            except: pass
            raise RuntimeError(f"Office 转换失败: {e}")

    def _normalize(self, file_path: str) -> str:
        """
        【核心路由】把任何支持的文件，变成临时目录里的一个 PDF 文件。
        返回：临时 PDF 的绝对路径。
        """
        ext = Path(file_path).suffix.lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_pdf_path = str(self.temp_dir / f"temp_{timestamp}.pdf")

        if ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
            self._img_to_pdf(file_path, temp_pdf_path)
        elif ext in [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"]:
            self._office_to_pdf(file_path, temp_pdf_path)
        elif ext == ".pdf":
            # PDF 直接透传
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
            print(f"[Fallback] ⚠️ 打印失败，已触发降级！文件安全保存至桌面: {desktop_pdf}")
        else:
            print(f"[Mode A] ✅ 模拟打印完成，文件已保存至桌面: {desktop_pdf}")
        return desktop_pdf

    def execute(self, file_path: str) -> dict:
        """
        【主入口】外部业务层（悬浮窗）只调用这个方法。
        """
        print(f"\n========== 开始处理: {Path(file_path).name} ==========")
        temp_pdf = None

        try:
            # 1. 归一化 (不管是啥，先转成临时 PDF)
            temp_pdf = self._normalize(file_path)

            # 2. 环境探测
            physical_printer = self._probe_physical_printer()

            # 3. 路径分轨
            if not physical_printer or not self.sumatra_path:
                # 进入模式 A（无实体机，或没装 SumatraPDF）
                if physical_printer and not self.sumatra_path:
                    print(f"[Engine] 发现实体机，但未安装 SumatraPDF，无法静默打印。")
                
                # 复制到桌面并返回结果
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=False)
                return {"status": "success", "mode": "A", "path": final_path, "msg": "已降级保存到桌面"}

            # 进入模式 B（真实静默打印）
            try:
                print(f"[Mode B] 正在发送 PDF 到实体打印机 ({physical_printer})...")
                # 使用 SumatraPDF 命令行静默打印
                # 参数说明: -print-to 指定打印机, -silent 静默不弹窗, -exit-when-done 打完退出
                cmd = [
                    self.sumatra_path,
                    "-print-to", physical_printer,
                    "-silent",
                    temp_pdf
                ]
                # 调用命令行并等待完成 (设置 30 秒超时熔断)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode != 0:
                    raise RuntimeError(f"SumatraPDF 返回错误码: {result.returncode}, 信息: {result.stderr}")

                print(f"[Mode B] ✅ 成功发送到打印机！")
                return {"status": "success", "mode": "B", "path": None, "msg": "已发送至打印机出纸"}

            except Exception as print_error:
                # 【终极兜底】打印机坏了/离线/卡纸，强制降级到桌面
                print(f"[Mode B Error] 打印失败: {print_error}")
                final_path = self._fallback_to_desktop(temp_pdf, is_fallback=True)
                return {"status": "fallback", "mode": "B->A", "path": final_path, "msg": "打印机异常，已保存到桌面"}

        except Exception as e:
            print(f"[Error] 处理严重失败: {e}")
            return {"status": "failed", "msg": str(e)}
            
        finally:
            # 4. 清理临时文件
            if temp_pdf and os.path.exists(temp_pdf):
                try:
                    os.remove(temp_pdf)
                    print(f"[Cleanup] 临时文件已清理")
                except OSError:
                    pass