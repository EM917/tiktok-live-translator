"""QR 编码器：字节模式、纠错等级 L、版本 2-6，仅用标准库。

为什么自己写（决定记在 phone-viewer 规格 §9，供以后改动时对照）：
  * 手机同看要发一个 43 字符的 token 给同事的手机，桌面剪贴板到不了手机，
    手抄 43 个字符不现实——二维码不是锦上添花，没它这个功能基本不可用。
  * 这个仓库的 requirements.txt 刻意小，依赖边每一条都换过事故
    （glossary/curl_cffi 那几段注释就是账单）。给手机下发一段未审计的
    第三方 JS 反而是把不可读代码放到了新暴露的监听面上。
  * 版本 2-6 + 字节模式 + 单一纠错等级把实现面砍到能被端到端往返测试
    钉住的规模（见 tests/test_qr.py）。

只返回矩阵（0/1 二维数组）和按行拼好的字符串；把矩阵画成图是调用方
（桌面 canvas 或本文件的 CLI）的事，这里不处理颜色/缩放。
"""
import argparse
import sys


class QRError(ValueError):
    """输入在给定的版本范围/纠错等级下编不出来。"""


# ---------------------------------------------------------------------------
# GF(256) 算术：QR 用的原始多项式 x^8+x^4+x^3+x^2+1（0x11D），生成元 alpha=2。
# 只在模块加载时建一次 exp/log 表，纠错编码全靠它，不查外部库。
# ---------------------------------------------------------------------------

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _gf_init():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_gf_init()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(n):
    """RS 生成多项式 g(x) = 乘积(x + alpha^i)，i=0..n-1，系数高次在前。

    测试用一份不查表的 GF(256) 长除法独立算 msg·x^n mod g(x) 来交叉验证，
    所以这里的实现必须是能被单独验证的标准构造，不能抄近路。
    """
    g = [1]
    for i in range(n):
        root = _GF_EXP[i]
        shifted = g + [0]                                    # x * g(x)
        scaled = [0] + [_gf_mul(c, root) for c in g]          # root * g(x)
        g = [a ^ b for a, b in zip(shifted, scaled)]
    return g


def _rs_codewords(msg, n):
    """纠错码字 = msg(x)·x^n mod g(x)（合成除法），返回长度为 n 的整数列表。"""
    g = _rs_generator_poly(n)
    work = list(msg) + [0] * n
    for i in range(len(msg)):
        coef = work[i]
        if coef == 0:
            continue
        for j, gc in enumerate(g):
            work[i + j] ^= _gf_mul(gc, coef)
    return work[len(msg):]


# ---------------------------------------------------------------------------
# 版本 2-6、纠错等级 L 的码字结构（ISO/IEC 18004 表）
# version -> (总码字, 数据码字, 每块纠错码字, 块数)；v2-5 单块，v6 两块等分交织。
# ---------------------------------------------------------------------------

_BLOCKS_L = {
    2: (44, 34, 10, 1),
    3: (70, 55, 15, 1),
    4: (100, 80, 20, 1),
    5: (134, 108, 26, 1),
    6: (172, 136, 18, 2),
}

# 每个版本编码完数据+纠错码字后，矩阵里还剩这么多个填不满一个码字的比特位。
_REMAINDER_BITS = {2: 7, 3: 7, 4: 7, 5: 7, 6: 7}

# 版本 2-6 只有一个对齐图案，中心行/列坐标（正方形，行列相同）。
_ALIGNMENT_CENTER = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}

# 格式信息里纠错等级的 2 位指示符（ISO/IEC 18004 表 25）。
_ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


def _version_size(version):
    return 17 + 4 * version


def _data_codewords(version):
    return _BLOCKS_L[version][1]


def _capacity_bytes(version):
    """字节模式在这个版本(等级 L)下能装的最大原始字节数。

    12 = 4 位模式指示符 + 8 位长度域（版本 1-9 的字节模式长度域都是 8 位）；
    再往下的空间留给至少放得下终止符的那 4 位，多出来的由 padding 填满。
    """
    return (_data_codewords(version) * 8 - 12) // 8


def _choose_version(data_len, min_version, max_version):
    for v in range(min_version, max_version + 1):
        if data_len <= _capacity_bytes(v):
            return v
    raise QRError(
        "数据长度 {} 字节超过版本 {}-{} 等级 L 的容量上限 {} 字节".format(
            data_len, min_version, max_version, _capacity_bytes(max_version)
        )
    )


def _int_to_bits(value, width):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def _bits_to_bytes(bits):
    out = []
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return out


def _build_data_bits(data, version):
    """拼出模式指示符+长度域+数据+终止符+填充，凑满这个版本的数据码字容量。"""
    total_bits = _data_codewords(version) * 8
    bits = []
    bits.extend(_int_to_bits(0b0100, 4))     # 字节模式
    bits.extend(_int_to_bits(len(data), 8))  # 长度域（1-9 版本固定 8 位）
    for b in data:
        bits.extend(_int_to_bits(b, 8))
    if len(bits) > total_bits:
        raise QRError("数据超过版本 {} 等级 L 的容量".format(version))

    remaining = total_bits - len(bits)
    term = min(4, remaining)                 # 终止符 0000，若容量已用满则省略
    bits.extend([0] * term)
    remaining -= term
    pad_to_byte = (-len(bits)) % 8
    bits.extend([0] * pad_to_byte)
    remaining -= pad_to_byte

    pad_bytes = (0xEC, 0x11)                 # 交替填充字节，ISO/IEC 18004 §8.4.9
    i = 0
    while remaining > 0:
        bits.extend(_int_to_bits(pad_bytes[i % 2], 8))
        remaining -= 8
        i += 1
    assert len(bits) == total_bits
    return bits


def _codewords_for_version(data_bytes, version):
    """按版本的块结构切块、各块独立算纠错码字，再交织成最终码字序列。

    v2-5 只有一块，交织退化成原样拼接；v6 是两块等长的块，交织即
    「先各块第 0 个码字、再各块第 1 个……」，读取端必须按同样规则解交织。
    """
    _total, data_cw, ec_per_block, nblocks = _BLOCKS_L[version]
    per_block = data_cw // nblocks
    blocks = [data_bytes[i * per_block:(i + 1) * per_block] for i in range(nblocks)]
    ec_blocks = [_rs_codewords(block, ec_per_block) for block in blocks]

    interleaved = []
    for i in range(per_block):
        for block in blocks:
            interleaved.append(block[i])
    for i in range(ec_per_block):
        for ec in ec_blocks:
            interleaved.append(ec[i])
    return interleaved


# ---------------------------------------------------------------------------
# 矩阵构造：功能图形（定位/时序/对齐/格式信息/暗模块）与数据放置
# ---------------------------------------------------------------------------

def _new_matrix(size):
    return [[0] * size for _ in range(size)]


def _draw_finder(matrix, is_function, size, top, left):
    """画一个定位图案 + 周边分隔带；越界的格子（贴着矩阵边缘的那一侧）跳过。"""
    for dr in range(-1, 8):
        r = top + dr
        if not (0 <= r < size):
            continue
        for dc in range(-1, 8):
            c = left + dc
            if not (0 <= c < size):
                continue
            if 0 <= dr <= 6 and 0 <= dc <= 6:
                dark = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                matrix[r][c] = 1 if dark else 0
            else:
                matrix[r][c] = 0   # 分隔带，恒为白
            is_function[r][c] = True


def _draw_timing(matrix, is_function, size):
    for i in range(size):
        if not is_function[6][i]:
            matrix[6][i] = 1 if i % 2 == 0 else 0
            is_function[6][i] = True
        if not is_function[i][6]:
            matrix[i][6] = 1 if i % 2 == 0 else 0
            is_function[i][6] = True


def _draw_alignment(matrix, is_function, version):
    if version < 2:
        return
    center = _ALIGNMENT_CENTER[version]
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            r, c = center + dr, center + dc
            dark = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
            matrix[r][c] = 1 if dark else 0
            is_function[r][c] = True


def _reserve_format_info(matrix, is_function, size):
    """只占位（标记 is_function），实际比特要等选完掩码后由 _draw_format_bits 写入。

    近侧的两条 9 格必须跳过下标 6：那一格是时序图案，不属于格式信息
    （ISO/IEC 18004 图 25：两份格式信息各自绕开时序图案的那一格）。
    """
    for i in range(9):
        if i != 6:
            is_function[8][i] = True
            is_function[i][8] = True
    for i in range(size - 8, size):
        is_function[8][i] = True
        is_function[i][8] = True
    matrix[size - 8][8] = 1     # 暗模块：固定值，不受掩码影响
    is_function[size - 8][8] = True


def _draw_format_bits(matrix, ecc, mask, size):
    """算 15 位格式信息（BCH(15,5)，生成多项式 0x537，掩码 0x5412）并写两份。"""
    data5 = (_ECC_BITS[ecc] << 3) | mask
    rem = data5
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits15 = ((data5 << 10) | rem) ^ 0x5412

    def bit(i):
        return (bits15 >> i) & 1

    # 第一份：列 8 的上半段（跳过第 6 行的时序图案）+ 行 8 的左半段
    for i in range(6):
        matrix[i][8] = bit(i)
    matrix[7][8] = bit(6)
    matrix[8][8] = bit(7)
    matrix[8][7] = bit(8)
    for i in range(9, 15):
        matrix[8][14 - i] = bit(i)

    # 第二份：行 8 的右半段 + 列 8 的下半段
    for i in range(8):
        matrix[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        matrix[size - 15 + i][8] = bit(i)

    matrix[size - 8][8] = 1


def _place_data(matrix, is_function, bits, size):
    """按标准的「从右下往左上、双列一组、上下交替」Z 字序填数据比特。

    每组双列本来是 (right, right-1)；一旦这组的右列号 <=6，就整体左移一格
    （right -= 1），这样第 6 列（时序图案所在列）永远不会被当成某一组的
    右列去配对，同时仍保证第 0..5 列都被某一组覆盖到——直接用
    range(size-1, 0, -2) 的每个 right 独立算，不在多次迭代之间累积这个偏移。
    """
    bit_index = 0
    total = len(bits)
    for right in range(size - 1, 0, -2):
        col = right - 1 if right <= 6 else right
        upward = ((col + 1) & 2) == 0
        for vert in range(size):
            row = (size - 1 - vert) if upward else vert
            for j in range(2):
                cc = col - j
                if not is_function[row][cc] and bit_index < total:
                    matrix[row][cc] = bits[bit_index]
                    bit_index += 1
    if bit_index != total:
        raise QRError(
            "数据放置与容量不匹配（放入 {}，应为 {}）——这是编码器内部错误，不是输入问题".format(
                bit_index, total
            )
        )


# ---------------------------------------------------------------------------
# 掩码：8 种公式 + 4 条标准罚分规则，选罚分最低的（同分取编号最小的，保证确定性）
# ---------------------------------------------------------------------------

_MASK_FUNCS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _apply_mask(matrix, is_function, mask_index, size):
    fn = _MASK_FUNCS[mask_index]
    out = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if not is_function[r][c] and fn(r, c):
                out[r][c] ^= 1
    return out


def _run_penalty(line):
    total = 0
    run = 1
    for i in range(1, len(line)):
        if line[i] == line[i - 1]:
            run += 1
        else:
            if run >= 5:
                total += 3 + (run - 5)
            run = 1
    if run >= 5:
        total += 3 + (run - 5)
    return total


def _penalty_rule1(matrix, size):
    total = 0
    for r in range(size):
        total += _run_penalty(matrix[r])
    for c in range(size):
        total += _run_penalty([matrix[r][c] for r in range(size)])
    return total


def _penalty_rule2(matrix, size):
    total = 0
    for r in range(size - 1):
        row, nxt = matrix[r], matrix[r + 1]
        for c in range(size - 1):
            v = row[c]
            if v == row[c + 1] == nxt[c] == nxt[c + 1]:
                total += 3
    return total


_FINDER_LIKE_A = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
_FINDER_LIKE_B = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]


def _penalty_rule3(matrix, size):
    total = 0
    for r in range(size):
        row = matrix[r]
        for c in range(size - 10):
            window = row[c:c + 11]
            if window == _FINDER_LIKE_A or window == _FINDER_LIKE_B:
                total += 40
    for c in range(size):
        col = [matrix[r][c] for r in range(size)]
        for r in range(size - 10):
            window = col[r:r + 11]
            if window == _FINDER_LIKE_A or window == _FINDER_LIKE_B:
                total += 40
    return total


def _penalty_rule4(matrix, size):
    dark = sum(row.count(1) for row in matrix)
    percent = dark * 100 // (size * size)
    prev5 = (percent // 5) * 5
    next5 = prev5 + 5
    return min(abs(prev5 - 50), abs(next5 - 50)) // 5 * 10


def _penalty(matrix, size):
    return (
        _penalty_rule1(matrix, size)
        + _penalty_rule2(matrix, size)
        + _penalty_rule3(matrix, size)
        + _penalty_rule4(matrix, size)
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def encode(data, *, min_version=2, max_version=6, ecc="L"):
    """把 data（bytes）编成一个纠错等级 L 的字节模式 QR 矩阵，返回 0/1 二维数组。

    版本从 min_version 到 max_version 里选能装下 data 的最小版本；都装不下抛
    QRError。掩码在 8 种里选标准罚分最低的一种，同分取编号最小的那个——
    保证同一输入两次调用结果完全一致。
    """
    if ecc != "L":
        raise QRError("这个实现只支持纠错等级 L")
    if not isinstance(data, bytes):
        raise QRError("data 必须是 bytes（字节模式的输入约束）")
    if not (2 <= min_version <= max_version <= 6):
        raise QRError("版本范围必须落在 2-6 之内，且 min_version<=max_version")

    version = _choose_version(len(data), min_version, max_version)
    data_bits = _build_data_bits(data, version)
    data_codewords = _bits_to_bytes(data_bits)
    all_codewords = _codewords_for_version(data_codewords, version)

    size = _version_size(version)
    matrix = _new_matrix(size)
    is_function = [[False] * size for _ in range(size)]

    _draw_finder(matrix, is_function, size, 0, 0)
    _draw_finder(matrix, is_function, size, 0, size - 7)
    _draw_finder(matrix, is_function, size, size - 7, 0)
    _draw_timing(matrix, is_function, size)
    _draw_alignment(matrix, is_function, version)
    _reserve_format_info(matrix, is_function, size)

    bitstream = []
    for cw in all_codewords:
        bitstream.extend(_int_to_bits(cw, 8))
    bitstream.extend([0] * _REMAINDER_BITS[version])
    _place_data(matrix, is_function, bitstream, size)

    best = None
    for m in range(8):
        candidate = _apply_mask(matrix, is_function, m, size)
        score = _penalty(candidate, size)
        if best is None or score < best[0]:
            best = (score, m, candidate)

    _score, mask, final_matrix = best
    _draw_format_bits(final_matrix, ecc, mask, size)
    return final_matrix


def rows(matrix, quiet=4):
    """把矩阵拼成每行一个 "0"/"1" 字符串，含 quiet 指定宽度的静区。"""
    size = len(matrix)
    width = size + 2 * quiet
    blank = "0" * width
    out = [blank] * quiet
    for row in matrix:
        out.append("0" * quiet + "".join(str(v) for v in row) + "0" * quiet)
    out.extend([blank] * quiet)
    return out


# ---------------------------------------------------------------------------
# CLI：给负责人独立核对用（例如塞进系统识别器验证一次），不是产品路径的一部分。
# ---------------------------------------------------------------------------

def _write_pbm(path, grid_rows, scale):
    if scale < 1:
        raise QRError("--scale 必须是正整数")
    height = len(grid_rows) * scale
    width = len(grid_rows[0]) * scale if grid_rows else 0
    with open(path, "w", encoding="ascii") as f:
        f.write("P1\n")
        f.write("{} {}\n".format(width, height))
        for line in grid_rows:
            scaled_line = "".join(ch * scale for ch in line)
            for _ in range(scale):
                f.write(" ".join(scaled_line) + "\n")


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.qr", description="生成一个二维码矩阵，可选写出 PBM 图片供人工核对"
    )
    parser.add_argument("text", help="要编码的文本，按 UTF-8 转成字节")
    parser.add_argument("--pbm", metavar="PATH", help="把结果写成一张 PBM(P1) 图片")
    parser.add_argument("--scale", type=int, default=8, help="每个模块放大到多少像素（默认 8）")
    parser.add_argument("--min-version", type=int, default=2)
    parser.add_argument("--max-version", type=int, default=6)
    args = parser.parse_args(argv)

    try:
        matrix = encode(
            args.text.encode("utf-8"),
            min_version=args.min_version,
            max_version=args.max_version,
        )
    except QRError as exc:
        print("编码失败: {}".format(exc), file=sys.stderr)
        return 1

    grid_rows = rows(matrix, quiet=4)
    if args.pbm:
        _write_pbm(args.pbm, grid_rows, args.scale)
        print("已写出 {}".format(args.pbm))
    else:
        for line in grid_rows:
            print(line.replace("1", "██").replace("0", "  "))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
