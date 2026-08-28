"""词不达意实测：译文有没有把这句话的意思传达到。

与 bench_glossary.py 的分工：那个测商品名有没有译成规范写法，这个测**中控读了
之后会不会理解错**。两者会分开的地方正是要害——「plata gratis」译成「免费银币」
不违反任何词条，却让人完全看不明白在说什么。

判据只做包含/不包含，不做语义打分。让模型当裁判的方案已实测否决（1.8B 对所有
句子都判不一致，7B 十次只对四次），回译后比词汇重合度也否决过（正确与错误的
译文得分完全相同）。所以这里用人写死的断言，条目来自实盘日志里真实的错译。

跑法：
    python3 tools/bench_meaning.py              # 默认引擎
    python3 tools/bench_meaning.py hymt2-7b     # 指定引擎
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.glossary import load                    # noqa: E402
from app.translator import create_translator     # noqa: E402

CASES_FILE = Path(__file__).resolve().parent / "meaning_cases.txt"


def load_cases(path=None):
    """解析回归集。格式：西语 | 必须出现（; 分隔） | 不能出现（; 分隔）"""
    cases = []
    for line in (path or CASES_FILE).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip() if line.lstrip().startswith("#") else line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            continue
        text, must, never = parts
        cases.append((text,
                      [w.strip() for w in must.split(";") if w.strip()],
                      [w.strip() for w in never.split(";") if w.strip()]))
    return cases


def judge(out, must, never):
    """返回 (通过?, 说明)。必须出现的是「任一即可」——中文说法本来就多。"""
    for w in never:
        if w in out:
            return False, "出现了不该出现的「{}」".format(w)
    if must and not any(w in out for w in must):
        return False, "没有传达出「{}」".format(" / ".join(must))
    return True, ""


async def main():
    engine = sys.argv[1] if len(sys.argv) > 1 else "auto"
    g = load("glossary.txt")
    cases = load_cases()
    tr = create_translator(engine)
    if tr is None:
        print("没有可用的翻译引擎")
        return
    print("引擎 {}（{}），{} 条回归用例\n".format(
        engine, getattr(tr, "name", "?"), len(cases)))
    passed = 0
    for text, must, never in cases:
        out = await tr.translate(text, "zh-CN", source="es",
                                 glossary=tuple(g.translation_pairs(text)) or None)
        out = g.apply(text, out or "")
        ok, why = judge(out, must, never)
        passed += ok
        print("{} {}".format("✅" if ok else "❌", text[:72]))
        if not ok:
            print("   {}".format(why))
            print("   译文 {}".format(out[:110]))
    print("\n{}/{} 通过 = {:.0f}%".format(passed, len(cases),
                                          100.0 * passed / max(len(cases), 1)))
    await tr.close()


if __name__ == "__main__":
    asyncio.run(main())
