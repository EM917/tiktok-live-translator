# 音频链路架构图 · Audio pipeline diagram

每种语言有**两份几何、一套内容**，因为两个产物的约束正好相反：

| 源文件 | 产物 | 画布 | 为什么 |
|---|---|---|---|
| `audio-chain.{zh,en}.architecture.json` | `assets/*.svg`（README） | 830×720 | README 栏宽只有约 1012px，画布必须窄，字才够大 |
| `audio-chain.{zh,en}.wide.architecture.json` | `docs/architecture/*.html`（网页版） | 1240×600 | 网页版不受栏宽限制，宽扁才能一屏放下、不用滚 |

两份的节点、关系、卡片、引导视图、标题逐字相同，只有坐标、画布尺寸和边界框不同
（窄版为了腾出宽度去掉了边界框，宽版保留）。改内容时**两份都要改**。

每个组件都带 `sources` 字段，指向它在仓库里的实际位置——渲染时会逐条核对，对不上就渲染失败。

Two geometries per language, one set of content. The narrow source feeds the README SVG, where the
~1012px column forces a narrow canvas to keep the captions legible; the `.wide.` source feeds the published
HTML, where a wide canvas is what fits a browser window without scrolling. Nodes, relationships, cards and
guided views are identical between them — edit both when the content changes.

Each SVG carries its own `prefers-color-scheme` rules, so one file serves both light and dark and follows
the reader's theme live. Every component carries `sources` pointing at the real code; rendering verifies
each reference against the repository and fails if one no longer resolves.

## 重新生成 · Regenerate

需要 [Archify](https://github.com/tt-a1i/archify)（Agent Skill，一次性安装）：

```bash
npx -y skills add tt-a1i/archify --skill archify --agent claude-code --global --yes
```

改完 JSON 后，在仓库根目录跑（注意两个产物用**不同的源**）：

```bash
A=~/.claude/skills/archify/bin/archify.mjs

# 网页版：用 .wide. 源，直接产出到 docs/ 供 Pages 发布
node $A deliver architecture docs/architecture/audio-chain.zh.wide.architecture.json \
  docs/architecture/audio-chain.zh.html --quality showcase --repo-root .

# README 用的 SVG：用窄版源先出一个临时 HTML，再从里面导出 SVG
node $A deliver architecture docs/architecture/audio-chain.zh.architecture.json \
  /tmp/audio-chain.zh.html --quality showcase --repo-root .
```

英文版把 `zh` 换成 `en`。`deliver` 会做 9 项结构校验、核对全部 `sources` 引用，
并给出 spec 与产物的 SHA-256。生成的 HTML 可交互（搜索节点、聚焦、追路径、深浅主题）。

改完网页版后建议跑一次 `node $A visual-check docs/architecture/audio-chain.zh.html --json`，
确认 1440×900 起的四个视口都不溢出——宽扁画布就是为这个选的。它会在同目录留下
`*.visual-check.*` 旁生文件，看完删掉，别提交。

README 里那两张图由**窄版**临时 HTML 用 **Export → SVG 可编辑矢量图** 导出，
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
