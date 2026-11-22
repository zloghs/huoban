"""生成掌阅弹注的注标图：`huoban/assets/note.png`。

一枚深灰圆牌（`#686868`）+ 白「注」字，无描边、无阴影、无渐变。

为什么是灰不是黑：纯黑在白纸页上太硬，像钉子钉在正文里；灰也只能深到这个程度 ——
白字压灰的对比本就比压黑上弱，再浅一档（`#808080`）缩到 16px 就糊成一团白雾。
位图认不出阅读器的深浅，按主题换图或整张反相都不可靠，所以只做这一种：灰在白纸页
上比纸深、在深色页上比背景浅，两种页面都站得住。

为什么不描边：0.9em（约 12~14px）下一根 1px 的边本就在断与不断之间，只会在小尺寸
上抖成一圈灰雾。

为什么必须真渲一个汉字：注标只能是图片（注文写在 `zy-footnote` 属性里，见
huoban/epub.py 的 `_note_mark_html`），图上写什么就等于告诉读者"这里点了有注"；
圆环加中心点那类几何形缩到 12px 只是一个脏点。字体取系统的「微软雅黑」粗体、退
「黑体」，400px 渲进 512 方框后按**墨迹包围盒**裁出 —— DrawText 的居中是对字身框
而不是墨迹，直接居中会偏下偏左几个像素。

降采样自己算（不依赖 Pillow / numpy）：面积加权等效一次盒式滤波，圆边另用 4×4 超
采样算 alpha。尺寸 96px：正文里显示成 0.9em，两三倍屏下仍有余量，文件也小。版面
占位不归这张图管 —— 样式表 `img.zhangyue-footnote` 定 0.9em × 0.9em、无内边距，
牌的直径就等于 0.9em，比一个汉字略窄。

用法（在项目根下）：

    python tools/make_note_mark.py

生成物已入库，只有想换注标时才需要重跑。
"""

from __future__ import annotations

import ctypes
import math
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

SIZE = 96               # 成品边长（px）
SUPERSAMPLE = 4         # 圆边 alpha 的超采样倍数
GLYPH_RATIO = 0.64      # 墨迹包围盒长边占成品边长的比例：留出牌面底色
PLATE_GRAY = 0x68       # 牌面底色（深灰）：白字压在纯黑上太"硬"，但再浅字就糊了
FONT_PX = 400           # 渲染字号（大字号下 GDI 的抗锯齿最干净，之后再缩）
FONT_BOX = 512          # 渲染画布边长
FONT_FACE = "Microsoft YaHei"   # 缺字时退到 SimHei，见 _render_glyph
FONT_FALLBACK = "SimHei"

# ---- 下面这一堆常量是 GDI 的，名字照抄 Win32 头文件，免得对着文档猜 ----
BI_RGB = 0
DIB_RGB_COLORS = 0
TRANSPARENT = 1
FW_BOLD = 700
GB2312_CHARSET = 134
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
ANTIALIASED_QUALITY = 5
DEFAULT_PITCH = 0
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


def _render_glyph(text: str) -> tuple[bytes, int, int]:
    """把 `text` 用系统字体渲成 32 位灰度图，返回 (像素, 宽, 高)。

    黑底白字直接渲：GDI 的抗锯齿在黑白之间算出的灰色恰好就是覆盖率，不用再
    反解 alpha。调用方按墨迹包围盒裁剪，所以这里只需要字落在画布里 —— 偏几个
    像素无所谓，反正会被重新居中。
    """
    gdi = ctypes.windll.gdi32
    user = ctypes.windll.user32

    screen = user.GetDC(None)
    if not screen:
        raise OSError("GetDC 失败")
    hdc = None
    hbm = None
    hfont = None
    try:
        hdc = gdi.CreateCompatibleDC(screen)
        if not hdc:
            raise OSError("CreateCompatibleDC 失败")

        info = _BITMAPINFO()
        head = info.bmiHeader
        head.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        head.biWidth = FONT_BOX
        head.biHeight = -FONT_BOX      # 负高 = 自上而下的行序，取像素时不用翻
        head.biPlanes = 1
        head.biBitCount = 32
        head.biCompression = BI_RGB

        bits = ctypes.c_void_p()
        hbm = gdi.CreateDIBSection(
            hdc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not hbm or not bits:
            raise OSError("CreateDIBSection 失败")
        gdi.SelectObject(hdc, hbm)
        # 底色直接清零，不用 PatBlt：ROP 码是魔术数，BLACKNESS(0x42) 与
        # WHITENESS(0x62) 只差一位，写反了会得到一张全白的图，而且画面上
        # 看不出是哪一步错的。DIB 段的内存就是这块缓冲区，清零即黑底。
        ctypes.memset(bits, 0, FONT_BOX * FONT_BOX * 4)

        gdi.SetBkMode(hdc, TRANSPARENT)     # 不画字背后的底色，白字直接压在黑上
        gdi.SetTextColor(hdc, 0x00FFFFFF)   # COLORREF 是 0x00BBGGRR
        flags = DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX

        picked = None
        for face in (FONT_FACE, FONT_FALLBACK):
            hfont = gdi.CreateFontW(
                -FONT_PX, 0, 0, 0, FW_BOLD, 0, 0, 0, GB2312_CHARSET,
                OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS, ANTIALIASED_QUALITY,
                DEFAULT_PITCH, ctypes.c_wchar_p(face),
            )
            if not hfont:
                continue
            gdi.SelectObject(hdc, hfont)
            box = wintypes.RECT(0, 0, FONT_BOX, FONT_BOX)
            # DrawText 在 user32 里（不在 gdi32），写错 dll 会得到一句
            # "function 'DrawTextW' not found"，而那正是最容易顺手写错的一处
            user.DrawTextW(
                hdc, ctypes.c_wchar_p(text), -1, ctypes.byref(box), flags
            )
            raw = ctypes.string_at(bits, FONT_BOX * FONT_BOX * 4)
            if any(raw[i] for i in range(0, len(raw), 4)):
                picked = raw
                break
            # 这个字在选中的字体里没有字形（渲出来一片黑），换下一个再试
            gdi.DeleteObject(hfont)
            hfont = None
        if picked is None:
            raise OSError(f"字体里找不到「{text}」的字形")

        # BGRA，取蓝通道即可：黑底白字的三通道恒等
        gray = bytes(picked[i] for i in range(0, len(picked), 4))
        return gray, FONT_BOX, FONT_BOX
    finally:
        if hfont:
            gdi.DeleteObject(hfont)
        if hbm:
            gdi.DeleteObject(hbm)
        if hdc:
            gdi.DeleteDC(hdc)
        user.ReleaseDC(None, screen)


def _ink_box(gray: bytes, w: int, h: int) -> tuple[int, int, int, int]:
    """墨迹包围盒 (x0, y0, x1, y1)，右下为开区间。阈值取 8：低于它的都是
    抗锯齿的边缘毛刺，算进去只会让字在圆牌上偏一点点。"""
    x0, y0, x1, y1 = w, h, 0, 0
    for y in range(h):
        row = gray[y * w:(y + 1) * w]
        cols = [x for x, v in enumerate(row) if v > 8]
        if not cols:
            continue
        y0 = min(y0, y)
        y1 = max(y1, y + 1)
        x0 = min(x0, cols[0])
        x1 = max(x1, cols[-1] + 1)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("渲出来的图是空的")
    return x0, y0, x1, y1


def _resample(gray: bytes, sw: int, sh: int, tw: int, th: int) -> bytearray:
    """按面积加权重采样（盒式滤波），返回 tw×th 的灰度。

    缩小倍数在 4~6 之间，最近邻会丢掉笔画之间的空隙，双线性只取 4 个点
    （在这本书的注标尺寸下等于没滤波），面积平均才既不糊也不闪。
    """
    out = bytearray(tw * th)
    for y in range(th):
        sy0, sy1 = y * sh / th, (y + 1) * sh / th
        for x in range(tw):
            sx0, sx1 = x * sw / tw, (x + 1) * sw / tw
            acc = 0.0
            wsum = 0.0
            for sy in range(int(sy0), min(sh, math.ceil(sy1))):
                wy = min(sy1, sy + 1.0) - max(sy0, float(sy))
                if wy <= 0:
                    continue
                base = sy * sw
                for sx in range(int(sx0), min(sw, math.ceil(sx1))):
                    wx = min(sx1, sx + 1.0) - max(sx0, float(sx))
                    if wx <= 0:
                        continue
                    w = wx * wy
                    acc += gray[base + sx] * w
                    wsum += w
            out[y * tw + x] = round(acc / wsum) if wsum else 0
    return out


def _coverage(inside) -> list[bytearray]:
    """对 SIZE 见方的每个像素做 4×4 超采样，返回 0~255 的覆盖率。

    圆边是一条把像素切开的曲线，只计算像素中心的内外关系会得到锯齿；
    4×4 超采样是在不依赖 Pillow 等图像库时最省事且够用的抗锯齿办法。
    """
    step = 1.0 / SUPERSAMPLE
    samples = SUPERSAMPLE * SUPERSAMPLE
    rows: list[bytearray] = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            hits = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = px + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    if inside(x, y):
                        hits += 1
            row.append(round(255 * hits / samples))
        rows.append(row)
    return rows


def _disc_alpha() -> list[bytearray]:
    """圆牌的 alpha：SIZE 见方的一个内切圆。圆牌之外的像素整个透明。"""
    radius = SIZE / 2.0

    def inside(x: float, y: float) -> bool:
        dx, dy = x - radius, y - radius
        return dx * dx + dy * dy <= radius * radius

    return _coverage(inside)


def _compose(gray: bytearray, tw: int, th: int) -> list[bytearray]:
    """把字摆进圆牌正中：牌面是深灰，字按覆盖率混到白。

    gray 是字在**黑底上**的白度（0 = 没墨，255 = 笔画中心），即"这个像素有多少
    笔画覆盖"。牌面底色给 PLATE_GRAY，字按覆盖率往白上插值 —— 笔画中心是纯白，
    抗锯齿的边落在灰与白之间，与阅读器把白字压灰底上看到的一样。
    圆牌之外也涂同色，反正 alpha 为 0，不会显示。
    """
    ox = (SIZE - tw) // 2
    oy = (SIZE - th) // 2
    plate = PLATE_GRAY
    span = 255 - plate
    rows: list[bytearray] = []
    for y in range(SIZE):
        row = bytearray()
        inside_row = oy <= y < oy + th
        for x in range(SIZE):
            v = plate
            if inside_row and ox <= x < ox + tw:
                g = gray[(y - oy) * tw + (x - ox)]
                if g:
                    v = plate + (g * span + 127) // 255
            row += bytes((v, v, v))
        rows.append(row)
    return rows


def _fallback_rows() -> list[bytearray]:
    """没有 GDI 时的退路：一块没有字的灰牌。

    与成品只差那个「注」：位置、大小、底色都一样，一眼能看出是没渲出来，而不是
    被当成了别的设计。stderr 也会说明。
    """
    rows = [bytearray(bytes((PLATE_GRAY,)) * SIZE * 3) for _ in range(SIZE)]
    return rows


def _png(rows: list[bytearray], alpha: list[bytearray]) -> bytes:
    """拼成 RGBA PNG：颜色来自字，透明度来自圆牌的形状。"""
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # 过滤器类型 None
        px = rows[y]
        for x in range(SIZE):
            i = x * 3
            raw += bytes((px[i], px[i + 1], px[i + 2], alpha[y][x]))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8 位、RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parent.parent
    out = Path(argv[0]) if argv else root / "huoban" / "assets" / "note.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        gray, gw, gh = _render_glyph("注")
    except Exception as exc:  # noqa: BLE001 —— 任何一步失败都走退路，不让工具崩
        print(f"渲染「注」字失败（{exc}），退到没有字的灰牌注标", file=sys.stderr)
        rows = _fallback_rows()
        alpha = _disc_alpha()
    else:
        x0, y0, x1, y1 = _ink_box(gray, gw, gh)
        bw, bh = x1 - x0, y1 - y0
        ink = round(SIZE * GLYPH_RATIO)
        scale = ink / max(bw, bh)
        tw, th = max(1, round(bw * scale)), max(1, round(bh * scale))
        # 先按包围盒裁掉四周空白，再整体缩放 —— 顺序反过来会把空白也缩进去，
        # 字就永远居中在"字身框"上而不是圆牌中心
        cropped = bytearray()
        for y in range(y0, y1):
            cropped += gray[y * gw + x0:y * gw + x1]
        rows = _compose(_resample(cropped, bw, bh, tw, th), tw, th)
        alpha = _disc_alpha()
        print(f"注字墨迹 {bw}×{bh} → 成品 {tw}×{th}（圆牌 {SIZE}px）")

    data = _png(rows, alpha)
    out.write_bytes(data)
    print(f"{out}  {len(data)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
