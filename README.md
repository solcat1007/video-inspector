# VideoInspector
> 视频编码参数解析工具（v2.0）

## 功能
- 选择视频文件（支持常用格式），一键解析完整参数
- **优先 ffprobe 解析**：分辨率 / 编码格式 / 帧率 / 码率 / 时长 / 音频参数
- **无 ffmpeg 时兜底**：用 Python struct 解析 MP4/MKV 头部获取基本信息
- 同时显示文件大小、修改时间等元信息
- **v2.0 新增**：格式识别、缩略图生成、批量扫描、JSON 导出、命令行模式与彩蛋

## 使用方法
```bash
# 图形界面（默认）
python video_inspector.py
```
点击「选择视频文件」即可解析。推荐安装 ffmpeg（含 ffprobe）以获得完整参数信息。

## v2.0 更新说明

### 1. 更丰富的元数据
ffprobe 模式下新增：色彩空间/转移/原色、编码档次与 Level、场序、帧数、SAR/DAR、
视频/音频独立码率、音频语言、容器标签（创建时间、encoder）、流数量。
文件元信息新增创建时间与 SHA-256（CLI `--hash`）。

### 2. 格式识别
`detect_video_format()` 结合扩展名与文件头魔数验证（ftyp / EBML / RIFF-AVI / FLV / MPEG-TS），
即使扩展名错误也能识别真实格式。

### 3. 缩略图生成
```bash
python video_inspector.py -i video.mp4 --thumb shot.png --thumb-time 5
```
优先调用 ffmpeg 截帧；未安装 ffmpeg 时生成纯标准库占位 PNG，功能始终可用。

### 4. 批量扫描与 JSON 导出
```bash
python video_inspector.py -i ./videos --batch --recursive --json all.json --hash
```
支持目录、通配符与多个输入，自动去重并显示进度；JSON 含工具版本、生成时间与全部结果。

### 5. 命令行查看
```bash
python video_inspector.py -i video.mp4 --hash
```
无需图形环境，直接输出关键参数摘要。

## 彩蛋秘籍

| 入口 | 触发方式 | 效果 |
| --- | --- | --- |
| `--easter` | `python video_inspector.py --easter` | 输出 SOLCAT ASCII 署名与隐藏提示 |
| 缩略图彩蛋 | `--thumb-time 42` | 42 秒处藏着惊喜 ;) |

通用秘籍（GUI/其他 SOLCAT 项目）：↑↑↓↓←→←→BA、连点标题 5 次、输入 `solcat`、Ctrl+Shift+T。
