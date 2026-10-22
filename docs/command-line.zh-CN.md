# 命令行

*本文从 README（快速开始和命令行参数）移来。正文没有改动，只调整了相对链接。*

## 克隆安装与更新

> 想手动控制安装过程？`bash setup.sh`（macOS/Linux）或 `powershell -ExecutionPolicy Bypass -File setup.ps1`（Windows）做的是同样的事。

> **之前克隆过旧版本？** 不要重新 clone（会报 `destination path already exists`）——进目录执行 `git pull` 再启动即可；v0.2.0 起页面里就能一键更新，不再需要命令行。

## 命令行参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--target` | 字幕译成哪种语言。翻译引擎认的代码都行（`zh-CN`/`zh-TW`/`en`/`es`/`ja`/…）。顶栏的「字幕译成」菜单只列 `zh-CN`、`zh-TW`、`en`、`es`；这里给了别的代码时，菜单里多出这一项，改选四种之一就换掉它 | 界面上次的选择；没有则 `zh-CN`（新装且界面是英文时为 `en`） |
| `--source` | 主播语言：单个语言码（如 `es`/`en`/`ja`），或逗号列表只在这些语言里自动检测（如 `es,en`，最多 4 个，第一个是主语言；检测到列表外的语言会强制重转一遍） | 界面上次的选择；没有则 `es,en` |
| `--backend` | 识别后端：`mlx`（Apple GPU）/`ct2`（faster-whisper）/`auto` | `auto` |
| `--model` | whisper 模型：`tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` | 按硬件自动 |
| `--device` | `auto`/`cpu`/`cuda` | 按硬件自动 |
| `--compute-type` | ct2 精度（`int8`/`float16`/…） | 按硬件自动 |
| `--beam` | beam search 宽度（越大越准越慢，`1`=贪心；仅 ct2 后端） | `5` |
| `--context` | 开启滚动上下文。**默认关闭**：实测它会诱发复读死循环，反而大幅拉低召回率 | 关 |
| `--asr-temperature` | 识别解码温度。**默认 0（只解码一次）**：Whisper 默认会在质量不达标时用更高温度重解最多 6 次，音乐段上实测单次识别可达 25 秒 | `0` |
| `--translator` | 翻译引擎：`auto`/`hymt2`/`hymt2-7b`/`gemma`/`deepl`/`google`/`claude`/`openai`/`none`，见[翻译引擎](../README.md#zh-translation-engines) | 界面上次的选择；没有则 `auto` |
| `--denoise` | RNNoise 人声降噪：`auto`/`on`/`off` | `auto`（开） |
| `--port` | 本地 UI 端口 | `8765` |
| `--cookies` | yt-dlp cookies.txt 路径（yt-dlp 报告直播间要登录才能看时用） | 无 |
| `--glossary` | 领域词表文件，见[词表](translation-engines.zh-CN.md#词表) | 项目目录下的 `glossary.txt`，首次运行时由 `glossary.example.txt` 生成 |
| `--banned-terms` | 违禁词表文件，见[违禁词报警](../README.md#zh-banned-term-alerts) | 项目目录下的 `banned_terms.txt`，首次运行时由 `banned_terms.example.txt` 生成 |
| `--demo` | 演示模式，仅驱动 UI | 关 |
| `--doctor` | 打印硬件体检和推荐配置后退出 | 关 |
| `--no-open` | 启动后不自动打开应用窗口或浏览器 | 关 |
| `--ui-lang` | 只对这一次启动生效的界面语言：`zh`/`en`，不保存，见[界面语言](interface-language.zh-CN.md) | 按「界面语言」设置 |
