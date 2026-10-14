# The audio chain as Mermaid

*Moved from the README (Architecture). The text is unchanged except for relative links.*

Drawn from the source at [`430fe06`](https://github.com/EM917/tiktok-live-translator/tree/430fe06c139bb3cadc423221b6f4091702a4d51b), with every component checked against where it lives in the code. The typed source and regeneration steps are in [`docs/architecture/`](./).

<details>
<summary>The same topology as Mermaid — editable without any tooling</summary>

```mermaid
flowchart TD
    URL["TikTok live link"] --> RESOLVE["stream resolver<br/>signed-in page (macOS) → live API → retry → WebKit (macOS) → yt-dlp → +sign-in → page<br/>(audio-only preferred; see Fault Tolerance)"]
    RESOLVE -->|"checked media URL"| FF["ffmpeg → RNNoise noise reduction → 16 kHz PCM"]
    FF --> VAD["energy-VAD segmenter (2.5–9 s)"]
    VAD --> AQ["audio queue<br/>60 s cap"]
    AQ --> ASR["Whisper ASR<br/>MLX GPU / faster-whisper<br/>confidence + hallucination filter"]
    GL["glossary<br/>global · brand · streamer"] -.->|"hotwords"| ASR
    ASR -->|"source caption, no wait"| SRV["CaptionServer"]
    ASR --> TQ["translation queue<br/>4 lines, drop oldest"]
    TQ --> TR["translation engine<br/>Hy-MT2 1.8B / 7B (local) / TranslateGemma / DeepL / Google / Claude / OpenAI / off"]
    GL -.->|"term hints · fix-ups"| TR
    TR -->|"translation filled in"| SRV
    ASR -->|"raw_text"| DET["banned-term scan<br/>exact · variant · similar"]
    DET -->|"alert (when on)"| SRV
    SRV --> WS(("WebSocket"))
    WS --> UI["caption surface<br/>app window · Phone Viewing"]
    FF -.->|"stream drops: re-resolve + reconnect"| RESOLVE
```

</details>
