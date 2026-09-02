# 音频链路架构图 · Audio pipeline diagram

`assets/audio-chain.{zh,en}.svg` 是由这里的 typed JSON 源渲染出来的。两份源拓扑完全一致，
只有措辞语言不同。SVG 内部自带 `prefers-color-scheme`，一个文件同时提供浅色和深色，随系统主题实时切换。
每个组件都带 `sources` 字段，指向它在仓库里的实际位置——渲染时会逐条核对，对不上就渲染失败。

The SVGs in `assets/` are rendered from the typed JSON sources here. Both sources share one topology and
differ only in authored language. Each SVG carries its own `prefers-color-scheme` rules, so one file serves
both light and dark and follows the reader's theme live. Every component carries `sources` pointing at the real code; rendering verifies
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

README 里那两张图由生成的 HTML 用 **Export → SVG 可编辑矢量图** 导出，
放到 `assets/audio-chain.{zh,en}.svg`。SVG 不需要按主题导两次——深浅规则都在文件里。

注意 SVG 的固有宽度等于画布宽（830），README 里要写 `width="1000"` 把它拉到栏宽，
否则会按 830 渲染、字比预期小。字体用 `local('JetBrains Mono')` 加 fallback 栈，
没装该字体的机器会走 fallback，实测排版不受影响。

## 已发布 · Published

- 中文：https://em917.github.io/tiktok-live-translator/architecture/audio-chain.zh.html
- English: https://em917.github.io/tiktok-live-translator/architecture/audio-chain.en.html

GitHub Pages 从 `main` 分支的 `/docs` 目录发布。改完 JSON 后重新 `deliver` 到
`docs/architecture/audio-chain.{zh,en}.html`，推上去 Pages 会自动重建。

Served by GitHub Pages from `/docs` on `main`. Re-`deliver` into
`docs/architecture/` and push; Pages rebuilds on its own.
