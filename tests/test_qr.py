"""app/qr.py 的测试：往返、纠错交叉验证、结构、掩码罚分、降级、回归夹具。

往返测试里的朴素读取器是刻意独立写的一份（不调用 app.qr 里任何私有的放置/掩码
辅助函数），只共享「versions 2-6、纠错等级 L」这个公开约定——这样如果编码器
在 Z 字序、掩码方向、格式信息位置上有 bug，读取器大概率也会用不同的方式踩到，
而不是抄同一段代码把 bug 也抄一遍。

纠错交叉验证同理：GF(256) 乘法在这里用按位移位 + 0x11D 约简重新写一遍，
不查 app.qr 建好的 exp/log 表。
"""
import json
import os

import pytest

from app import qr


# ---------------------------------------------------------------------------
# 独立实现 1：GF(256) 长除法（不查表），用来交叉验证 app.qr._rs_codewords
# ---------------------------------------------------------------------------

def _gf_mul_shift(a, b):
    """按位移位 + 0x11D 约简的 GF(256) 乘法，不使用 app.qr 的 exp/log 表。"""
    result = 0
    aa = a
    for i in range(8):
        if (b >> i) & 1:
            result ^= aa << i
    for bit in range(14, 7, -1):
        if (result >> bit) & 1:
            result ^= 0x11D << (bit - 8)
    return result


def _alpha_pow_shift(i):
    """alpha=2 的 i 次幂，只用 _gf_mul_shift 累乘，不查表。"""
    v = 1
    for _ in range(i):
        v = _gf_mul_shift(v, 2)
    return v


def _gen_poly_shift(n):
    g = [1]
    for i in range(n):
        root = _alpha_pow_shift(i)
        shifted = g + [0]
        scaled = [0] + [_gf_mul_shift(c, root) for c in g]
        g = [x ^ y for x, y in zip(shifted, scaled)]
    return g


def _rs_codewords_shift(msg, n):
    g = _gen_poly_shift(n)
    work = list(msg) + [0] * n
    for i in range(len(msg)):
        coef = work[i]
        if coef:
            for j, gc in enumerate(g):
                work[i + j] ^= _gf_mul_shift(gc, coef)
    return work[len(msg):]


class TestReedSolomonCrossCheck:
    def test_matches_independent_long_division(self):
        cases = [
            ([0] * 34, 10),
            (list(range(34)), 10),
            ([255] * 55, 15),
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 20),
            (list(range(68)), 18),
        ]
        for msg, n in cases:
            assert qr._rs_codewords(msg, n) == _rs_codewords_shift(msg, n)

    def test_generator_degree_equals_n(self):
        for n in (10, 15, 18, 20, 26):
            g = _gen_poly_shift(n)
            assert len(g) - 1 == n
            assert g[0] == 1

    def test_changing_one_byte_changes_ec(self):
        msg = list(range(1, 35))
        ec = qr._rs_codewords(msg, 10)
        msg2 = list(msg)
        msg2[5] ^= 0x01
        ec2 = qr._rs_codewords(msg2, 10)
        assert ec != ec2


# ---------------------------------------------------------------------------
# 独立实现 2：朴素 QR 读取器（格式信息 -> 反掩码 -> Z 字序 -> 解模式/长度/字节）
# ---------------------------------------------------------------------------

_BLOCKS_L = {
    2: (34, 10, 1),
    3: (55, 15, 1),
    4: (80, 20, 1),
    5: (108, 26, 1),
    6: (136, 18, 2),
}
_ALIGNMENT_CENTER = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}
_REMAINDER_BITS = {2: 7, 3: 7, 4: 7, 5: 7, 6: 7}


def _reader_mask_condition(mask, r, c):
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    if mask == 7:
        return ((r + c) % 2 + (r * c) % 3) % 2 == 0
    raise ValueError("坏掩码编号")


def _reader_is_function(size, version):
    is_func = [[False] * size for _ in range(size)]

    def mark(r, c):
        is_func[r][c] = True

    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            r = top + dr
            if not (0 <= r < size):
                continue
            for dc in range(-1, 8):
                c = left + dc
                if 0 <= c < size:
                    mark(r, c)

    for i in range(size):
        mark(6, i)
        mark(i, 6)

    center = _ALIGNMENT_CENTER[version]
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            mark(center + dr, center + dc)

    for i in range(9):
        if i != 6:
            mark(8, i)
            mark(i, 8)
    for i in range(size - 8, size):
        mark(8, i)
        mark(i, 8)

    return is_func


def _reader_format_bits(matrix, size):
    """从第一份格式信息读出原始 15 位（未做 XOR 掩码解除）。"""
    raw = 0
    positions = []
    for i in range(6):
        positions.append((i, 8))
    positions.append((7, 8))
    positions.append((8, 8))
    positions.append((8, 7))
    for i in range(9, 15):
        positions.append((8, 14 - i))
    for i, (r, c) in enumerate(positions):
        raw |= (matrix[r][c] << i)
    return raw


def _reader_zigzag_order(size):
    """重建放置顺序（行, 列）的列表，独立于 app.qr._place_data 单独写。"""
    order = []
    for right in range(size - 1, 0, -2):
        col = right - 1 if right <= 6 else right
        upward = ((col + 1) & 2) == 0
        for vert in range(size):
            row = (size - 1 - vert) if upward else vert
            for j in range(2):
                order.append((row, col - j))
    return order


def _naive_read(matrix):
    size = len(matrix)
    version = (size - 17) // 4
    assert 2 <= version <= 6, "只支持版本 2-6"

    is_func = _reader_is_function(size, version)

    raw15 = _reader_format_bits(matrix, size)
    unmasked15 = raw15 ^ 0x5412
    data5 = unmasked15 >> 10
    mask = data5 & 0b111

    bits = []
    for row, col in _reader_zigzag_order(size):
        if is_func[row][col]:
            continue
        v = matrix[row][col]
        if _reader_mask_condition(mask, row, col):
            v ^= 1
        bits.append(v)

    data_cw, ec_per_block, nblocks = _BLOCKS_L[version]
    per_block = data_cw // nblocks
    total_codewords = data_cw + ec_per_block * nblocks

    codeword_bits = bits[: total_codewords * 8]
    codewords = []
    for i in range(0, len(codeword_bits), 8):
        byte = 0
        for b in codeword_bits[i:i + 8]:
            byte = (byte << 1) | b
        codewords.append(byte)

    interleaved_data = codewords[:data_cw]
    data_bytes = [0] * data_cw
    for block_idx in range(nblocks):
        for pos in range(per_block):
            k = pos * nblocks + block_idx
            data_bytes[block_idx * per_block + pos] = interleaved_data[k]

    data_bits = []
    for byte in data_bytes:
        for i in range(7, -1, -1):
            data_bits.append((byte >> i) & 1)

    def take(n):
        nonlocal data_bits
        chunk, data_bits = data_bits[:n], data_bits[n:]
        val = 0
        for b in chunk:
            val = (val << 1) | b
        return val

    mode = take(4)
    assert mode == 0b0100, "只实现了字节模式，读到了别的模式指示符"
    length = take(8)
    payload = bytearray()
    for _ in range(length):
        payload.append(take(8))
    return bytes(payload)


# ---------------------------------------------------------------------------
# 1. 往返
# ---------------------------------------------------------------------------

def _viewer_url_payloads():
    token = "a" * 43
    return [
        ("http://192.168.1.23:8766/#k=" + token).encode("utf-8"),
        ("http://10.0.0.5:8766/#k=" + token).encode("utf-8"),
        ("http://172.16.9.9:8766/#k=" + token).encode("utf-8"),
        ("http://255.255.255.255:65535/#k=" + token).encode("utf-8"),
    ]


def _boundary_payloads():
    out = []
    for version in range(2, 7):
        cap = qr._capacity_bytes(version)
        for delta in (-1, 0, 1):
            n = cap + delta
            if n < 0:
                continue
            if n > qr._capacity_bytes(6):
                continue  # 超过所有版本容量的情形在降级测试里单独覆盖
            out.append(bytes((i % 256) for i in range(n)))
    return out


def _ascii_payload():
    return bytes(range(0x20, 0x7F))  # 全部可见 ASCII


def _all_round_trip_payloads():
    payloads = []
    payloads.extend(_viewer_url_payloads())
    payloads.extend(_boundary_payloads())
    payloads.append(_ascii_payload())
    payloads.append(b"")
    payloads.append(b"hello")
    payloads.append(b"\x00\x01\x02\xff\xfe")
    return payloads


@pytest.mark.parametrize("payload", _all_round_trip_payloads())
def test_round_trip(payload):
    matrix = qr.encode(payload)
    assert _naive_read(matrix) == payload


def test_round_trip_covers_all_versions_2_to_6():
    seen_versions = set()
    for payload in _all_round_trip_payloads():
        matrix = qr.encode(payload)
        seen_versions.add((len(matrix) - 17) // 4)
    assert seen_versions == {2, 3, 4, 5, 6}


def test_round_trip_min_max_version_pins_version():
    payload = b"short"
    matrix = qr.encode(payload, min_version=4, max_version=4)
    assert (len(matrix) - 17) // 4 == 4
    assert _naive_read(matrix) == payload


# ---------------------------------------------------------------------------
# 3. 结构
# ---------------------------------------------------------------------------

class TestStructure:
    def test_three_finder_patterns(self):
        matrix = qr.encode(b"hola")
        size = len(matrix)
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            for dr in range(7):
                for dc in range(7):
                    dark = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    assert matrix[top + dr][left + dc] == (1 if dark else 0)

    def test_timing_pattern(self):
        matrix = qr.encode(b"hola")
        size = len(matrix)
        for i in range(8, size - 8):
            assert matrix[6][i] == (1 if i % 2 == 0 else 0)
            assert matrix[i][6] == (1 if i % 2 == 0 else 0)

    def test_alignment_pattern_present_for_all_versions(self):
        for version in range(2, 7):
            payload = bytes((i % 256) for i in range(qr._capacity_bytes(version)))
            matrix = qr.encode(payload, min_version=version, max_version=version)
            center = _ALIGNMENT_CENTER[version]
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    assert matrix[center + dr][center + dc] == (1 if dark else 0)

    def test_dark_module_position(self):
        for version in range(2, 7):
            payload = b"x" * min(5, qr._capacity_bytes(version))
            matrix = qr.encode(payload, min_version=version, max_version=version)
            size = len(matrix)
            assert size - 8 == 4 * version + 9
            assert matrix[size - 8][8] == 1

    def test_format_info_matches_independent_bch(self):
        """独立算一遍 BCH(15,5)（原始比特长除法，不复用 _draw_format_bits），
        与矩阵里实际写的两份格式信息互相核对。"""

        def bch15_5(data5):
            gen = 0b10100110111  # x^10+x^8+x^5+x^4+x^2+x+1，11 位
            value = data5 << 10
            for shift in range(4, -1, -1):
                bit_pos = 10 + shift
                if (value >> bit_pos) & 1:
                    value ^= gen << shift
            remainder = value & 0x3FF
            return (data5 << 10) | remainder

        for version in range(2, 7):
            payload = b"y" * min(5, qr._capacity_bytes(version))
            matrix = qr.encode(payload, min_version=version, max_version=version)
            size = len(matrix)

            raw1 = _reader_format_bits(matrix, size)
            data5 = (raw1 ^ 0x5412) >> 10
            expected = bch15_5(data5) ^ 0x5412
            assert raw1 == expected

            # 第二份格式信息：行 8 右半段 + 列 8 下半段，必须和第一份一致
            raw2 = 0
            for i in range(8):
                raw2 |= matrix[8][size - 1 - i] << i
            for i in range(8, 15):
                raw2 |= matrix[size - 15 + i][8] << i
            assert raw2 == raw1

            ecc_bits = data5 >> 3
            assert ecc_bits == qr._ECC_BITS["L"]


# ---------------------------------------------------------------------------
# 4. 掩码选择
# ---------------------------------------------------------------------------

class TestMasking:
    def test_all_eight_masks_implemented(self):
        assert len(qr._MASK_FUNCS) == 8
        # 8 个公式两两不同（用一小片坐标网格采样，几乎不可能碰撞成相同函数）
        size = 21
        samples = [(r, c) for r in range(size) for c in range(size)]
        signatures = set()
        for fn in qr._MASK_FUNCS:
            sig = tuple(fn(r, c) for r, c in samples)
            signatures.add(sig)
        assert len(signatures) == 8

    def test_penalty_rule1_long_run(self):
        line = [0, 0, 0, 0, 0, 0, 1]  # 6 连同色，罚 3+(6-5)=4
        assert qr._run_penalty(line) == 4
        line_exact5 = [1, 1, 1, 1, 1, 0]
        assert qr._run_penalty(line_exact5) == 3

    def test_penalty_rule2_2x2_blocks(self):
        # 棋盘格背景本身不含任何同色 2x2 块（每个 2x2 块恒为 2 个 0 + 2 个 1）；
        # 只在左上角强行铺一块 2x2 同色，隔离出唯一一次命中，避免背景色块干扰计数。
        size = 6
        matrix = [[(r + c) % 2 for c in range(size)] for r in range(size)]
        matrix[0][0] = matrix[0][1] = matrix[1][0] = matrix[1][1] = 1
        assert qr._penalty_rule2(matrix, size) == 3

    def test_penalty_rule3_finder_like_pattern(self):
        size = 21
        matrix = [[0] * size for _ in range(size)]
        matrix[0][0:11] = qr._FINDER_LIKE_A
        assert qr._penalty_rule3(matrix, size) >= 40

    def test_penalty_rule4_dark_light_imbalance(self):
        size = 10
        all_dark = [[1] * size for _ in range(size)]
        assert qr._penalty_rule4(all_dark, size) == 10 * (50 // 5)
        half = [[1 if c < size // 2 else 0 for c in range(size)] for _ in range(size)]
        assert qr._penalty_rule4(half, size) == 0

    def test_deterministic_same_input_same_output(self):
        payload = b"http://192.168.1.23:8766/#k=" + b"a" * 43
        m1 = qr.encode(payload)
        m2 = qr.encode(payload)
        assert m1 == m2


# ---------------------------------------------------------------------------
# 5. 降级
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_over_capacity_raises(self):
        too_big = bytes(qr._capacity_bytes(6) + 1)
        with pytest.raises(qr.QRError):
            qr.encode(too_big)

    def test_over_capacity_within_restricted_version_range_raises(self):
        payload = bytes(qr._capacity_bytes(2) + 1)
        with pytest.raises(qr.QRError):
            qr.encode(payload, min_version=2, max_version=2)

    def test_bad_ecc_level_raises(self):
        with pytest.raises(qr.QRError):
            qr.encode(b"hola", ecc="H")

    def test_bad_version_range_raises(self):
        with pytest.raises(qr.QRError):
            qr.encode(b"hola", min_version=1, max_version=6)
        with pytest.raises(qr.QRError):
            qr.encode(b"hola", min_version=5, max_version=3)
        with pytest.raises(qr.QRError):
            qr.encode(b"hola", min_version=2, max_version=7)

    def test_non_bytes_input_raises(self):
        with pytest.raises(qr.QRError):
            qr.encode("hola")  # 必须是 bytes，不是 str


# ---------------------------------------------------------------------------
# rows()：静区与行内容
# ---------------------------------------------------------------------------

class TestRows:
    def test_quiet_zone_and_dimensions(self):
        matrix = qr.encode(b"hola")
        size = len(matrix)
        r = qr.rows(matrix, quiet=4)
        assert len(r) == size + 8
        assert all(len(line) == size + 8 for line in r)
        for line in r[:4] + r[-4:]:
            assert line == "0" * (size + 8)
        for i, row in enumerate(matrix):
            assert r[4 + i] == "0" * 4 + "".join(str(v) for v in row) + "0" * 4

    def test_default_quiet_zone_is_4(self):
        matrix = qr.encode(b"hola")
        assert qr.rows(matrix) == qr.rows(matrix, quiet=4)

    def test_quiet_zero_returns_bare_matrix_rows(self):
        matrix = qr.encode(b"hola")
        r = qr.rows(matrix, quiet=0)
        assert r == ["".join(str(v) for v in row) for row in matrix]


# ---------------------------------------------------------------------------
# A11：回归夹具（负责人用系统识别器独立验证通过后落盘才会存在）
# ---------------------------------------------------------------------------

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "qr_known_good.json")


def test_known_good_fixture_regression():
    """夹具文件格式（供负责人生成时对照）：

        {"input": "<UTF-8 文本，encode() 的原始输入>",
         "min_version": 2, "max_version": 6,
         "matrix": [[0,1,...], ...]}   # qr.encode(...) 的原始返回值

    文件不存在就跳过——这是刻意的（A11）：先把测试写成这个形态，等负责人拿一台
    真手机/系统识别器扫过一次、确认能读出来之后，再把当时的矩阵存成夹具，
    这个测试才会真的跑起来钉住逐位回归。
    """
    if not os.path.exists(_FIXTURE_PATH):
        pytest.skip("tests/fixtures/qr_known_good.json 还不存在，等负责人独立验证后落盘")
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture = json.load(f)
    matrix = qr.encode(
        fixture["input"].encode("utf-8"),
        min_version=fixture.get("min_version", 2),
        max_version=fixture.get("max_version", 6),
    )
    assert matrix == fixture["matrix"]
