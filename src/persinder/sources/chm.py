"""Чтение CHM (Compiled HTML Help) на стандартной библиотеке.

Формат ITSS (тот же, что у ``.chm``/``.lit``):

- **ITSF** — заголовок файла: смещение оглавления (0x48), длина (0x50),
  смещение данных (0x58, только v3);
- **ITSP** — заголовок оглавления: размер блока, корень/голова индекса;
- **PMGL/PMGI** — листья/ветви оглавления. Запись листа — три «сжатых
  dword» (base-128 varint): длина имени, имя, затем ``space``, ``start``,
  ``length``. ``space == 0`` — сырые байты по ``data_offset + start``;
  ``space == 1`` — сжатый поток ``MSCompressed``;
- **LZX** — сжатие потока ``MSCompressed`` (окно 32 КБ–2 МБ, три типа
  блоков: verbatim / aligned offset / uncompressed).

LZX — вариант LZ77 с Хаффманом (как DEFLATE, но со своими деревьями и
битовым порядком: 16-битные little-endian слова, биты читаются сверху
вниз). Алгоритм реализован по его описанию (Microsoft [MS-CAB]) и
эталонным распаковщикам CHMLib/cabextract; это самостоятельный порт на
Python, без C-расширений.

Распаковка ленива: сжатый поток разжимается целиком при первом обращении
к сжатой записи и кэшируется. Для типичных справок это единицы-десятки
мегабайт; память ограничена размером распакованного потока.
"""

from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["ChmArchive"]

# --- константы LZX --------------------------------------------------------

_LZX_MIN_MATCH = 2
_LZX_NUM_CHARS = 256
_BLOCKTYPE_INVALID = 0
_BLOCKTYPE_VERBATIM = 1
_BLOCKTYPE_ALIGNED = 2
_BLOCKTYPE_UNCOMPRESSED = 3
_PRETREE_NUM_ELEMENTS = 20
_ALIGNED_NUM_ELEMENTS = 8
_NUM_PRIMARY_LENGTHS = 7
_NUM_SECONDARY_LENGTHS = 249
_LENTABLE_SAFETY = 64

_EXTRA_BITS = (
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
    7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14,
    15, 15, 16, 16, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17,
    17, 17, 17,
)

_POSITION_BASE = (
    0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192,
    256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288,
    16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144,
    393216, 524288, 655360, 786432, 917504, 1048576, 1179648, 1310720,
    1441792, 1572864, 1703936, 1835008, 1966080, 2097152,
)

_M32 = 0xFFFFFFFF


class _IllegalDataError(Exception):
    """Повреждённые данные LZX."""


class _BitReader:
    """Битовый поток LZX: 16-битные LE-слова, биты читаются старшими вперёд."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.bitbuf = 0
        self.bitsleft = 0
        self.end = len(data)

    def ensure(self, n: int) -> None:
        while self.bitsleft < n:
            b0 = self.data[self.pos] if self.pos < self.end else 0
            b1 = self.data[self.pos + 1] if self.pos + 1 < self.end else 0
            self.pos += 2
            word = b0 | (b1 << 8)
            self.bitbuf |= (word << (16 - self.bitsleft)) & _M32
            self.bitsleft += 16

    def peek(self, n: int) -> int:
        self.ensure(n)
        return (self.bitbuf >> (32 - n)) & ((1 << n) - 1)

    def remove(self, n: int) -> None:
        self.bitbuf = (self.bitbuf << n) & _M32
        self.bitsleft -= n

    def read(self, n: int) -> int:
        value = self.peek(n)
        self.remove(n)
        return value

    def check_exhausted(self) -> None:
        if self.pos > self.end and (self.pos > self.end + 2 or self.bitsleft < 16):
            raise _IllegalDataError("bitstream exhausted")


def _make_decode_table(nsyms: int, nbits: int, length: list[int]) -> list[int]:
    """Каноническое дерево Хаффмана → таблица быстрого декодирования."""
    table = [0] * ((1 << nbits) + (nsyms << 1))
    bit_num = 1
    pos = 0
    table_mask = 1 << nbits
    bit_mask = table_mask >> 1
    next_symbol = bit_mask

    while bit_num <= nbits:
        for sym in range(nsyms):
            if length[sym] == bit_num:
                leaf = pos
                pos += bit_mask
                if pos > table_mask:
                    raise _IllegalDataError("huffman table overrun")
                for fill in range(bit_mask):
                    table[leaf + fill] = sym
        bit_mask >>= 1
        bit_num += 1

    if pos != table_mask:
        for sym in range(pos, table_mask):
            table[sym] = 0
        pos <<= 16
        table_mask <<= 16
        bit_mask = 1 << 15
        while bit_num <= 16:
            for sym in range(nsyms):
                if length[sym] == bit_num:
                    leaf = pos >> 16
                    for fill in range(bit_num - nbits):
                        if table[leaf] == 0:
                            table[next_symbol << 1] = 0
                            table[(next_symbol << 1) + 1] = 0
                            table[leaf] = next_symbol
                            next_symbol += 1
                        leaf = table[leaf] << 1
                        if (pos >> (15 - fill)) & 1:
                            leaf += 1
                    table[leaf] = sym
                    pos += bit_mask
                    if pos > table_mask:
                        raise _IllegalDataError("huffman table overflow")
            bit_mask >>= 1
            bit_num += 1

    if pos == table_mask:
        return table
    for sym in range(nsyms):
        if length[sym]:
            raise _IllegalDataError("bad huffman table")
    return table


class _Huffman:
    def __init__(self, maxsymbols: int, tablebits: int) -> None:
        self.maxsymbols = maxsymbols
        self.tablebits = tablebits
        self.lens = [0] * (maxsymbols + _LENTABLE_SAFETY)

    def build(self) -> list[int]:
        return _make_decode_table(self.maxsymbols, self.tablebits, self.lens)

    def read_symbol(self, br: _BitReader, table: list[int]) -> int:
        br.ensure(16)
        index = table[br.peek(self.tablebits)]
        if index >= self.maxsymbols:
            bit = 1 << (32 - self.tablebits)
            while True:
                bit >>= 1
                index = (index << 1) | (1 if (br.bitbuf & bit) else 0)
                if not bit:
                    raise _IllegalDataError("bad huffman code")
                index = table[index]
                if index < self.maxsymbols:
                    break
        length = self.lens[index]
        br.remove(length)
        return index


def _read_lens(
    br: _BitReader, pre: _Huffman, huff: _Huffman, first: int, last: int
) -> None:
    """Прочитать длины кодов таблицы (с дельтами от прошлого блока)."""
    for x in range(_PRETREE_NUM_ELEMENTS):
        pre.lens[x] = br.read(4)
    pre_table = pre.build()

    x = first
    while x < last:
        z = pre.read_symbol(br, pre_table)
        if z == 17:
            count = br.read(4) + 4
            for _ in range(count):
                huff.lens[x] = 0
                x += 1
        elif z == 18:
            count = br.read(5) + 20
            for _ in range(count):
                huff.lens[x] = 0
                x += 1
        elif z == 19:
            count = br.read(1) + 4
            z = pre.read_symbol(br, pre_table)
            value = huff.lens[x] - z
            if value < 0:
                value += 17
            for _ in range(count):
                huff.lens[x] = value
                x += 1
        else:
            value = huff.lens[x] - z
            if value < 0:
                value += 17
            huff.lens[x] = value
            x += 1


class LZXState:
    """Состояние LZX-декомпрессора (окно, деревья, последние смещения)."""

    def __init__(self, window: int) -> None:
        if window < 15 or window > 21:
            raise ValueError("окно LZX должно быть 2^15..2^21")
        self.window_size = 1 << window
        self.window = bytearray(self.window_size)
        if window == 20:
            posn_slots = 42
        elif window == 21:
            posn_slots = 50
        else:
            posn_slots = window << 1
        self.r0 = self.r1 = self.r2 = 1
        self.main = _Huffman(_LZX_NUM_CHARS + (posn_slots << 3), 12)
        self.length = _Huffman(_NUM_SECONDARY_LENGTHS + 1, 12)
        self.aligned = _Huffman(_ALIGNED_NUM_ELEMENTS, 7)
        self.pre = _Huffman(_PRETREE_NUM_ELEMENTS, 6)
        self.header_read = False
        self.block_type = _BLOCKTYPE_INVALID
        self.block_length = 0
        self.block_remaining = 0
        self.window_posn = 0
        self.length_table: list[int] = []

    def reset(self) -> None:
        self.r0 = self.r1 = self.r2 = 1
        self.header_read = False
        self.block_type = _BLOCKTYPE_INVALID
        self.block_length = 0
        self.block_remaining = 0
        self.window_posn = 0
        for huff in (self.main, self.length):
            huff.lens = [0] * (huff.maxsymbols + _LENTABLE_SAFETY)

    def _read_trees(self, br: _BitReader) -> list[int]:
        _read_lens(br, self.pre, self.main, 0, 256)
        _read_lens(br, self.pre, self.main, 256, self.main.maxsymbols)
        main_table = self.main.build()
        _read_lens(br, self.pre, self.length, 0, _NUM_SECONDARY_LENGTHS)
        self.length_table = self.length.build()
        return main_table

    def decompress(self, data: bytes, outlen: int) -> bytes:
        """Разжать один блок ``outlen`` байт из ``data``; вернуть его байты."""
        br = _BitReader(data)
        window = self.window
        window_size = self.window_size
        window_posn = self.window_posn
        r0, r1, r2 = self.r0, self.r1, self.r2
        mask = window_size - 1

        if not self.header_read:
            if br.read(1):
                br.read(16)
                br.read(16)
            self.header_read = True

        togo = outlen
        main_table: list[int] = []
        aligned_table: list[int] = []

        while togo > 0:
            if self.block_remaining == 0:
                if self.block_type == _BLOCKTYPE_UNCOMPRESSED:
                    if self.block_length & 1:
                        br.pos += 1
                    br.bitbuf = 0
                    br.bitsleft = 0

                self.block_type = br.read(3)
                hi = br.read(16)
                lo = br.read(8)
                self.block_length = (hi << 8) | lo
                self.block_remaining = self.block_length

                if self.block_type == _BLOCKTYPE_ALIGNED:
                    for x in range(_ALIGNED_NUM_ELEMENTS):
                        self.aligned.lens[x] = br.read(3)
                    aligned_table = self.aligned.build()
                    main_table = self._read_trees(br)
                elif self.block_type == _BLOCKTYPE_VERBATIM:
                    aligned_table = []
                    main_table = self._read_trees(br)
                elif self.block_type == _BLOCKTYPE_UNCOMPRESSED:
                    br.ensure(16)
                    if br.bitsleft > 16:
                        br.pos -= 2
                    r0 = int.from_bytes(data[br.pos:br.pos + 4], "little")
                    r1 = int.from_bytes(data[br.pos + 4:br.pos + 8], "little")
                    r2 = int.from_bytes(data[br.pos + 8:br.pos + 12], "little")
                    br.pos += 12
                    aligned_table = []
                    main_table = []
                else:
                    raise _IllegalDataError("неизвестный тип блока")

            br.check_exhausted()

            while self.block_remaining > 0 and togo > 0:
                this_run = min(self.block_remaining, togo)
                togo -= this_run
                self.block_remaining -= this_run

                window_posn &= mask
                if window_posn + this_run > window_size:
                    raise _IllegalDataError("переполнение окна")

                if self.block_type == _BLOCKTYPE_UNCOMPRESSED:
                    if br.pos + this_run > br.end:
                        raise _IllegalDataError("выход за сжатые данные")
                    window[window_posn:window_posn + this_run] = data[
                        br.pos:br.pos + this_run
                    ]
                    br.pos += this_run
                    window_posn += this_run
                    continue

                while this_run > 0:
                    element = self.main.read_symbol(br, main_table)
                    if element < _LZX_NUM_CHARS:
                        window[window_posn] = element
                        window_posn += 1
                        this_run -= 1
                        continue

                    element -= _LZX_NUM_CHARS
                    match_length = element & _NUM_PRIMARY_LENGTHS
                    if match_length == _NUM_PRIMARY_LENGTHS:
                        match_length += self.length.read_symbol(
                            br, self.length_table
                        )
                    match_length += _LZX_MIN_MATCH
                    match_offset = element >> 3

                    if match_offset > 2:
                        if self.block_type == _BLOCKTYPE_VERBATIM:
                            if match_offset != 3:
                                extra = _EXTRA_BITS[match_offset]
                                verbatim = br.read(extra)
                                match_offset = (
                                    _POSITION_BASE[match_offset] - 2 + verbatim
                                )
                            else:
                                match_offset = 1
                        else:  # aligned offset
                            extra = _EXTRA_BITS[match_offset]
                            match_offset = _POSITION_BASE[match_offset] - 2
                            if extra > 3:
                                extra -= 3
                                match_offset += br.read(extra) << 3
                                match_offset += self.aligned.read_symbol(
                                    br, aligned_table
                                )
                            elif extra == 3:
                                match_offset += self.aligned.read_symbol(
                                    br, aligned_table
                                )
                            elif extra > 0:
                                match_offset += br.read(extra)
                            else:
                                match_offset = 1
                        r2, r1, r0 = r1, r0, match_offset
                    elif match_offset == 0:
                        match_offset = r0
                    elif match_offset == 1:
                        match_offset = r1
                        r1, r0 = r0, match_offset
                    else:
                        match_offset = r2
                        r2, r0 = r0, match_offset

                    src = window_posn - match_offset
                    dest = window_posn
                    window_posn += match_length
                    if window_posn > window_size:
                        raise _IllegalDataError("переполнение окна")
                    this_run -= match_length
                    for _ in range(match_length):
                        window[dest] = window[src & mask]
                        dest += 1
                        src += 1

        if togo != 0:
            raise _IllegalDataError("блок не дочитан")
        start = window_size if window_posn == 0 else window_posn
        result = bytes(window[start - outlen:start])
        self.window_posn = window_posn
        self.r0, self.r1, self.r2 = r0, r1, r2
        return result


def _cword(buf: bytes, pos: int) -> tuple[int, int]:
    """Прочитать «сжатый dword» (base-128 varint) из оглавления CHM."""
    accum = 0
    while True:
        byte = buf[pos]
        pos += 1
        if byte < 0x80:
            return (accum << 7) + byte, pos
        accum = (accum << 7) + (byte & 0x7F)


class ChmArchive:
    """CHM-архив: оглавление и чтение записей (с распаковкой LZX)."""

    def __init__(self, path: str | Path) -> None:
        self.data = Path(path).read_bytes()
        data = self.data
        if data[:4] != b"ITSF":
            raise ValueError("не CHM: нет сигнатуры ITSF")
        header_len = struct.unpack_from("<I", data, 8)[0]
        dir_offset = struct.unpack_from("<Q", data, 0x48)[0]
        self.data_offset = (
            struct.unpack_from("<Q", data, 0x58)[0] if header_len >= 0x60 else 0
        )
        itsp = data[dir_offset:dir_offset + 0x54]
        if itsp[:4] != b"ITSP":
            raise ValueError("не CHM: нет заголовка ITSP")
        itsp_len = struct.unpack_from("<I", itsp, 8)[0]
        self.block_len = struct.unpack_from("<I", itsp, 0x10)[0]
        index_root, index_head = struct.unpack_from("<ii", itsp, 0x1C)
        self.dir_offset = dir_offset + itsp_len
        if index_root <= -1:
            index_root = index_head
        self._entries: dict[str, tuple[int, int, int]] = {}
        self._load_directory(index_root)
        self._lzx_stream: bytes | None = None

    def _load_directory(self, root: int) -> None:
        page = root
        seen = 0
        while page != -1 and seen < 1_000_000:
            seen += 1
            base = self.dir_offset + page * self.block_len
            block = self.data[base:base + self.block_len]
            if block[:4] == b"PMGL":
                free_space = struct.unpack_from("<I", block, 4)[0]
                _prev, next_page = struct.unpack_from("<ii", block, 0x0C)
                end = self.block_len - free_space
                pos = 0x14
                while pos < end:
                    name_len, pos = _cword(block, pos)
                    name = block[pos:pos + name_len].decode("utf-8", "replace")
                    pos += name_len
                    space, pos = _cword(block, pos)
                    start, pos = _cword(block, pos)
                    length, pos = _cword(block, pos)
                    self._entries[name] = (space, start, length)
                page = next_page
            elif block[:4] == b"PMGI":
                free_space = struct.unpack_from("<I", block, 4)[0]
                end = self.block_len - free_space
                pos = 0x08
                page = -1
                while pos < end:
                    name_len, pos = _cword(block, pos)
                    pos += name_len
                    page, pos = _cword(block, pos)
            else:
                break

    def names(self) -> list[str]:
        """Имена всех записей оглавления (включая служебные ``/#...``)."""
        return list(self._entries)

    def read(self, name: str) -> bytes:
        """Прочитать запись по имени (с распаковкой LZX при необходимости)."""
        space, start, length = self._entries[name]
        if space == 0:
            offset = self.data_offset + start
            return self.data[offset:offset + length]
        return self._decompressed()[start:start + length]

    def _decompressed(self) -> bytes:
        if self._lzx_stream is None:
            self._lzx_stream = self._decompress_all()
        return self._lzx_stream

    def _decompress_all(self) -> bytes:
        control = self.read("::DataSpace/Storage/MSCompressed/ControlData")
        if control[4:8] != b"LZXC":
            raise ValueError("нет управляющих данных LZXC")
        version, reset_interval, window_size, windows_per_reset = (
            struct.unpack_from("<IIII", control, 8)
        )
        if version == 2:
            reset_interval *= 0x8000
            window_size *= 0x8000
        if window_size <= 0 or reset_interval <= 0:
            raise ValueError("некорректные параметры LZX")

        reset_name = (
            "::DataSpace/Storage/MSCompressed/Transform/"
            "{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/InstanceData/ResetTable"
        )
        reset_table = self.read(reset_name)
        rt_version, block_count, _unknown, table_offset = struct.unpack_from(
            "<IIII", reset_table, 0
        )
        if rt_version != 2:
            raise ValueError("неподдерживаемая версия ResetTable")
        uncompressed_len, compressed_len, block_size = struct.unpack_from(
            "<QQQ", reset_table, 16
        )
        offsets = struct.unpack_from(f"<{block_count}Q", reset_table, table_offset)
        content = self.read("::DataSpace/Storage/MSCompressed/Content")

        window = window_size.bit_length() - 1
        state = LZXState(window)
        reset_blocks = reset_interval // (window_size // 2) * windows_per_reset
        if reset_blocks <= 0:
            reset_blocks = 1
        out = bytearray()
        for index in range(block_count):
            if index % reset_blocks == 0:
                state.reset()
            start = offsets[index]
            if index < block_count - 1:
                length = offsets[index + 1] - start
            else:
                length = compressed_len - start
            out += state.decompress(content[start:start + length], block_size)
        return bytes(out[:uncompressed_len])
