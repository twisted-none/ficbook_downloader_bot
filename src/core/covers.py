"""Conservative Ficbook placeholder detection and local typographic covers."""

from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont

from src.core.limits import (LimitedBytesIO, ResourceLimitError, check_deadline,
                             current_limits, image_is_safe)
from src.core.models import CoverImage, Story


# 144 adjacent-pixel comparisons from the confirmed 510x228 Ficbook share banner.
# Aspect ratio alone must never discard an author's landscape cover.
_FICBOOK_BANNER = 0xd36191475cb996b6a5e6a48888f88d6d3d48
_FONT_PAIRS = (
    ('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf'),
    ('C:/Windows/Fonts/times.ttf', 'C:/Windows/Fonts/timesbd.ttf'),
)


def is_ficbook_placeholder(cover: CoverImage) -> bool:
    if not image_is_safe(cover.data):
        return False
    try:
        with Image.open(BytesIO(cover.data)) as image:
            if abs(image.width / image.height - 510 / 228) > 0.07:
                return False
            pixels = list(image.convert('L').resize((17, 9), Image.Resampling.LANCZOS).get_flattened_data())
        fingerprint = 0
        for row in range(9):
            for column in range(16):
                offset = row * 17 + column
                fingerprint = (fingerprint << 1) | (pixels[offset] > pixels[offset + 1])
        return (fingerprint ^ _FICBOOK_BANNER).bit_count() <= 4
    except (OSError, ValueError):
        return False


def cover_for_story(story: Story, *, enabled: bool = True) -> CoverImage | None:
    if not enabled:
        return None
    is_ficbook = urlsplit(story.source_url).hostname in {'ficbook.net', 'www.ficbook.net', 'ficbook.com', 'www.ficbook.com'}
    if story.cover is not None and not (is_ficbook and is_ficbook_placeholder(story.cover)):
        return story.cover
    return text_cover(story.title, story.author, story.translator)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for pair in _FONT_PAIRS:
        path = pair[int(bold)]
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise ResourceLimitError('Не удалось создать обложку: отсутствует шрифт с поддержкой кириллицы.')


def _width(draw, text, font):
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _wrap(draw, text, font, width):
    lines = []
    line = ''
    for word in text.split():
        check_deadline()
        if _width(draw, word, font) > width:
            if line:
                lines.append(line)
                line = ''
            for char in word:
                if line and _width(draw, line + char, font) > width:
                    lines.append(line)
                    line = ''
                line += char
        elif line and _width(draw, line + ' ' + word, font) <= width:
            line += ' ' + word
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _height(draw, lines, font):
    heights = [draw.textbbox((0, 0), line, font=font)[3] - draw.textbbox((0, 0), line, font=font)[1] for line in lines]
    return sum(heights) + max(0, len(lines) - 1) * max(4, font.size // 4)


def _fit_text(draw, text, width, height, max_size, *, bold=False):
    text = ' '.join(text.split())[:4096]
    words = text.split()
    minimum_font = _font(18, bold)
    preserve_words = all(_width(draw, word, minimum_font) <= width for word in words)
    low, high = 18, max_size
    best = None
    while low <= high:
        size = (low + high) // 2
        font = _font(size, bold)
        if preserve_words and any(_width(draw, word, font) > width for word in words):
            high = size - 1
            continue
        lines = _wrap(draw, text, font, width)
        if _height(draw, lines, font) <= height:
            best = font, lines
            low = size + 1
        else:
            high = size - 1
    if best is not None:
        return best
    font = _font(18, bold)
    lines = _wrap(draw, text, font, width)
    while len(lines) > 1 and _height(draw, lines, font) > height:
        lines.pop()
    if lines:
        while lines[-1] and _width(draw, lines[-1] + '...', font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '...'
    return font, lines


def _draw_text(draw, text, top, height, max_size, *, bold=False):
    font, lines = _fit_text(draw, text, 1000, height, max_size, bold=bold)
    y = top + (height - _height(draw, lines, font)) / 2
    for line in lines:
        left, upper, right, lower = draw.textbbox((0, 0), line, font=font)
        draw.text(((1200 - (right - left)) / 2 - left, y - upper), line, fill='black', font=font)
        y += lower - upper + max(4, font.size // 4)


def text_cover(title: str, author: str, translator: str = '') -> CoverImage:
    check_deadline()
    if current_limits().max_cover_pixels < 1200 * 1800:
        raise ResourceLimitError('Лимит размера изображений слишком мал для текстовой обложки.')
    image = Image.new('RGB', (1200, 1800), 'white')
    draw = ImageDraw.Draw(image)
    _draw_text(draw, author.strip() if author.strip().lower() not in {'', 'unknown'} else 'Автор не указан', 170, 300, 64)
    _draw_text(draw, title.strip() or 'Фанфик', 550, 850, 108, bold=True)
    if translator.strip():
        _draw_text(draw, 'Перевод: ' + translator.strip(), 1490, 180, 42)
    check_deadline()
    buffer = LimitedBytesIO()
    image.save(buffer, format='PNG')
    return CoverImage(buffer.getvalue(), 'image/png', 'png')
