"""Детектор кодировок текстовых файлов.

Порядок (по убыванию надёжности):

1. **BOM** — UTF-8/16/32 с сигнатурой;
2. **UTF-8** строгим декодом (валидный UTF-8 — это UTF-8);
3. **windows-1251** (кириллица) → **cp1252** (западная) → **latin-1**.

Кандидаты оцениваются «штрафом за подозрительные символы»: диакритики
(À–ÿ) и управляющие C1 почти не встречаются в реальных текстах — их много,
когда текст прочитан не той кодовой таблицей. Кириллица штрафа не несёт
(это обычные буквы), поэтому русский текст в cp1251 выигрывает у cp1252,
который даёт те же байты как À–ÿ. При равенстве штрафов первый кандидат
(cp1251) — файлы профиля преимущественно русскоязычные.

``latin-1`` декодирует любые байты без ошибок и даёт C1-управляющие на
байтах 0x80–0x9F — это всегда худший вариант, берётся только когда cp1251
и cp1252 дают одинаково «мусорный» результат (например, бинарный мусор,
который дошёл до экстрактора).
"""

from __future__ import annotations

import codecs

_BOM_CANDIDATES: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF8, "utf-8"),
)

# Порядок важен: при равенстве штрафов побеждает первый.
_CP_CANDIDATES: tuple[str, ...] = ("cp1251", "cp1252", "latin-1")


def decode_text(data: bytes) -> str:
    """Декодировать байты в текст (BOM → UTF-8 → cp1251 → cp1252 → latin-1)."""
    if not data:
        return ""
    for bom, encoding in _BOM_CANDIDATES:
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best_text = ""
    best_score: int | None = None
    for encoding in _CP_CANDIDATES:
        text = data.decode(encoding, errors="replace")
        score = _penalty(text)
        if best_score is None or score < best_score:
            best_score = score
            best_text = text
    # cp1251 vs cp1252. Те же байты 0xC0–0xFF дают кириллицу в cp1251 и
    # диакритики À–ÿ в cp1252, поэтому симметричные «доли» не различают.
    # Решает бедность обеих гипотез: настоящий западный текст (cp1252)
    # почти не содержит ни диакритик (акценты на единичных буквах), ни —
    # будучи прочитан как cp1251 — «случайной» кириллицы. Русский текст
    # хотя бы в одной из проекций даёт заметную долю. Порог 0.2.
    cp1251_text = data.decode("cp1251", errors="replace")
    cp1252_text = data.decode("cp1252", errors="replace")
    if _cyrillic_share(cp1251_text) < 0.2 and _diacritic_share(cp1252_text) < 0.2:
        return cp1252_text
    return best_text


def _cyrillic_share(text: str) -> float:
    """Доля кириллических символов среди букв (0.0–1.0)."""
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return 0.0
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    return cyrillic / letters


def _diacritic_share(text: str) -> float:
    """Доля диакритик À–ÿ среди букв (0.0–1.0)."""
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return 0.0
    diacritics = sum(1 for ch in text if 0xC0 <= ord(ch) <= 0xFF)
    return diacritics / letters


def _penalty(text: str) -> int:
    """Штраф за «непохожесть на реальный текст» (меньше — лучше)."""
    score = 0
    for ch in text:
        code = ord(ch)
        if code == 0xFFFD:  # replacement char — потерянные данные
            score += 2
        elif code < 0x20 and ch not in "\t\n\r":
            score += 1  # управляющие
        elif 0x80 <= code <= 0x9F:
            score += 1  # C1 control: latin-1 читает так байты 0x80–0x9F
        elif 0xC0 <= code <= 0xFF:
            score += 1  # À–ÿ: cp1252 так читает байты cp1251-кириллицы
    return score
