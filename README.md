# VideoInspector
> 视频编码参数解析工具

## 功能
- 选择视频文件（支持常用格式），一键解析完整参数
- **优先 ffprobe 解析**：分辨率 / 编码格式 / 帧率 / 码率 / 时长 / 音频参数
- **无 ffmpeg 时兜底**：用 Python struct 解析 MP4/MKV 头部获取基本信息
- 同时显示文件大小、修改时间等元信息

## 使用方法
```bash
python video_inspector.py
```
点击「选择视频文件」即可解析。推荐安装 ffmpeg（含 ffprobe）以获得完整参数信息。
