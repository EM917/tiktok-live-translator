# 音频链路的 Mermaid 版本

*本文从 README（架构）移来。正文没有改动，只调整了相对链接。*

图按 [`430fe06`](https://github.com/EM917/tiktok-live-translator/tree/430fe06c139bb3cadc423221b6f4091702a4d51b) 的源码画出，每个组件都核对过实际位置。Typed 源与重新生成的方法见 [`docs/architecture/`](./)。

<details>
<summary>同一份拓扑的 Mermaid 版本——不装任何工具也能改</summary>

```mermaid
flowchart TD
    URL["TikTok 直播间地址"] --> RESOLVE["解析直播流<br/>登录直播页（macOS）→ 官方接口 → 重试 → WebKit（macOS）→ yt-dlp → 借登录态 → 直播页<br/>（优先纯音频档，见「抗故障」）"]
    RESOLVE -->|"校验后的媒体地址"| FF["ffmpeg → RNNoise 人声降噪 → 16 kHz PCM"]
    FF --> VAD["能量 VAD 切段（2.5–9 秒）"]
    VAD --> AQ["音频队列<br/>上限 60 秒"]
    AQ --> ASR["Whisper 语音识别<br/>MLX GPU / faster-whisper<br/>置信度过滤 + 幻觉丢弃"]
    GL["领域词表<br/>全局 · 本场品牌 · 主播"] -.->|"识别热词"| ASR
    ASR -->|"原文字幕，不等翻译"| SRV["CaptionServer"]
    ASR --> TQ["翻译队列<br/>最多 4 句，满则丢最旧"]
    TQ --> TR["翻译引擎<br/>Hy-MT2 1.8B / 7B（本地）/ TranslateGemma / DeepL / Google / Claude / OpenAI / 关闭"]
    GL -.->|"术语提示 · 译文兜底"| TR
    TR -->|"译文回填"| SRV
    ASR -->|"raw_text"| DET["违禁词检测<br/>命中 · 变体 · 疑似"]
    DET -->|"报警（开关打开时）"| SRV
    SRV --> WS(("WebSocket"))
    WS --> UI["字幕界面<br/>应用窗口 · 手机同看"]
    FF -.->|"断流：重连，原地址没有音频再重新解析"| RESOLVE
```

</details>
