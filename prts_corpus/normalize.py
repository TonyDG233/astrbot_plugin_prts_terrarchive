"""文本归一化、键比较与 PRTSNG2 二进制倒排格式编解码。

复用上游 prts-terrarchive 的分词、NFKC 规约、LEB128 varint 及二进制索引格式。
"""

import hashlib
import json
import math
import re
import struct
import unicodedata

PRTSNG2_MAGIC = b"PRTSNG2\x00"
PRTSTG1_MAGIC = b"PRTSTG1\x00"


def normalize_search_text(value: str) -> str:
    """搜索端文本归一化：NFKC + 空白折叠 + 小写 + 两端裁剪。

    与上游 JS search.js `normalizeText(value).toLowerCase()` 严格保持一致。
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    collapsed = re.sub(r"\s+", " ", normalized).strip().lower()
    return collapsed


def compare_ngram_keys(left: str, right: str) -> int:
    """与编译期 SQLite BINARY（UTF-8 字节序）保持一致。

    负数表示 left < right，正数表示 left > right，0 表示相等。
    """
    b_left = str(left if left is not None else "").encode("utf-8")
    b_right = str(right if right is not None else "").encode("utf-8")
    if b_left < b_right:
        return -1
    elif b_left > b_right:
        return 1
    return 0


def iter_ngrams(text: str, sizes: tuple[int, ...] = (1, 2, 3)) -> list[str]:
    """提取文本中的 n-gram 片段，保持出现顺序并去重。"""
    if not text:
        return []
    chars = list(text)
    result = []
    seen = set()
    for size in sizes:
        if size <= 0 or len(chars) < size:
            continue
        for i in range(len(chars) - size + 1):
            gram = "".join(chars[i : i + size])
            if gram not in seen:
                seen.add(gram)
                result.append(gram)
    return result


def encode_varint(value: int) -> bytes:
    """无符号 LEB128 整数编码。"""
    if value < 0:
        raise ValueError("Varint value must be non-negative")
    res = bytearray()
    while True:
        towrite = value & 0x7F
        value >>= 7
        if value:
            res.append(towrite | 0x80)
        else:
            res.append(towrite)
            break
    return bytes(res)


def decode_varint(buffer: bytes, offset: int = 0) -> tuple[int, int]:
    """无符号 LEB128 整数解码，返回 (value, next_offset)。"""
    value = 0
    shift = 0
    limit = len(buffer)
    pos = offset
    while pos < limit and shift <= 49:
        b = buffer[pos]
        pos += 1
        value += (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
    raise ValueError("CorpusStore: invalid trigram varint")


def encode_prtsng2(entries: list[tuple[bytes | str, list[int]]]) -> bytes:
    """将 (key_utf8_bytes, ascending postings) 编码为 PRTSNG2 二进制格式。

    格式结构：
      magic(8) + uint32LE count + (count+1) uint32LE offsets + records
      record = varint(len(key)) + key + varint(posting_count) + delta-varint postings
    """
    def entry_key_bytes(entry):
        k = entry[0]
        return k if isinstance(k, bytes) else str(k).encode("utf-8")

    sorted_entries = sorted(entries, key=entry_key_bytes)
    records = []
    offsets = [0]
    current_offset = 0

    for raw_key, postings in sorted_entries:
        k_bytes = raw_key if isinstance(raw_key, bytes) else str(raw_key).encode("utf-8")
        rec = bytearray()
        rec.extend(encode_varint(len(k_bytes)))
        rec.extend(k_bytes)
        rec.extend(encode_varint(len(postings)))
        curr = 0
        for p in sorted(postings):
            delta = p - curr
            rec.extend(encode_varint(delta))
            curr = p
        records.append(rec)
        current_offset += len(rec)
        offsets.append(current_offset)

    count = len(sorted_entries)
    header = bytearray(PRTSNG2_MAGIC)
    header.extend(struct.pack("<I", count))
    for off in offsets:
        header.extend(struct.pack("<I", off))
    for rec in records:
        header.extend(rec)
    return bytes(header)


def decode_prtsng2(data: bytes) -> list[tuple[str, list[int]]]:
    """解码 PRTSNG2 或 PRTSTG1 二进制格式，返回 [(key_str, [posting_id, ...]), ...]"""
    if len(data) < 12:
        raise ValueError("Invalid prtsng2 buffer: too short")
    magic = data[:8]
    if magic != PRTSNG2_MAGIC and magic != PRTSTG1_MAGIC:
        raise ValueError("Invalid prtsng2 buffer: magic mismatch")
    count = struct.unpack_from("<I", data, 8)[0]
    payload_start = 12 + (count + 1) * 4
    if payload_start > len(data):
        raise ValueError("Invalid prtsng2 buffer: offset table overflow")

    offsets = [struct.unpack_from("<I", data, 12 + i * 4)[0] for i in range(count + 1)]
    results = []
    for i in range(count):
        start = payload_start + offsets[i]
        end = payload_start + offsets[i + 1]
        if start > len(data) or end > len(data) or start > end:
            raise ValueError("Invalid prtsng2 buffer: malformed record boundaries")
        pos = start
        k_len, pos = decode_varint(data, pos)
        k_bytes = data[pos : pos + k_len]
        pos += k_len
        posting_count, pos = decode_varint(data, pos)
        postings = []
        curr = 0
        for _ in range(posting_count):
            delta, pos = decode_varint(data, pos)
            curr += delta
            postings.append(curr)
        if pos != end:
            raise ValueError("Invalid prtsng2 buffer: record length mismatch")
        results.append((k_bytes.decode("utf-8", errors="replace"), postings))
    return results


def sha256_hex(data: bytes | str) -> str:
    """计算文本或字节的 SHA-256 十六进制字符串。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(value) -> str:
    """递归排序对象键并序列化为严格 JSON，用于发布包内容根校验。

    与 installer.js canonicalJson 保持完全一致的格式与键排序行为。
    """
    if isinstance(value, (list, tuple)):
        return f"[{','.join(canonical_json(x) for x in value)}]"
    if isinstance(value, dict):
        items = []
        for k in sorted(value.keys(), key=lambda x: str(x)):
            items.append(f"{json.dumps(str(k), ensure_ascii=False)}:{canonical_json(value[k])}")
        return f"{{{','.join(items)}}}"
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def estimate_tokens(value: int | str | None) -> int:
    """保守估算 token 开销：Math.ceil(length / 2.5)。"""
    length = value if isinstance(value, int) and not isinstance(value, bool) else len(str(value or ""))
    return math.ceil(length / 2.5)
