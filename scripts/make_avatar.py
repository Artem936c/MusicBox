"""Генерация аватарки бота MusicBox: векторный SVG и растровый PNG 512x512.

Запуск из корня проекта:

    python -m scripts.make_avatar

Результат:

    assets/bot_avatar.svg   — векторный эскиз (скрипичный ключ на градиенте);
    assets/bot_avatar.png   — тот же рисунок, 512x512, готов для @BotFather.

Важно: в Telegram Bot API НЕТ метода установки фото профиля бота
(`setMyPhoto` не существует, а `setChatPhoto` работает только для чатов).
Готовый файл ставится вручную: @BotFather -> /setuserpic. Подсказку печатает
`python -m scripts.setup_profile`.

Зависимости: скрипт работает на чистой стандартной библиотеке — Pillow,
cairosvg и прочие пакеты НЕ обязательны. Геометрия описана в коде
(`CLEF_STROKES`), из неё строятся контуры, а из контуров — и SVG, и PNG,
поэтому оба файла гарантированно совпадают по форме.

Если Pillow всё же установлен, он используется для растеризации и
сглаживания (чуть быстрее и мягче края); ключ `--renderer` позволяет
выбрать движок принудительно.
"""

from __future__ import annotations

import argparse
import logging
import math
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger("scripts.make_avatar")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PNG = PROJECT_ROOT / "assets" / "bot_avatar.png"
DEFAULT_SVG = PROJECT_ROOT / "assets" / "bot_avatar.svg"

#: Сторона картинки по умолчанию. Telegram принимает квадрат от 150 px.
DEFAULT_SIZE = 512
MIN_SIZE = 128
MAX_SIZE = 2048

#: Кратность суперсэмплинга при растеризации (сглаживание краёв).
SUPERSAMPLE = 4

#: SVG всегда пишется в системе координат 512x512 (масштабируется как вектор).
SVG_SIZE = 512

Point = tuple[float, float]
Rgb = tuple[int, int, int]

# ---------------------------------------------------------------------------
# Палитра и композиция
# ---------------------------------------------------------------------------

#: Диагональный градиент фона: фиолетовый -> индиго -> бирюзовый.
BG_STOPS: tuple[tuple[float, Rgb], ...] = (
    (0.00, (0x7C, 0x3A, 0xED)),
    (0.52, (0x4F, 0x46, 0xE5)),
    (1.00, (0x06, 0xB6, 0xD4)),
)

#: Мягкая засветка в левом верхнем углу: центр (доли стороны), радиус, сила.
GLOW_CENTER: Point = (0.30, 0.22)
GLOW_RADIUS = 0.82
GLOW_ALPHA = 0.30

#: Тонкое кольцо у края — рамка, которая остаётся видимой при круглой обрезке.
RING_RADIUS = 0.452
RING_WIDTH = 0.0090
RING_ALPHA = 0.18

#: Тень под ключом: смещение (доли стороны), радиус размытия, сила.
SHADOW_OFFSET: Point = (0.000, 0.022)
SHADOW_BLUR = 0.024
SHADOW_ALPHA = 0.34

#: Заливка ключа — вертикальный градиент от белого к холодному светлому.
CLEF_TOP: Rgb = (0xFF, 0xFF, 0xFF)
CLEF_BOTTOM: Rgb = (0xDC, 0xE7, 0xFF)

#: Свободное поле вокруг ключа (доля стороны с каждого края).
CLEF_MARGIN = 0.105

#: Шаг разбиения кривых Безье в единицах эскиза (~1.6 px при стороне 512).
FLATTEN_STEP = 1.0


@dataclass(frozen=True, slots=True)
class StrokeNode:
    """Узел осевой линии штриха.

    :param x: координата X в системе эскиза (0..100).
    :param y: координата Y в системе эскиза (0..260, ось направлена вниз).
    :param r: половина толщины линии в этом узле.
    :param cin: управляющая точка Безье со стороны предыдущего узла.
    :param cout: управляющая точка Безье со стороны следующего узла.
    """

    x: float
    y: float
    r: float
    cin: Point | None = None
    cout: Point | None = None

    @property
    def point(self) -> Point:
        return (self.x, self.y)


#: Нижняя петля («брюшко») скрипичного ключа. Спираль раскручивается по часовой
#: стрелке от кончика внутри петли: короткий завиток вверх-влево, затем полный
#: внешний виток (верх — право — низ — лево) и выход в восходящую дугу.
#: Между завитком и внешним витком остаётся узкая щель — «глазок» спирали.
_CLEF_BELLY: tuple[StrokeNode, ...] = (
    StrokeNode(36.0, 182.0, 1.8, cout=(31.0, 180.0)),
    StrokeNode(28.0, 171.0, 3.6, cin=(27.0, 178.0), cout=(29.0, 163.0)),
    StrokeNode(28.0, 150.0, 5.2, cin=(24.7, 156.7), cout=(31.6, 142.8)),
    StrokeNode(48.0, 140.0, 6.4, cin=(39.0, 139.0), cout=(64.0, 141.0)),
    StrokeNode(80.0, 178.0, 8.0, cin=(78.0, 158.0), cout=(82.0, 199.0)),
    StrokeNode(47.0, 212.0, 9.0, cin=(64.0, 213.0), cout=(28.0, 211.0)),
    StrokeNode(10.0, 184.0, 8.4, cin=(10.0, 201.0), cout=(9.8, 175.0)),
    StrokeNode(8.5, 156.0, 7.8, cin=(9.5, 170.0)),
)

#: Восходящая дуга, вершина и штиль с каплей внизу — вторая половина ключа.
#: Первый узел совпадает с последним узлом петли, поэтому стык незаметен.
_CLEF_SPINE: tuple[StrokeNode, ...] = (
    StrokeNode(8.5, 156.0, 7.8, cout=(8.4, 139.0)),
    StrokeNode(8.0, 108.0, 7.6, cin=(6.7, 130.0), cout=(9.1, 90.0)),
    StrokeNode(27.0, 50.0, 5.8, cin=(17.0, 70.7), cout=(34.4, 34.7)),
    StrokeNode(48.0, 14.0, 3.6, cin=(39.0, 17.0), cout=(57.0, 11.0)),
    StrokeNode(60.0, 44.0, 4.4, cin=(63.0, 20.0), cout=(58.0, 64.0)),
    StrokeNode(56.0, 116.0, 5.8, cin=(59.0, 76.0), cout=(54.0, 152.0)),
    StrokeNode(48.0, 188.0, 5.6, cin=(52.0, 155.0), cout=(45.0, 218.0)),
    StrokeNode(36.0, 236.0, 4.4, cin=(40.4, 230.6), cout=(31.6, 241.4)),
    StrokeNode(24.0, 246.0, 9.0, cin=(29.0, 247.0)),
)

#: Штрихи ключа. Каждый рисуется отдельным контуром, объединение — по максимуму
#: покрытия: осевая линия ключа пересекает сама себя, и единый контур с
#: чётно-нечётной заливкой дал бы дырки в местах пересечений.
CLEF_STROKES: tuple[tuple[StrokeNode, ...], ...] = (_CLEF_BELLY, _CLEF_SPINE)


class AvatarError(RuntimeError):
    """Ошибка генерации аватарки с готовым русским описанием."""


# ---------------------------------------------------------------------------
# Геометрия: кривые -> осевая линия -> замкнутый контур
# ---------------------------------------------------------------------------


def _cubic(p0: Point, c1: Point, c2: Point, p3: Point, t: float) -> Point:
    """Точка кубической кривой Безье при параметре `t` (0..1)."""
    u = 1.0 - t
    a, b, c, d = u * u * u, 3.0 * u * u * t, 3.0 * u * t * t, t * t * t
    return (
        a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0],
        a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1],
    )


def _control_polygon_length(p0: Point, c1: Point, c2: Point, p3: Point) -> float:
    """Длина контрольной ломаной — верхняя оценка длины кривой."""
    total = 0.0
    prev = p0
    for point in (c1, c2, p3):
        total += math.hypot(point[0] - prev[0], point[1] - prev[1])
        prev = point
    return total


def _flatten_stroke(stroke: Sequence[StrokeNode]) -> list[tuple[float, float, float]]:
    """Разбить штрих на точки осевой линии `(x, y, r)` в системе эскиза.

    Управляющие точки, не заданные явно, заменяются концами сегмента —
    тогда участок вырождается в прямую.
    """
    if len(stroke) < 2:
        raise AvatarError("В штрихе должно быть не менее двух узлов — проверьте CLEF_STROKES.")

    samples: list[tuple[float, float, float]] = []
    for index in range(len(stroke) - 1):
        start, end = stroke[index], stroke[index + 1]
        p0, p3 = start.point, end.point
        c1 = start.cout or p0
        c2 = end.cin or p3
        steps = max(6, int(_control_polygon_length(p0, c1, c2, p3) / FLATTEN_STEP))
        for step in range(steps + 1):
            if index and step == 0:
                continue  # общий узел уже добавлен предыдущим сегментом
            t = step / steps
            x, y = _cubic(p0, c1, c2, p3, t)
            samples.append((x, y, start.r + (end.r - start.r) * t))

    return _dedupe(samples)


def _dedupe(samples: Sequence[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Убрать совпадающие подряд точки: без них не вычислить касательную."""
    result: list[tuple[float, float, float]] = []
    for sample in samples:
        if result:
            prev = result[-1]
            if math.hypot(sample[0] - prev[0], sample[1] - prev[1]) < 1e-9:
                continue
        result.append(sample)
    if len(result) < 2:
        raise AvatarError("Штрих выродился в точку — проверьте координаты узлов.")
    return result


def _cap(centre: Point, radius: float, normal: Point, tangent: Point, *, forward: bool) -> list[Point]:
    """Полуокружность скруглённого конца штриха (без крайних точек)."""
    steps = max(6, int(radius * 1.6))
    sign = 1.0 if forward else -1.0
    points: list[Point] = []
    for step in range(1, steps):
        angle = math.pi * step / steps
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        points.append(
            (
                centre[0] + radius * sign * (cos_a * normal[0] + sin_a * tangent[0]),
                centre[1] + radius * sign * (cos_a * normal[1] + sin_a * tangent[1]),
            )
        )
    return points


def _stroke_outline(samples: Sequence[tuple[float, float, float]]) -> list[Point]:
    """Построить замкнутый контур штриха переменной толщины со скруглёнными концами."""
    count = len(samples)
    left: list[Point] = []
    right: list[Point] = []
    normals: list[Point] = []
    tangents: list[Point] = []

    for index, (x, y, radius) in enumerate(samples):
        prev_x, prev_y, _ = samples[max(index - 1, 0)]
        next_x, next_y, _ = samples[min(index + 1, count - 1)]
        tx, ty = next_x - prev_x, next_y - prev_y
        length = math.hypot(tx, ty) or 1.0
        tx, ty = tx / length, ty / length
        nx, ny = -ty, tx
        tangents.append((tx, ty))
        normals.append((nx, ny))
        left.append((x + nx * radius, y + ny * radius))
        right.append((x - nx * radius, y - ny * radius))

    end = samples[-1]
    start = samples[0]
    outline = list(left)
    outline += _cap((end[0], end[1]), end[2], normals[-1], tangents[-1], forward=True)
    outline += list(reversed(right))
    outline += _cap((start[0], start[1]), start[2], normals[0], tangents[0], forward=False)
    return outline


def _design_bounds() -> tuple[float, float, float, float]:
    """Габариты рисунка в системе эскиза с учётом толщины линий."""
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for stroke in CLEF_STROKES:
        for x, y, radius in _flatten_stroke(stroke):
            min_x = min(min_x, x - radius)
            max_x = max(max_x, x + radius)
            min_y = min(min_y, y - radius)
            max_y = max(max_y, y + radius)
    return min_x, min_y, max_x, max_y


def _validate_strokes() -> None:
    """Проверить, что петля и восходящая дуга стыкуются в одном узле.

    Штрихи рисуются отдельно, и рассинхронизация координат превратила бы
    плавный переход в разрыв. Проверка ловит это при правке `CLEF_STROKES`.
    """
    tail, head = _CLEF_BELLY[-1], _CLEF_SPINE[0]
    if (tail.x, tail.y, tail.r) != (head.x, head.y, head.r):
        raise AvatarError(
            "Штрихи ключа не стыкуются: последний узел петли "
            f"({tail.x}, {tail.y}, r={tail.r}) не совпадает с первым узлом дуги "
            f"({head.x}, {head.y}, r={head.r}). Приведите их к общим координатам."
        )


def build_polygons(size: int) -> list[list[Point]]:
    """Контуры ключа в пикселях холста `size` x `size`, отцентрованные по полю."""
    _validate_strokes()
    min_x, min_y, max_x, max_y = _design_bounds()
    width, height = max_x - min_x, max_y - min_y
    box = size * (1.0 - 2.0 * CLEF_MARGIN)
    scale = min(box / width, box / height)
    offset_x = (size - width * scale) / 2.0 - min_x * scale
    offset_y = (size - height * scale) / 2.0 - min_y * scale

    polygons: list[list[Point]] = []
    for stroke in CLEF_STROKES:
        samples = [
            (x * scale + offset_x, y * scale + offset_y, r * scale)
            for x, y, r in _flatten_stroke(stroke)
        ]
        polygons.append(_stroke_outline(samples))
    return polygons


# ---------------------------------------------------------------------------
# Растеризация
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Edge:
    """Ребро контура, подготовленное для построчной заливки."""

    y_min: float
    y_max: float
    x_at_y_min: float
    slope: float


def _edges(polygon: Sequence[Point]) -> list[_Edge]:
    """Невырожденные по вертикали рёбра замкнутого контура."""
    edges: list[_Edge] = []
    count = len(polygon)
    for index in range(count):
        x0, y0 = polygon[index]
        x1, y1 = polygon[(index + 1) % count]
        if y0 == y1:
            continue
        if y0 > y1:
            x0, y0, x1, y1 = x1, y1, x0, y0
        edges.append(_Edge(y0, y1, x0, (x1 - x0) / (y1 - y0)))
    return edges


def rasterize_builtin(polygons: Sequence[Sequence[Point]], size: int) -> list[float]:
    """Растеризовать контуры в маску покрытия 0..1 средствами стандартной библиотеки.

    По вертикали используется суперсэмплинг, по горизонтали — точное покрытие
    пикселя интервалом заливки. Контуры объединяются по максимуму, поэтому
    их пересечения не «прожигают» дырки.
    """
    mask = [0.0] * (size * size)
    samples = SUPERSAMPLE
    weight = 1.0 / samples
    rows = size * samples

    for polygon in polygons:
        edges = _edges(polygon)
        if not edges:
            continue
        buckets: list[list[_Edge]] = [[] for _ in range(rows + 1)]
        for edge in edges:
            row = min(max(int(edge.y_min * samples), 0), rows)
            buckets[row].append(edge)

        active: list[_Edge] = []
        row_cover = [0.0] * size
        current_row = 0
        for row in range(rows):
            out_row = row // samples
            if out_row != current_row:
                _merge_row(mask, row_cover, current_row, size)
                row_cover = [0.0] * size
                current_row = out_row

            active.extend(buckets[row])
            scan_y = (row + 0.5) / samples
            if active:
                active = [edge for edge in active if edge.y_max > scan_y]
            if not active:
                continue

            crossings = [
                edge.x_at_y_min + (scan_y - edge.y_min) * edge.slope
                for edge in active
                if edge.y_min <= scan_y < edge.y_max
            ]
            if len(crossings) < 2:
                continue
            crossings.sort()
            for pair in range(0, len(crossings) - 1, 2):
                _accumulate_span(row_cover, crossings[pair], crossings[pair + 1], weight, size)

        _merge_row(mask, row_cover, current_row, size)

    return mask


def _accumulate_span(cover: list[float], x0: float, x1: float, weight: float, size: int) -> None:
    """Добавить в строку покрытия горизонтальный отрезок `[x0, x1)`."""
    if x1 <= 0.0 or x0 >= size or x1 <= x0:
        return
    x0 = max(x0, 0.0)
    x1 = min(x1, float(size))
    first = int(x0)
    last = min(int(x1), size - 1)
    if first == last:
        cover[first] += (x1 - x0) * weight
        return
    cover[first] += (first + 1 - x0) * weight
    for index in range(first + 1, last):
        cover[index] += weight
    cover[last] += (x1 - last) * weight


def _merge_row(mask: list[float], cover: Sequence[float], row: int, size: int) -> None:
    """Объединить строку покрытия с общей маской по максимуму."""
    base = row * size
    for index, value in enumerate(cover):
        if value <= 0.0:
            continue
        clamped = 1.0 if value > 1.0 else value
        if clamped > mask[base + index]:
            mask[base + index] = clamped


def rasterize_pillow(polygons: Sequence[Sequence[Point]], size: int) -> list[float]:
    """Растеризовать контуры через Pillow (суперсэмплинг + фильтр Ланцоша)."""
    from PIL import Image, ImageDraw  # noqa: PLC0415 — мягкий импорт по требованию

    scale = SUPERSAMPLE
    image = Image.new("L", (size * scale, size * scale), 0)
    draw = ImageDraw.Draw(image)
    for polygon in polygons:
        draw.polygon([(x * scale, y * scale) for x, y in polygon], fill=255)
    resampling = getattr(Image, "Resampling", Image)
    image = image.resize((size, size), resampling.LANCZOS)
    return [value / 255.0 for value in image.getdata()]


def pillow_available() -> bool:
    """Проверить, установлен ли Pillow, не поднимая исключение."""
    try:
        import PIL  # noqa: F401, PLC0415 — только проверка наличия
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Композиция кадра
# ---------------------------------------------------------------------------


def _box_blur(mask: Sequence[float], size: int, radius: int) -> list[float]:
    """Тройное блочное размытие маски — дешёвое приближение гауссова."""
    if radius <= 0:
        return list(mask)
    data = list(mask)
    for _ in range(3):
        data = _blur_pass(data, size, radius)
        data = _transpose(data, size)
        data = _blur_pass(data, size, radius)
        data = _transpose(data, size)
    return data


def _blur_pass(data: Sequence[float], size: int, radius: int) -> list[float]:
    """Скользящее среднее по строкам с зажатием на краях."""
    window = radius * 2 + 1
    result = [0.0] * (size * size)
    for row in range(size):
        base = row * size
        total = data[base] * (radius + 1)
        for index in range(1, radius + 1):
            total += data[base + min(index, size - 1)]
        for column in range(size):
            result[base + column] = total / window
            total -= data[base + max(column - radius, 0)]
            total += data[base + min(column + radius + 1, size - 1)]
    return result


def _transpose(data: Sequence[float], size: int) -> list[float]:
    """Транспонировать квадратную матрицу, разложенную по строкам."""
    result = [0.0] * (size * size)
    for row in range(size):
        base = row * size
        for column in range(size):
            result[column * size + row] = data[base + column]
    return result


def _shift(mask: Sequence[float], size: int, dx: int, dy: int) -> list[float]:
    """Сдвинуть маску на целое число пикселей, дополняя нулями."""
    if dx == 0 and dy == 0:
        return list(mask)
    result = [0.0] * (size * size)
    for row in range(size):
        source_row = row - dy
        if not 0 <= source_row < size:
            continue
        src = source_row * size
        dst = row * size
        for column in range(size):
            source_column = column - dx
            if 0 <= source_column < size:
                result[dst + column] = mask[src + source_column]
    return result


def _gradient_table(size: int) -> list[Rgb]:
    """Цвета диагонального градиента для всех значений `x + y`."""
    span = 2 * (size - 1)
    table: list[Rgb] = []
    for index in range(span + 1):
        table.append(_gradient_color(index / span if span else 0.0))
    return table


def _gradient_color(t: float) -> Rgb:
    """Цвет фона в точке `t` (0..1) вдоль диагонали."""
    t = min(max(t, 0.0), 1.0)
    previous = BG_STOPS[0]
    for stop in BG_STOPS[1:]:
        if t <= stop[0]:
            width = stop[0] - previous[0] or 1.0
            k = (t - previous[0]) / width
            return (
                round(previous[1][0] + (stop[1][0] - previous[1][0]) * k),
                round(previous[1][1] + (stop[1][1] - previous[1][1]) * k),
                round(previous[1][2] + (stop[1][2] - previous[1][2]) * k),
            )
        previous = stop
    return BG_STOPS[-1][1]


def compose(size: int, clef: Sequence[float]) -> bytes:
    """Собрать итоговый кадр RGB: фон, засветка, кольцо, тень и ключ."""
    blur_radius = max(1, round(SHADOW_BLUR * size))
    shadow = _box_blur(
        _shift(clef, size, round(SHADOW_OFFSET[0] * size), round(SHADOW_OFFSET[1] * size)),
        size,
        blur_radius,
    )

    table = _gradient_table(size)
    glow_x, glow_y = GLOW_CENTER[0] * size, GLOW_CENTER[1] * size
    glow_r = GLOW_RADIUS * size
    glow_r2 = glow_r * glow_r
    centre = (size - 1) / 2.0
    ring_r = RING_RADIUS * size
    ring_half = RING_WIDTH * size / 2.0
    ring_outer = (ring_r + ring_half + 1.0) ** 2
    ring_inner = max(ring_r - ring_half - 1.0, 0.0) ** 2

    pixels = bytearray(size * size * 3)
    cursor = 0
    for y in range(size):
        glow_dy2 = (y - glow_y) ** 2
        ring_dy2 = (y - centre) ** 2
        clef_t = y / (size - 1) if size > 1 else 0.0
        clef_r = CLEF_TOP[0] + (CLEF_BOTTOM[0] - CLEF_TOP[0]) * clef_t
        clef_g = CLEF_TOP[1] + (CLEF_BOTTOM[1] - CLEF_TOP[1]) * clef_t
        clef_b = CLEF_TOP[2] + (CLEF_BOTTOM[2] - CLEF_TOP[2]) * clef_t
        row = y * size
        for x in range(size):
            red, green, blue = table[x + y]
            red = float(red)
            green = float(green)
            blue = float(blue)

            glow_d2 = (x - glow_x) ** 2 + glow_dy2
            if glow_d2 < glow_r2:
                k = (1.0 - math.sqrt(glow_d2) / glow_r) ** 2 * GLOW_ALPHA
                red += (255.0 - red) * k
                green += (255.0 - green) * k
                blue += (255.0 - blue) * k

            ring_d2 = (x - centre) ** 2 + ring_dy2
            if ring_inner < ring_d2 < ring_outer:
                edge = abs(math.sqrt(ring_d2) - ring_r)
                k = min(max(ring_half + 0.5 - edge, 0.0), 1.0) * RING_ALPHA
                if k > 0.0:
                    red += (255.0 - red) * k
                    green += (255.0 - green) * k
                    blue += (255.0 - blue) * k

            index = row + x
            shade = shadow[index] * SHADOW_ALPHA
            if shade > 0.0:
                keep = 1.0 - shade
                red *= keep
                green *= keep
                blue *= keep

            alpha = clef[index]
            if alpha > 0.0:
                inverse = 1.0 - alpha
                red = red * inverse + clef_r * alpha
                green = green * inverse + clef_g * alpha
                blue = blue * inverse + clef_b * alpha

            pixels[cursor] = _byte(red)
            pixels[cursor + 1] = _byte(green)
            pixels[cursor + 2] = _byte(blue)
            cursor += 3

    return bytes(pixels)


def _byte(value: float) -> int:
    """Округлить и зажать значение канала в диапазон 0..255."""
    result = int(value + 0.5)
    if result < 0:
        return 0
    if result > 255:
        return 255
    return result


# ---------------------------------------------------------------------------
# Запись PNG
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """Собрать чанк PNG вместе с контрольной суммой."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int, pixels: bytes) -> None:
    """Записать 8-битный RGB PNG без внешних зависимостей."""
    expected = size * size * 3
    if len(pixels) != expected:
        raise AvatarError(
            f"Внутренняя ошибка: получено {len(pixels)} байт пикселей вместо {expected}."
        )

    stride = size * 3
    raw = bytearray()
    for row in range(size):
        raw.append(0)  # тип фильтра строки: None
        raw += pixels[row * stride : (row + 1) * stride]

    body = b"\x89PNG\r\n\x1a\n"
    body += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    body += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    body += _png_chunk(b"IEND", b"")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def verify_png(path: Path, size: int) -> tuple[int, int]:
    """Прочитать заголовок готового файла и убедиться, что это корректный PNG."""
    data = path.read_bytes()
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AvatarError(f"Файл {path} не является PNG — генерация не удалась.")
    if data[12:16] != b"IHDR":
        raise AvatarError(f"В файле {path} отсутствует заголовок IHDR.")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (size, size):
        raise AvatarError(
            f"Ожидался размер {size}x{size}, а в файле {path} записано {width}x{height}."
        )
    if data[-8:-4] != b"IEND":
        raise AvatarError(f"Файл {path} обрывается — нет завершающего чанка IEND.")

    if pillow_available():
        from PIL import Image  # noqa: PLC0415 — мягкий импорт, только для проверки

        try:
            with Image.open(path) as image:
                image.load()
                actual = image.size
        except OSError as error:
            raise AvatarError(f"Pillow не смог открыть готовый файл {path}: {error}") from error
        if actual != (size, size):
            raise AvatarError(f"Pillow прочитал {actual[0]}x{actual[1]} вместо {size}x{size}.")
    return width, height


# ---------------------------------------------------------------------------
# Запись SVG
# ---------------------------------------------------------------------------


def _path_data(polygon: Iterable[Point]) -> str:
    """Перевести контур в атрибут `d` SVG-пути."""
    parts: list[str] = []
    for index, (x, y) in enumerate(polygon):
        parts.append(f"{'M' if index == 0 else 'L'}{x:.2f} {y:.2f}")
    parts.append("Z")
    return "".join(parts)


def build_svg(size: int = SVG_SIZE) -> str:
    """Собрать SVG с тем же рисунком, что и PNG."""
    polygons = build_polygons(size)
    bg_stops = "".join(
        f'\n      <stop offset="{offset:.2f}" stop-color="{_hex(color)}"/>'
        for offset, color in BG_STOPS
    )
    glow_stops = "".join(
        f'\n      <stop offset="{offset:.2f}" stop-color="#ffffff" '
        f'stop-opacity="{(1.0 - offset) ** 2 * GLOW_ALPHA:.3f}"/>'
        for offset in (0.0, 0.35, 0.7, 1.0)
    )
    paths = "\n    ".join(f'<path d="{_path_data(polygon)}"/>' for polygon in polygons)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Аватарка бота MusicBox. Файл создаётся скриптом scripts/make_avatar.py —
     правьте геометрию там (CLEF_STROKES), а не здесь. -->
<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"
     viewBox="0 0 {size} {size}" role="img" aria-label="Скрипичный ключ MusicBox">
  <title>MusicBox</title>
  <defs>
    <linearGradient id="mb-bg" gradientUnits="userSpaceOnUse"
                    x1="0" y1="0" x2="{size}" y2="{size}">{bg_stops}
    </linearGradient>
    <radialGradient id="mb-glow" gradientUnits="userSpaceOnUse"
                    cx="{GLOW_CENTER[0] * size:.1f}" cy="{GLOW_CENTER[1] * size:.1f}"
                    r="{GLOW_RADIUS * size:.1f}">{glow_stops}
    </radialGradient>
    <linearGradient id="mb-clef" gradientUnits="userSpaceOnUse"
                    x1="0" y1="0" x2="0" y2="{size}">
      <stop offset="0" stop-color="{_hex(CLEF_TOP)}"/>
      <stop offset="1" stop-color="{_hex(CLEF_BOTTOM)}"/>
    </linearGradient>
    <filter id="mb-shadow" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="{SHADOW_OFFSET[0] * size:.1f}" dy="{SHADOW_OFFSET[1] * size:.1f}"
                    stdDeviation="{SHADOW_BLUR * size * 0.6:.1f}"
                    flood-color="#000000" flood-opacity="{SHADOW_ALPHA:.2f}"/>
    </filter>
  </defs>
  <rect width="{size}" height="{size}" fill="url(#mb-bg)"/>
  <rect width="{size}" height="{size}" fill="url(#mb-glow)"/>
  <circle cx="{size / 2:.1f}" cy="{size / 2:.1f}" r="{RING_RADIUS * size:.1f}"
          fill="none" stroke="#ffffff" stroke-opacity="{RING_ALPHA:.2f}"
          stroke-width="{RING_WIDTH * size:.2f}"/>
  <g fill="url(#mb-clef)" filter="url(#mb-shadow)">
    {paths}
  </g>
</svg>
"""


def _hex(color: Rgb) -> str:
    """Цвет в формате `#rrggbb`."""
    return "#{:02x}{:02x}{:02x}".format(*color)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


NEXT_STEPS = (
    "Что дальше:\n"
    "  1. Откройте @BotFather и отправьте команду /setuserpic\n"
    "  2. Выберите своего бота в списке\n"
    "  3. Отправьте файл assets/bot_avatar.png как ФОТО (не как документ)\n"
    "  4. Имя и описания бота ставит скрипт: python -m scripts.setup_profile\n"
    "Метода установки аватарки в Bot API нет, шаг 1-3 выполняется вручную."
)

PILLOW_HINT = (
    "Pillow не установлен — рисунок собран встроенным растеризатором "
    "(результат корректный, только считается чуть дольше).\n"
    "Хотите более мягкое сглаживание — установите Pillow:\n"
    "    .venv/Scripts/python.exe -m pip install Pillow\n"
    "Устанавливать не обязательно: готовые assets/bot_avatar.png и "
    "assets/bot_avatar.svg можно сразу отдать @BotFather."
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.make_avatar",
        description="Генерирует аватарку бота MusicBox: assets/bot_avatar.svg и assets/bot_avatar.png.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=NEXT_STEPS,
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_PNG, help="путь к PNG (по умолчанию assets/bot_avatar.png)")
    parser.add_argument("--svg-out", type=Path, default=DEFAULT_SVG, help="путь к SVG (по умолчанию assets/bot_avatar.svg)")
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        help=f"сторона PNG в пикселях, {MIN_SIZE}..{MAX_SIZE} (по умолчанию {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--renderer",
        choices=("auto", "pillow", "builtin"),
        default="auto",
        help="движок растеризации: auto — Pillow, если он установлен (по умолчанию auto)",
    )
    parser.add_argument("--skip-png", action="store_true", help="не создавать PNG")
    parser.add_argument("--skip-svg", action="store_true", help="не создавать SVG")
    return parser.parse_args(argv)


def _select_renderer(choice: str) -> str:
    """Выбрать движок растеризации и сообщить об этом пользователю."""
    if choice == "pillow":
        if not pillow_available():
            raise AvatarError(
                "Запрошен движок Pillow, но пакет не установлен.\n"
                "Установите его командой:\n"
                "    .venv/Scripts/python.exe -m pip install Pillow\n"
                "либо запустите без ключа --renderer: встроенный растеризатор "
                "работает без внешних зависимостей."
            )
        return "pillow"
    if choice == "builtin":
        return "builtin"
    if pillow_available():
        return "pillow"
    logger.info("%s", PILLOW_HINT)
    return "builtin"


def main(argv: Sequence[str] | None = None) -> int:
    """Сгенерировать SVG и PNG. Возвращает код выхода процесса."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    args = _parse_args(argv)

    try:
        if not MIN_SIZE <= args.size <= MAX_SIZE:
            raise AvatarError(
                f"Недопустимый размер {args.size}: укажите значение от {MIN_SIZE} до {MAX_SIZE}."
            )
        if args.skip_png and args.skip_svg:
            raise AvatarError("Указаны сразу --skip-png и --skip-svg — генерировать нечего.")

        if not args.skip_svg:
            svg_path: Path = args.svg_out
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            svg_path.write_text(build_svg(), encoding="utf-8")
            logger.info("SVG готов: %s (%d байт)", svg_path, svg_path.stat().st_size)

        if not args.skip_png:
            renderer = _select_renderer(args.renderer)
            started = time.perf_counter()
            polygons = build_polygons(args.size)
            mask = (
                rasterize_pillow(polygons, args.size)
                if renderer == "pillow"
                else rasterize_builtin(polygons, args.size)
            )
            pixels = compose(args.size, mask)
            write_png(args.out, args.size, pixels)
            width, height = verify_png(args.out, args.size)
            logger.info(
                "PNG готов: %s (%dx%d, %d байт, движок %s, %.1f с)",
                args.out,
                width,
                height,
                args.out.stat().st_size,
                renderer,
                time.perf_counter() - started,
            )
    except AvatarError as error:
        logger.error("%s", error)
        return 1
    except OSError as error:
        logger.error("Не удалось записать файл: %s", error)
        return 1

    logger.info("%s", NEXT_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
