# 音频链路架构图 · Audio pipeline diagram

`assets/audio-chain.{zh,en}.{light,dark}.png` 是由这里的 typed JSON 源渲染出来的。两份源拓扑完全一致，
只有措辞语言不同；每种语言各出浅色、深色两版，README 用 `<picture>` + `prefers-color-scheme` 自动切换。
每个组件都带 `sources` 字段，指向它在仓库里的实际位置——渲染时会逐条核对，对不上就渲染失败。

The PNGs in `assets/` are rendered from the typed JSON sources here. Both sources share one topology and
differ only in authored language; each language ships a light and a dark card, switched by `<picture>` and
`prefers-color-scheme`. Every component carries `sources` pointing at the real code; rendering verifies
each reference against the repository and fails if one no longer resolves.

## 重新生成 · Regenerate

需要 [Archify](https://github.com/tt-a1i/archify)（Agent Skill，一次性安装）：

```bash
npx -y skills add tt-a1i/archify --skill archify --agent claude-code --global --yes
```

改完 JSON 后，在仓库根目录跑：

```bash
node ~/.claude/skills/archify/bin/archify.mjs deliver architecture \
  docs/architecture/audio-chain.zh.architecture.json /tmp/audio-chain.zh.html \
  --quality showcase --repo-root .
```

`deliver` 会做 9 项结构校验、核对全部 `sources` 引用，并给出 spec 与产物的 SHA-256。
生成的 HTML 可交互（搜索节点、聚焦、追路径、深浅主题）。

README 里那四张 1200×630 PNG 由它的 **Export → 分享卡片** 导出。分享卡取当前主题，
所以浅色版要先用 `?theme=light` 打开、深色版用 `?theme=dark`，各导一次：

```
/tmp/audio-chain.zh.html?theme=light   →  assets/audio-chain.zh.light.png
/tmp/audio-chain.zh.html?theme=dark    →  assets/audio-chain.zh.dark.png
```

英文版同理，用 `audio-chain.en.architecture.json`。

## 已发布 · Published

- 中文：https://em917.github.io/tiktok-live-translator/architecture/audio-chain.zh.html
- English: https://em917.github.io/tiktok-live-translator/architecture/audio-chain.en.html

GitHub Pages 从 `main` 分支的 `/docs` 目录发布。改完 JSON 后重新 `deliver` 到
`docs/architecture/audio-chain.{zh,en}.html`，推上去 Pages 会自动重建。

Served by GitHub Pages from `/docs` on `main`. Re-`deliver` into
`docs/architecture/` and push; Pages rebuilds on its own.
