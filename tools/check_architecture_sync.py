"""架构图源文件的窄/宽几何漂移检查：不装 Archify 也能在 CI 里跑。

docs/architecture/README.md 定的规矩是「每种语言两份几何、一套内容」——
`audio-chain.{zh,en}.architecture.json`（README 用的窄画布）和
`audio-chain.{zh,en}.wide.architecture.json`（网页版用的宽画布）除坐标、画布
尺寸和宽版独有的边界框（`boundaries`）外，节点、连线、卡片、引导视图、标题要
逐字相同。这条约束只在人改 JSON 时靠自觉——2026-09-18 那次改 resolver 节点的
文案就是两份各改一次，漏改一份不会报错、也不会让 `archify deliver` 失败，
网页版和 README 图会悄悄不一致，直到有人肉眼对比出来。

这个脚本只做比较，不依赖 Archify、不需要 Node：把两份 JSON 里公认是几何的键
（位置、尺寸、画布、连线走线/标签偏移）剥掉、把宽版独有的 `boundaries` 去掉，
剩下的部分要求逐字相等；不相等就打印第一处分叉的路径和两边的值，退出码非零。

用法：
    python3 tools/check_architecture_sync.py            # 检查中英文两组
    python3 tools/check_architecture_sync.py --lang en  # 只检查一组
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时（Windows 上 `python3 tools/x.py > out.txt` 的默认编码是 ANSI
# 代码页 + strict），漂移报告里的路径和值可能带非 ASCII 字符，不该让工具中途
# 崩掉：见 app/stdio.py。这是本脚本对 app/ 的唯一依赖，不牵扯 main.py。
harden_stdio()

ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = ROOT / "docs" / "architecture"

# 几何相关的键：不管出现在结构里的哪一层，一律从比较对象里剔除。
# 位置/尺寸（pos、size、viewBox、canvas、bounds 及其常见别名）、连线为了适配
# 不同画布做的走线调整（via、fromSide、toSide、labelDx、labelDy）都算几何——
# 窄画布挤，连线常需要绕路或挪标签，这不算内容改动。
GEOMETRY_KEYS = {
    "pos", "size", "viewBox", "canvas", "bounds", "bbox",
    "x", "y", "width", "height", "x1", "y1", "x2", "y2", "cx", "cy",
    "via", "fromSide", "toSide", "labelDx", "labelDy",
}

# 宽版独有、窄版按文档规定直接不写的一节（腾出宽度用）：整节跳过比较，
# 而不是要求双方都没有或都有相同内容。
GEOMETRY_ONLY_TOP_LEVEL_KEYS = {"boundaries"}


def strip_geometry(node):
    """递归剥掉几何键，保留内容键的原始顺序无关紧要（比较时按值比较）。"""
    if isinstance(node, dict):
        return {
            key: strip_geometry(value)
            for key, value in node.items()
            if key not in GEOMETRY_KEYS
        }
    if isinstance(node, list):
        return [strip_geometry(item) for item in node]
    return node


def load_content(path):
    """读一份 JSON 源文件，返回剥掉几何键之后的可比较结构。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in GEOMETRY_ONLY_TOP_LEVEL_KEYS:
        data.pop(key, None)
    return strip_geometry(data)


def first_difference(a, b, path="$"):
    """深度比较，返回第一处分叉的 (路径, a 的值, b 的值)；完全相同则返回 None。

    只找第一处而不是全部列出：漂移检查的目的是「发现忘改的那一份」，第一处
    足够定位，且避免为一次搬移打印几十行噪音。"""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                return "{}.{}".format(path, key), "<missing>", b[key]
            if key not in b:
                return "{}.{}".format(path, key), a[key], "<missing>"
            found = first_difference(a[key], b[key], "{}.{}".format(path, key))
            if found:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return "{}[]".format(path), "{} items".format(len(a)), "{} items".format(len(b))
        for index, (item_a, item_b) in enumerate(zip(a, b)):
            found = first_difference(item_a, item_b, "{}[{}]".format(path, index))
            if found:
                return found
        return None
    if a != b:
        return path, a, b
    return None


def check_language(lang, arch_dir=ARCH_DIR):
    """比较一种语言的窄/宽源文件，返回 (是否一致, 说明字符串)。"""
    narrow_path = arch_dir / "audio-chain.{}.architecture.json".format(lang)
    wide_path = arch_dir / "audio-chain.{}.wide.architecture.json".format(lang)
    for candidate in (narrow_path, wide_path):
        if not candidate.exists():
            return False, "missing source file: {}".format(candidate)

    narrow = load_content(narrow_path)
    wide = load_content(wide_path)
    diff = first_difference(narrow, wide)
    if diff is None:
        return True, "{} narrow/wide content in sync".format(lang)
    path, narrow_value, wide_value = diff
    return False, (
        "{} narrow/wide content drift at {}\n"
        "  narrow ({}): {!r}\n"
        "  wide   ({}): {!r}"
    ).format(lang, path, narrow_path.name, narrow_value, wide_path.name, wide_value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=["en", "zh"], action="append",
                         help="只检查这一种语言（可重复传，默认检查 en 和 zh）")
    parser.add_argument("--arch-dir", type=Path, default=ARCH_DIR,
                         help="架构 JSON 所在目录（默认 docs/architecture）")
    args = parser.parse_args(argv)

    languages = args.lang or ["en", "zh"]
    ok = True
    for lang in languages:
        in_sync, message = check_language(lang, args.arch_dir)
        print(("OK: " if in_sync else "DRIFT: ") + message)
        ok = ok and in_sync
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
