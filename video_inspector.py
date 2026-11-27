# -*- coding: utf-8 -*-
"""
视频检视器 v2.0 — 视频编码参数解析工具
========================================
痛点：拿到素材不知道编码格式、分辨率、码率等参数，剪辑导出时参数配错导致画质损失。

功能：
  - 选择视频文件（支持拖拽）
  - 优先用 ffprobe 解析完整参数：分辨率/编码/帧率/码率/时长/音频/色彩空间
  - 无 ffmpeg 时用 Python struct 解析 MP4/MKV 头部获取基本信息
  - 同时显示文件大小、修改时间、哈希等元信息
  - v2.0 新增：格式识别、缩略图生成、批量扫描、JSON 导出、CLI 与 --easter 彩蛋

依赖：仅使用 Python 标准库（struct 解析）；ffprobe/ffmpeg 为推荐外部工具。
更新：2026-11-27 (v2.0)
"""

import argparse
import glob
import hashlib
import os
import struct
import subprocess
import json
import shutil
import sys
import threading
import zlib
from pathlib import Path
from datetime import datetime

# v2.0：无图形环境时仍可使用 CLI
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:
    tk = None

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
    """v2.0：跨平台查找 ffprobe 可执行文件路径"""
    found = shutil.which("ffprobe")
    if found:
        return found
    if os.name == "nt":
        try:
            result = subprocess.run(["where", "ffprobe"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    common = [r"C:\ffmpeg\bin\ffprobe.exe", r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
              "/usr/bin/ffprobe", "/usr/local/bin/ffprobe"]
    for p in common:
        if os.path.exists(p):
            return p
    return None


def find_ffmpeg() -> str | None:
    """v2.0：跨平台查找 ffmpeg（用于缩略图生成）"""
    return shutil.which("ffmpeg")


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

    # v2.0：容器标签与封装信息
    tags = fmt.get("tags", {})
    info["creation_time"] = tags.get("creation_time", "")
    info["encoder"] = tags.get("encoder", "")
    info["format_long_name"] = fmt.get("format_long_name", "")
    try:
        info["nb_streams"] = int(fmt.get("nb_streams", 0))
    except (ValueError, TypeError):
        info["nb_streams"] = 0

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
                info["dar"] = stream.get("display_aspect_ratio") or f"{w}:{h}"
                info["sar"] = stream.get("sample_aspect_ratio", "")
            # v2.0：更多视频元数据
            info["video_profile"] = stream.get("profile", "")
            info["video_level"] = stream.get("level", 0)
            info["color_space"] = stream.get("color_space", "")
            info["color_transfer"] = stream.get("color_transfer", "")
            info["color_primaries"] = stream.get("color_primaries", "")
            info["color_range"] = stream.get("color_range", "")
            info["field_order"] = stream.get("field_order", "")
            info["nb_frames"] = stream.get("nb_frames", "")
            try:
                info["video_bitrate_bps"] = int(stream.get("bit_rate", 0))
            except (ValueError, TypeError):
                info["video_bitrate_bps"] = 0

        elif codec_type == "audio":
            info["audio_codec"] = stream.get("codec_name", "unknown")
            info["audio_codec_long"] = stream.get("codec_long_name", "")
            info["audio_profile"] = stream.get("profile", "")
            info["audio_language"] = (stream.get("tags", {}) or {}).get("language", "")
            try:
                info["sample_rate_hz"] = int(stream.get("sample_rate", 0))
            except (ValueError, TypeError):
                pass
            try:
                info["audio_channels"] = int(stream.get("channels", 0))
            except (ValueError, TypeError):
                pass
            try:
                info["audio_bitrate_bps"] = int(stream.get("bit_rate", 0))
            except (ValueError, TypeError):
                info["audio_bitrate_bps"] = 0

    return info


# ============================================================================
# v2.0 新增功能：格式识别 / 缩略图 / 批量扫描 / JSON 导出 / CLI
# ============================================================================

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".flv",
              ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv"}

FORMAT_NAMES = {
    "mov,mp4,m4a,3gp,3g2,mj2": "MP4 / MOV", "matroska,webm": "Matroska / WebM",
    "avi": "AVI", "flv": "FLV", "mpegts": "MPEG-TS", "mpeg": "MPEG",
    "ogg": "OGG", "asf": "ASF / WMV",
}

# 扩展名 -> 中文格式名（无 ffprobe 时使用）
EXT_FORMAT_NAMES = {
    ".mp4": "MP4", ".mov": "QuickTime MOV", ".m4v": "iTunes M4V",
    ".mkv": "Matroska MKV", ".webm": "WebM", ".avi": "AVI",
    ".wmv": "WMV", ".flv": "FLV", ".ts": "MPEG-TS", ".mts": "MPEG-TS",
    ".m2ts": "MPEG-TS", ".mpg": "MPEG", ".mpeg": "MPEG", ".3gp": "3GP",
    ".ogv": "OGG Video",
}


def detect_video_format(file_path: str) -> str:
    """v2.0：根据扩展名与文件头魔数识别视频格式"""
    ext = Path(file_path).suffix.lower()
    by_ext = EXT_FORMAT_NAMES.get(ext, ext.lstrip('.').upper() or '未知')
    try:
        with open(file_path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return by_ext
    if head[4:8] == b'ftyp':
        return 'MP4 / MOV（文件头验证）'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'Matroska / WebM（EBML 文件头验证）'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'AVI（RIFF 文件头验证）'
    if head[:3] == b'FLV':
        return 'FLV（文件头验证）'
    if head[0] == 0x47:
        return 'MPEG-TS（同步字节验证）'
    return by_ext


def file_sha256(file_path: str, chunk_size: int = 1 << 20) -> str:
    """v2.0：计算文件 SHA-256（分批读取，适合大文件）"""
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def file_metadata(file_path: str) -> dict:
    """v2.0：收集文件系统元信息"""
    stat = os.stat(file_path)
    return {
        'size_bytes': stat.st_size,
        'size_human': format_size(stat.st_size),
        'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        'created': datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S'),
        'extension': Path(file_path).suffix.lower(),
    }


def _write_png(path: str, width: int, height: int, rgb: tuple) -> None:
    """v2.0：纯标准库写入纯色 PNG（ffmpeg 缺失时的缩略图占位）"""
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    raw = b''.join(b'\x00' + bytes(rgb) * width for _ in range(height))
    png = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
           chunk(b'tEXt', b'Comment\x00video-inspector v2.0 thumbnail placeholder') +
           chunk(b'IDAT', zlib.compress(raw, 9)) +
           chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)


def generate_thumbnail(file_path: str, output_path: str, time_pos: float = 1.0,
                       ffmpeg_path: str | None = None) -> dict:
    """
    v2.0：生成视频缩略图。
    优先调用 ffmpeg 截取指定时间点；无 ffmpeg 时生成带注释的占位 PNG，保证功能可用。
    返回 dict(path, method, success)。
    """
    ffmpeg_path = ffmpeg_path or find_ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if ffmpeg_path:
        cmd = [ffmpeg_path, '-y', '-ss', str(time_pos), '-i', file_path,
               '-vframes', '1', '-vf', 'scale=480:-2', output_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and os.path.exists(output_path):
                return {'path': output_path, 'method': 'ffmpeg', 'success': True}
        except (OSError, subprocess.SubprocessError):
            pass
    # 兜底：占位 PNG
    _write_png(output_path, 480, 270, (30, 30, 46))
    return {'path': output_path, 'method': 'placeholder', 'success': True,
            'note': '未安装 ffmpeg，已生成占位图'}


def scan_video_files(paths, recursive: bool = False):
    """v2.0：批量扫描文件或目录中的视频文件，返回去重排序后的列表"""
    found = []
    for path in paths:
        if any(ch in path for ch in '*?['):
            found.extend(glob.glob(path, recursive=recursive))
        elif os.path.isdir(path):
            pattern = os.path.join(path, '**', '*') if recursive else os.path.join(path, '*')
            found.extend(glob.glob(pattern, recursive=recursive))
        else:
            found.append(path)
    result, seen = [], set()
    for f in found:
        if os.path.isfile(f) and Path(f).suffix.lower() in VIDEO_EXTS:
            abs_path = os.path.abspath(f)
            if abs_path not in seen:
                seen.add(abs_path)
                result.append(f)
    return sorted(result)


def inspect_file(file_path: str, include_hash: bool = False) -> dict:
    """v2.0：统一检查入口，返回可用于 JSON 导出的完整字典"""
    info = dict(file_metadata(file_path))
    info['filename'] = file_path
    info['detected_format'] = detect_video_format(file_path)

    ffprobe = find_ffprobe()
    used_ffprobe = False
    if ffprobe:
        parsed = parse_with_ffprobe(ffprobe, file_path)
        if 'error' not in parsed:
            info.update(parsed)
            used_ffprobe = True

    if not used_ffprobe:
        ext = Path(file_path).suffix.lower()
        if ext in ('.mp4', '.mov', '.m4v'):
            info.update(parse_mp4_header(file_path))
        elif ext in ('.mkv', '.webm'):
            info.update(parse_mkv_header(file_path))

    if include_hash:
        info['sha256'] = file_sha256(file_path)
    info['analysis_method'] = 'ffprobe' if used_ffprobe else 'struct-fallback'
    return info


def export_json(results: list, output_path: str) -> str:
    """v2.0：导出检查结果为 JSON（UTF-8，保留中文）"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    payload = {
        'tool': 'video-inspector',
        'version': '2.0',
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'count': len(results),
        'results': results,
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return output_path


def _cli_progress(current, total, name=''):
    """v2.0：批量扫描进度提示"""
    if total <= 0:
        return
    pct = current * 100 // total
    bar = '#' * (pct // 4) + '-' * (25 - pct // 4)
    sys.stderr.write(f"\r[{bar}] {pct:3d}%  {current}/{total} {name}")
    sys.stderr.flush()
    if current >= total:
        sys.stderr.write('\n')


def print_easter_egg():
    """v2.0：--easter 隐藏彩蛋 —— SOLCAT ASCII 署名"""
    art = r"""
  ____             _   _       _   _
 / ___|  ___  _ __| |_(_) __ _| |_| |__
 \___ \ / _ \| '__| __| |/ _` | __| '_ \
  ___) | (_) | |  | |_| | (_| | |_| | |
 |____/ \___/|_|   \__|_|\__,_|\__|_| |_|
"""
    print(art)
    print("  SOLCAT EASTER EGG PROTOCOL v2.0 — 你找到了隐藏入口")
    print("  提示：↑↑↓↓←→←→BA · 连点标题 5 次 · 输入 solcat · Ctrl+Shift+T")
    print("  视频彩蛋：把缩略图时间点设为 42 秒，看看藏着什么 ;)")


def build_parser():
    """v2.0：构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog='video_inspector.py',
        description='视频检视器 v2.0 — 元数据解析、格式识别、缩略图、批量扫描与 JSON 导出',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例:\n'
               '  video_inspector.py -i video.mp4 --json out.json\n'
               '  video_inspector.py -i ./videos --batch --recursive --json all.json\n'
               '  video_inspector.py -i video.mp4 --thumb shot.png --thumb-time 5\n'
               '  video_inspector.py --easter\n'
    )
    parser.add_argument('-i', '--input', nargs='+', help='视频文件或目录（可多个）')
    parser.add_argument('--json', dest='json_path', help='导出 JSON 到指定路径')
    parser.add_argument('-b', '--batch', action='store_true', help='批量扫描模式')
    parser.add_argument('-r', '--recursive', action='store_true', help='递归扫描子目录')
    parser.add_argument('--thumb', dest='thumb_path', help='生成缩略图到指定路径（单文件）')
    parser.add_argument('--thumb-time', type=float, default=1.0, help='缩略图时间点（秒，默认 1.0）')
    parser.add_argument('--hash', action='store_true', help='计算 SHA-256（大文件较慢）')
    parser.add_argument('--no-progress', action='store_true', help='关闭进度提示')
    parser.add_argument('--gui', action='store_true', help='强制启动图形界面')
    parser.add_argument('--easter', action='store_true', help='隐藏彩蛋（嘘……）')
    return parser


def run_cli(argv=None):
    """v2.0：命令行入口，返回退出码"""
    args = build_parser().parse_args(argv)

    if args.easter:
        print_easter_egg()
        return 0

    if not args.input:
        if tk is None:
            print("错误：当前环境无 tkinter，请使用命令行参数（--help 查看用法）", file=sys.stderr)
            return 2
        main()  # 保持 v1 无参数启动 GUI 的行为
        return 0

    files = scan_video_files(args.input, recursive=args.recursive)
    if not files:
        print("错误：未找到可检查的视频文件", file=sys.stderr)
        return 1

    results = []
    failed = 0
    for i, f in enumerate(files, 1):
        try:
            results.append(inspect_file(f, include_hash=args.hash))
            if len(files) > 1 and not args.no_progress:
                _cli_progress(i, len(files), os.path.basename(f))
        except OSError as e:
            failed += 1
            print(f"\n失败: {f} — {e}", file=sys.stderr)

    if args.thumb_path:
        if len(files) != 1:
            print("错误：--thumb 仅支持单个视频文件", file=sys.stderr)
            return 1
        thumb = generate_thumbnail(files[0], args.thumb_path, time_pos=args.thumb_time)
        print(f"缩略图已生成: {thumb['path']}（方式: {thumb['method']}）")
        if thumb.get('note'):
            print(f"  提示: {thumb['note']}")

    if args.json_path:
        out = export_json(results, args.json_path)
        print(f"JSON 已导出: {out}（{len(results)} 条）")
    else:
        for r in results:
            print(f"\n{r['filename']}")
            print(f"  格式: {r.get('detected_format')} | 大小: {r.get('size_human')} | "
                  f"时长: {format_duration(r.get('duration_sec', 0))}")
            print(f"  视频: {r.get('video_codec', '?')} {r.get('width', 0)}x{r.get('height', 0)} "
                  f"@ {r.get('fps', 0):.2f}fps" if r.get('fps') else
                  f"  视频: {r.get('video_codec', '?')} {r.get('width', 0)}x{r.get('height', 0)}")
            print(f"  解析方式: {r['analysis_method']}")
            if r.get('sha256'):
                print(f"  SHA-256: {r['sha256']}")

    if len(files) > 1:
        print(f"\n批量扫描完成：成功 {len(results)}，失败 {failed}，共 {len(files)} 个文件")
    return 0 if failed == 0 else 1


# ============================================================================
# 主程序
# ============================================================================

class VideoInspectorApp:
    """视频检视器 v2.0"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频检视器 v2.0")
        self.root.geometry("560x560")
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

        # v2.0：操作按钮行（JSON 导出 / 缩略图 / 批量扫描）
        actions = tk.Frame(self.root, bg=COLOR_BG)
        actions.pack(fill=tk.X, **pad, pady=(0, 2))
        self.current_file = None
        ttk.Button(actions, text="导出 JSON", command=self._on_export_json).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="生成缩略图", command=self._on_thumbnail).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="批量扫描", command=self._on_batch_scan).pack(side=tk.LEFT)
        self.last_info = None

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
        self.current_file = file_path
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

    # --------------------------------------------------------------------------
    # v2.0：JSON 导出 / 缩略图 / 批量扫描
    # --------------------------------------------------------------------------

    def _on_export_json(self):
        """将当前检查结果导出为 JSON"""
        if not self.last_info:
            messagebox.showinfo("视频检视器 v2.0", "请先选择一个视频文件")
            return
        path = filedialog.asksaveasfilename(
            title="导出 JSON", defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if path:
            out = export_json([self.last_info], path)
            messagebox.showinfo("视频检视器 v2.0", f"已导出: {out}")

    def _on_thumbnail(self):
        """为当前视频生成缩略图"""
        if not self.current_file:
            messagebox.showinfo("视频检视器 v2.0", "请先选择一个视频文件")
            return
        path = filedialog.asksaveasfilename(
            title="保存缩略图", defaultextension=".png",
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
        if path:
            result = generate_thumbnail(self.current_file, path, time_pos=1.0)
            messagebox.showinfo("视频检视器 v2.0",
                                f"缩略图已生成: {result['path']}\n方式: {result['method']}")

    def _on_batch_scan(self):
        """批量扫描目录并导出 JSON"""
        folder = filedialog.askdirectory(title="选择视频目录")
        if not folder:
            return
        files = scan_video_files([folder], recursive=True)
        if not files:
            messagebox.showinfo("视频检视器 v2.0", "目录中未找到视频文件")
            return
        path = filedialog.asksaveasfilename(
            title="批量结果导出 JSON", defaultextension=".json",
            filetypes=[("JSON 文件", "*.json")])
        if not path:
            return
        results = [inspect_file(f) for f in files]
        export_json(results, path)
        messagebox.showinfo("视频检视器 v2.0",
                            f"扫描完成：{len(results)} 个视频\n已导出: {path}")

    def _display_info(self, info: dict):
        """格式化显示视频参数"""
        self.last_info = info
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
        # v2.0：格式识别与控制信息
        if info.get("detected_format"):
            lines.append(("label", "  格式识别："))
            lines.append(("value", f"{info['detected_format']}\n"))
        if info.get("created"):
            lines.append(("label", "  创建时间："))
            lines.append(("value", f"{info['created']}\n"))
        if info.get("sha256"):
            lines.append(("label", "  SHA-256："))
            lines.append(("value", f"{info['sha256'][:32]}...\n"))

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
            # v2.0：更多视频元数据
            if info.get("color_space"):
                lines.append(("label", "  色彩空间："))
                lines.append(("value", f"{info['color_space']} / {info.get('color_transfer', '-')} / "
                                      f"{info.get('color_primaries', '-')}\n"))
            if info.get("video_profile"):
                lines.append(("label", "  编码档次："))
                lines.append(("value", f"{info['video_profile']} (Level {info.get('video_level', '?')})\n"))
            if info.get("nb_frames"):
                lines.append(("label", "  帧数："))
                lines.append(("value", f"{info['nb_frames']}\n"))
            if info.get("video_bitrate_bps"):
                lines.append(("label", "  视频码率："))
                lines.append(("value", f"{info['video_bitrate_bps'] / 1000:.0f} kbps\n"))

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
            if info.get("audio_bitrate_bps"):
                lines.append(("label", "  音频码率："))
                lines.append(("value", f"{info['audio_bitrate_bps'] / 1000:.0f} kbps\n"))
            if info.get("audio_language"):
                lines.append(("label", "  语言："))
                lines.append(("value", f"{info['audio_language']}\n"))

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
    sys.exit(run_cli())
