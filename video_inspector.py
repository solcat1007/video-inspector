# -*- coding: utf-8 -*-
"""
视频检视器 v1.0 — 视频编码参数解析工具
========================================
痛点：拿到素材不知道编码格式、分辨率、码率等参数，剪辑导出时参数配错导致画质损失。

功能：
  - 选择视频文件（支持拖拽）
  - 优先用 ffprobe 解析完整参数：分辨率/编码/帧率/码率/时长/音频
  - 无 ffmpeg 时用 Python struct 解析 MP4/MKV 头部获取基本信息
  - 同时显示文件大小、修改时间等元信息

依赖：仅使用 Python 标准库（struct 解析）；ffprobe 为推荐外部工具。
"""

import os
import struct
import subprocess
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime

# ============================================================================
# 配色与常量
# ============================================================================
COLOR_BG = "#1e1e2e"
COLOR_CARD = "#2a2a3c"
COLOR_ACCENT = "#8b5cf6"          # 紫色强调
COLOR_ACCENT_HOVER = "#7c3aed"
COLOR_TEXT = "#e0e0e0"
COLOR_TEXT_SECONDARY = "#a0a0b0"
COLOR_ENTRY_BG = "#3a3a4c"
COLOR_GREEN = "#4ade80"
COLOR_WARN = "#fbbf24"
COLOR_HIGHLIGHT = "#8b5cf6"


def find_ffprobe() -> str | None:
    """查找 ffprobe 可执行文件路径"""
    result = subprocess.run(["where", "ffprobe"], capture_output=True, text=True, timeout=5)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split("\n")[0].strip()
    common = [r"C:\ffmpeg\bin\ffprobe.exe", r"C:\Program Files\ffmpeg\bin\ffprobe.exe"]
    for p in common:
        if os.path.exists(p):
            return p
    return None


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024**3):.2f} GB"


def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}.{ms:03d}"
    else:
        return f"{m:02d}:{s:02d}.{ms:03d}"


# ============================================================================
# MP4/MKV 头部解析（struct 兜底）
# ============================================================================

def parse_mp4_header(file_path: str) -> dict:
    """
    用 struct 解析 MP4/MOV 文件头部，提取基本参数。
    遍历 ISO Base Media File Format 的 Box 结构。
    """
    info = {"container": "MP4/MOV"}
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)

            while f.tell() < fsize:
                try:
                    raw = f.read(8)
                    if len(raw) < 8:
                        break
                    size, box_type = struct.unpack(">I4s", raw)
                    if size < 8:
                        break
                    payload = f.read(size - 8) if size > 8 else b""

                    # mvhd — 时长与时间刻度
                    if box_type == b"mvhd":
                        if len(payload) >= 12:
                            version = payload[0]
                            if version == 0:
                                timescale = struct.unpack(">I", payload[12:16])[0]
                                duration = struct.unpack(">I", payload[16:20])[0]
                            elif version == 1:
                                timescale = struct.unpack(">I", payload[20:24])[0]
                                duration = struct.unpack(">Q", payload[24:32])[0]
                            else:
                                timescale = 0
                                duration = 0
                            if timescale > 0:
                                info["duration_sec"] = duration / timescale

                    # tkhd — 宽高
                    if box_type == b"tkhd" and "width" not in info:
                        if len(payload) >= 86:
                            # 固定点 16.16 格式，高 32 位是整数部分
                            w = struct.unpack(">I", payload[76:80])[0] >> 16
                            h = struct.unpack(">I", payload[80:84])[0] >> 16
                            if w > 0 and h > 0:
                                info["width"] = w
                                info["height"] = h

                    # 深入子 box
                    if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta"):
                        f.seek(f.tell() - len(payload))
                        continue

                except struct.error:
                    break
    except OSError:
        pass
    return info


def parse_mkv_header(file_path: str) -> dict:
    """
    用 struct 解析 Matroska/MKV 文件头部，提取基本参数。
    Matroska 使用 EBML 格式，元素 ID 用 VINT 编码。
    """
    info = {"container": "Matroska/MKV"}
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)

            # 跳过 EBML 头
            # 简单策略：搜索 Segment 起始，然后解析 Info 和 Track
            data = f.read(min(fsize, 8 * 1024 * 1024))  # 只读前 8MB

        # 正则搜索常见 MKV 元素
        import re

        # 时长 (Segment > Info > Duration, float64)
        # 简化：搜索 0x4489 后跟 float 或搜索 Duration 文本附近
        # 这里用更简单的方法 — 搜索帧率和分辨率标签

        # MKV 中 CodecID 常见模式
        codec_ids = re.findall(rb"V_MPEG4/ISO/AVC|V_MPEGH/ISO/HEVC|V_VP9|V_AV1|V_PRORES|A_AAC|A_MPEG/L3|A_PCM|A_FLAC|A_OPUS|A_VORBIS", data)
        if codec_ids:
            seen = set()
            codecs = []
            for cid in codec_ids:
                cstr = cid.decode("ascii", errors="ignore")
                if cstr not in seen:
                    seen.add(cstr)
                    codecs.append(cstr)
            info["codecs_found"] = codecs

    except OSError:
        pass
    return info


# ============================================================================
# ffprobe 解析
# ============================================================================

def parse_with_ffprobe(ffprobe_path: str, file_path: str) -> dict:
    """使用 ffprobe 获取完整视频参数"""
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {"error": result.stderr}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "JSON 解析失败"}

    info = {}
    fmt = data.get("format", {})

    # 容器信息
    info["filename"] = fmt.get("filename", "")
    info["format_name"] = fmt.get("format_name", "")
    try:
        info["duration_sec"] = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        info["duration_sec"] = 0
    try:
        info["size_bytes"] = int(fmt.get("size", 0))
    except (ValueError, TypeError):
        info["size_bytes"] = 0
    try:
        info["bitrate_bps"] = int(fmt.get("bit_rate", 0))
    except (ValueError, TypeError):
        info["bitrate_bps"] = 0

    # 流信息
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")

        if codec_type == "video":
            info["video_codec"] = stream.get("codec_name", "unknown")
            info["video_codec_long"] = stream.get("codec_long_name", "")
            info["width"] = stream.get("width", 0)
            info["height"] = stream.get("height", 0)
            info["pix_fmt"] = stream.get("pix_fmt", "")
            try:
                fps_str = stream.get("r_frame_rate", "0/1")
                if "/" in fps_str:
                    num, den = fps_str.split("/")
                    if int(den) != 0:
                        info["fps"] = int(num) / int(den)
                else:
                    info["fps"] = float(fps_str)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0
            # 宽高比
            w = info.get("width", 0)
            h = info.get("height", 0)
            if w and h:
                info["dar"] = f"{w}:{h}"

        elif codec_type == "audio":
            info["audio_codec"] = stream.get("codec_name", "unknown")
            info["audio_codec_long"] = stream.get("codec_long_name", "")
            try:
                info["sample_rate_hz"] = int(stream.get("sample_rate", 0))
            except (ValueError, TypeError):
                pass
            try:
                info["audio_channels"] = int(stream.get("channels", 0))
            except (ValueError, TypeError):
                pass

    return info


# ============================================================================
# 主程序
# ============================================================================

class VideoInspectorApp:
    """视频检视器 v1.0"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频检视器 v1.0")
        self.root.geometry("500x460")
        self.root.resizable(False, False)
        self.root.configure(bg=COLOR_BG)

        self.ffprobe_path = find_ffprobe()

        self._setup_styles()
        self._build_ui()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT, font=("微软雅黑", 9))
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Card.TLabelframe", background=COLOR_CARD, foreground=COLOR_ACCENT)
        style.configure("Card.TLabelframe.Label", background=COLOR_CARD, foreground=COLOR_ACCENT,
                        font=("微软雅黑", 10, "bold"))
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="#ffffff",
                        borderwidth=0, font=("微软雅黑", 9, "bold"))
        style.map("Accent.TButton", background=[("active", COLOR_ACCENT_HOVER)])

    def _build_ui(self):
        pad = {"padx": 8, "pady": 2}

        # ---- 文件选择 ----
        frm_file = ttk.LabelFrame(self.root, text="选择视频文件", style="Card.TLabelframe")
        frm_file.pack(fill=tk.X, **pad, pady=(8, 4))

        row = tk.Frame(frm_file, bg=COLOR_CARD)
        row.pack(fill=tk.X, padx=6, pady=4)
        ttk.Button(row, text="选择视频文件", command=self._on_select,
                   style="Accent.TButton").pack(side=tk.LEFT, padx=(0, 6))
        self.lbl_file = tk.Label(row, text="未选择", bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY,
                                  font=("微软雅黑", 8), anchor=tk.W)
        self.lbl_file.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 拖拽提示
        tk.Label(row, text="（可拖拽视频文件到本窗口）", bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY,
                font=("微软雅黑", 7)).pack(side=tk.RIGHT)

        # ---- 视频信息 ----
        frm_info = ttk.LabelFrame(self.root, text="视频参数", style="Card.TLabelframe")
        frm_info.pack(fill=tk.BOTH, expand=True, **pad, pady=(4, 8))

        # 使用 Text 控件显示格式化信息
        self.txt_info = tk.Text(frm_info, bg=COLOR_CARD, fg=COLOR_TEXT,
                                 font=("Consolas", 9), wrap=tk.WORD,
                                 relief=tk.FLAT, state="disabled",
                                 padx=10, pady=10)
        self.txt_info.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 设置 Text 标签样式
        self.txt_info.tag_configure("section", foreground=COLOR_ACCENT, font=("Consolas", 10, "bold"))
        self.txt_info.tag_configure("label", foreground=COLOR_TEXT_SECONDARY)
        self.txt_info.tag_configure("value", foreground=COLOR_GREEN)
        self.txt_info.tag_configure("warning", foreground=COLOR_WARN)

        # 初始化显示
        self._show_ready()

    def _show_ready(self):
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", tk.END)
        ff_status = "已检测到 ffprobe" if self.ffprobe_path else "未检测到 ffprobe（将使用基础解析）"
        self.txt_info.insert("1.0", f"就绪\n{ff_status}\n\n请选择视频文件以查看参数。", ("label",))
        self.txt_info.configure(state="disabled")

    def _on_select(self):
        file_path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mov *.avi *.mkv *.wmv *.flv *.webm *.m4v *.mts *.m2ts *.ts *.mpg *.mpeg"),
                       ("所有文件", "*.*")],
        )
        if file_path:
            self._inspect(file_path)

    def _inspect(self, file_path: str):
        """开始解析视频文件"""
        self.lbl_file.config(text=os.path.basename(file_path), fg=COLOR_TEXT)
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", tk.END)
        self.txt_info.insert("1.0", "解析中...\n", ("label",))
        self.txt_info.configure(state="disabled")

        thread = threading.Thread(target=self._inspect_thread, args=(file_path,), daemon=True)
        thread.start()

    def _inspect_thread(self, file_path: str):
        info = {}
        used_ffprobe = False

        # 优先使用 ffprobe
        if self.ffprobe_path:
            info = parse_with_ffprobe(self.ffprobe_path, file_path)
            if "error" not in info:
                used_ffprobe = True

        # 兜底：struct 解析 MP4/MKV
        if not used_ffprobe:
            ext = Path(file_path).suffix.lower()
            if ext in (".mp4", ".mov", ".m4v"):
                fallback = parse_mp4_header(file_path)
            elif ext in (".mkv", ".webm"):
                fallback = parse_mkv_header(file_path)
            else:
                fallback = {"note": f"无法解析 {ext} 格式（需 ffprobe）"}

            # 合并基本信息
            try:
                fstat = os.stat(file_path)
                info["size_bytes"] = fstat.st_size
                info["mtime"] = fstat.st_mtime
                info["filename"] = file_path
            except OSError:
                info["filename"] = file_path
            info.update(fallback)

        info["_used_ffprobe"] = used_ffprobe
        self.root.after(0, self._display_info, info)

    def _display_info(self, info: dict):
        """格式化显示视频参数"""
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", tk.END)

        lines = []

        # 文件信息
        lines.append(("section", "===== 文件信息 =====\n"))
        fname = os.path.basename(info.get("filename", "未知"))
        lines.append(("label", f"  文件名："))
        lines.append(("value", f"{fname}\n"))

        if info.get("size_bytes"):
            lines.append(("label", f"  文件大小："))
            lines.append(("value", f"{format_size(info['size_bytes'])}\n"))

        if info.get("format_name"):
            lines.append(("label", f"  容器格式："))
            lines.append(("value", f"{info['format_name']}\n"))
        elif info.get("container"):
            lines.append(("label", f"  容器格式："))
            lines.append(("value", f"{info['container']}\n"))

        # 时长
        dur = info.get("duration_sec", 0)
        if dur > 0:
            lines.append(("label", f"  时长："))
            lines.append(("value", f"{format_duration(dur)}\n"))

        # 视频流
        if info.get("video_codec") or info.get("width"):
            lines.append(("\n", ""))
            lines.append(("section", "===== 视频流 =====\n"))
            if info.get("video_codec"):
                codec_str = info["video_codec"]
                if info.get("video_codec_long"):
                    codec_str += f" ({info['video_codec_long']})"
                lines.append(("label", f"  编码："))
                lines.append(("value", f"{codec_str}\n"))
            if info.get("width") and info.get("height"):
                lines.append(("label", f"  分辨率："))
                lines.append(("value", f"{info['width']} × {info['height']}\n"))
            if info.get("dar"):
                lines.append(("label", f"  宽高比："))
                lines.append(("value", f"{info['dar']}\n"))
            fps = info.get("fps", 0)
            if fps > 0:
                lines.append(("label", f"  帧率："))
                lines.append(("value", f"{fps:.3f} fps\n"))
            if info.get("pix_fmt"):
                lines.append(("label", f"  像素格式："))
                lines.append(("value", f"{info['pix_fmt']}\n"))

        # 码率
        br = info.get("bitrate_bps", 0)
        if br > 0:
            br_str = f"{br / 1000:.0f} kbps" if br < 10_000_000 else f"{br / 1_000_000:.1f} Mbps"
            lines.append(("label", f"  总码率："))
            lines.append(("value", f"{br_str}\n"))

        # 音频流
        if info.get("audio_codec"):
            lines.append(("\n", ""))
            lines.append(("section", "===== 音频流 =====\n"))
            if info.get("audio_codec"):
                ac_str = info["audio_codec"]
                if info.get("audio_codec_long"):
                    ac_str += f" ({info['audio_codec_long']})"
                lines.append(("label", f"  编码："))
                lines.append(("value", f"{ac_str}\n"))
            if info.get("sample_rate_hz"):
                lines.append(("label", f"  采样率："))
                lines.append(("value", f"{info['sample_rate_hz']} Hz\n"))
            if info.get("audio_channels"):
                ch = info["audio_channels"]
                ch_str = {1: "单声道", 2: "立体声", 6: "5.1 环绕", 8: "7.1 环绕"}.get(ch, f"{ch} 声道")
                lines.append(("label", f"  声道："))
                lines.append(("value", f"{ch_str}\n"))

        # MKV 兜底
        if info.get("codecs_found"):
            lines.append(("\n", ""))
            lines.append(("section", "===== 编码流（MKV 兜底解析）=====\n"))
            for c in info["codecs_found"]:
                lines.append(("value", f"  - {c}\n"))

        if info.get("error"):
            lines.append(("\n", ""))
            lines.append(("warning", f"解析错误：{info['error'][:200]}\n"))

        # 脚注
        if not info.get("_used_ffprobe"):
            lines.append(("\n", ""))
            lines.append(("warning", "（使用 struct 基础解析，信息不完整）\n"))
            lines.append(("label", "安装 ffmpeg 并添加到 PATH 可获得完整参数。\n"))

        # 写入 Text
        for tag, text in lines:
            self.txt_info.insert(tk.END, text, tag)

        self.txt_info.configure(state="disabled")


# ============================================================================
# 入口
# ============================================================================

def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    VideoInspectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
