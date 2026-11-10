"""Small dependency-free QR Code generator.

Byte mode, error-correction level M, versions 1-6 (up to 106 characters — plenty for a web
address). Output as an SVG string or a PNG file. Verified in tests by decoding the output
with a real QR decoder.
"""
import struct
import zlib

# version -> (data codewords per block, error-correction codewords per block, number of blocks)
_M = {1: (16, 10, 1), 2: (28, 16, 1), 3: (44, 26, 1), 4: (32, 18, 2), 5: (43, 24, 2), 6: (27, 16, 4)}
_ALIGN = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}  # centre of the single alignment pattern
MAX_BYTES = max(d * b for d, _, b in _M.values()) - 2


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(divisor):
            result[i] ^= _gf_mul(c, factor)
    return result


def _penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(c) for c in zip(*m)]):
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
            s = "".join("1" if c else "0" for c in row)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(1 for c in row if c) for row in m)
    total = n * n
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def make(text, mask=None):
    """Return the QR matrix as a list of rows of booleans (True = dark)."""
    data = text.encode("utf-8")
    for ver in range(1, 7):
        dpb, ecpb, nb = _M[ver]
        if len(data) <= dpb * nb - 2:
            break
    else:
        raise ValueError(f"Web address is too long for a QR code (max {MAX_BYTES} characters).")
    size = ver * 4 + 17

    # ---- data bits -> codewords
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, dpb * nb * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    cw = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cw) < dpb * nb:
        cw.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks = [cw[i * dpb : (i + 1) * dpb] for i in range(nb)]
    div = _rs_divisor(ecpb)
    ecs = [_rs_remainder(b, div) for b in blocks]
    final = [b[i] for i in range(dpb) for b in blocks] + [e[i] for i in range(ecpb) for e in ecs]

    # ---- function patterns
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setfn(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True

    for i in range(size):
        setfn(6, i, i % 2 == 0)
        setfn(i, 6, i % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setfn(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)
    if ver in _ALIGN:
        c = _ALIGN[ver]
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                setfn(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(m_, mk):
        d = mk  # error-correction level M = 00, then 3 mask bits
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits_ = ((d << 10) | rem) ^ 0x5412

        def b(i):
            return (bits_ >> i) & 1 == 1

        def s(x, y, v):
            m_[y][x] = v
            fn[y][x] = True

        for i in range(0, 6):
            s(8, i, b(i))
        s(8, 7, b(6))
        s(8, 8, b(7))
        s(7, 8, b(8))
        for i in range(9, 15):
            s(14 - i, 8, b(i))
        for i in range(0, 8):
            s(size - 1 - i, 8, b(i))
        for i in range(8, 15):
            s(8, size - 15 + i, b(i))
        s(8, size - 8, True)

    format_bits(mod, 0)  # reserve the format areas

    # ---- place codewords in the zig-zag
    i = 0
    total_bits = len(final) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and i < total_bits:
                    mod[y][x] = ((final[i >> 3] >> (7 - (i & 7))) & 1) == 1
                    i += 1
        right -= 2

    # ---- choose mask
    best, best_score = None, None
    for mk in [mask] if mask is not None else range(8):
        m = [row[:] for row in mod]
        f = _MASKS[mk]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and f(x, y):
                    m[y][x] = not m[y][x]
        format_bits(m, mk)
        sc = _penalty(m)
        if best is None or sc < best_score:
            best, best_score = m, sc
    return best


def to_svg(m, scale=10, border=4):
    n = len(m)
    dim = n + 2 * border
    path = "".join(
        f"M{x + border},{y + border}h1v1h-1z" for y in range(n) for x in range(n) if m[y][x]
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" width="{dim * scale}" height="{dim * scale}" '
        f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="#000"/></svg>'
    ).encode()


def to_png(m, scale=12, border=4):
    n = len(m)
    dim = (n + 2 * border) * scale
    rows = bytearray()
    for y in range(-border, n + border):
        line = bytearray()
        for x in range(-border, n + border):
            dark = 0 <= x < n and 0 <= y < n and m[y][x]
            line += (b"\x00" if dark else b"\xff") * scale
        rows += (b"\x00" + bytes(line)) * scale

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
