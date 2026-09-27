"""AstrBot 会话内的轻量证据状态与跨调用读取去重。

移植自 upstream `src/evidence-state.js`。
按会话（session_key）隔离证据状态；通过已读行区间与历史可见行（history_lines）
判断是否可复用已核验的本地原文，避免大篇幅重复读取消耗上下文。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

# 上限约束（对齐 upstream evidence-state.js）
MAX_DOCUMENTS = 96
MAX_CANDIDATES = 240
MAX_MAPPINGS = 240


def _lines_to_ranges(line_numbers: set[int] | list[int]) -> list[dict[str, int]]:
    """将散列行号集归并为连续区间列表。"""
    if not line_numbers:
        return []
    sorted_lines = sorted(set(line_numbers))
    ranges: list[dict[str, int]] = []
    start = sorted_lines[0]
    end = start
    for ln in sorted_lines[1:]:
        if ln == end + 1:
            end = ln
        else:
            ranges.append({"line_start": start, "line_end": end})
            start = ln
            end = ln
    ranges.append({"line_start": start, "line_end": end})
    return ranges


def _format_range_summary(ranges: list[dict[str, int]]) -> str:
    parts = []
    for r in ranges:
        if r["line_start"] == r["line_end"]:
            parts.append(str(r["line_start"]))
        else:
            parts.append(f"{r['line_start']}-{r['line_end']}")
    return "、".join(parts)


class EvidenceRegistry:
    """证据状态注册表：管理跨轮次证据缓存与阅读去重。"""

    def __init__(self, ttl_seconds: int = 6 * 3600, max_sessions: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.sessions: dict[str, dict[str, Any]] = {}
        self._timestamps: dict[str, float] = {}

    def session_key(self, umo: str, conversation_id: str) -> str:
        """根据统一消息对象（UMO）与会话 ID 构造确定性隔离键。"""
        return f"{str(umo or '').strip()}\0{str(conversation_id or '').strip()}"

    def begin_session(self, key: str, data_version: str | None = None) -> dict[str, Any]:
        """创建或获取会话证据状态；在数据版本变化、TTL 超时或显式失效时重置。"""
        now = time.time()
        state = self.sessions.get(key)
        ts = self._timestamps.get(key, 0.0)

        should_reset = False
        if state is None:
            should_reset = True
        elif (now - ts) > self.ttl_seconds:
            should_reset = True
        elif data_version and state.get("data_version") and state["data_version"] != data_version:
            should_reset = True

        if should_reset:
            suffix = secrets.token_hex(8)
            state = {
                "intent_id": f"dsh-{suffix}",
                "cloud_intent_id": f"prts-{suffix}",
                "last_cloud_request_id": "",
                "search_candidates": [],
                "cloud_source_mappings": [],
                "read_coverage": [],  # list of {"document_id": str, "line_start": int, "line_end": int}
                "documents": {},  # doc_id -> {"lines": set[int], "integrity": dict, "data_version": str}
                "completed_search_calls": {},  # signature -> dict
                "data_version": data_version,
                "created_at": now,
                "updated_at": now,
            }
            self.sessions[key] = state

        if data_version:
            state["data_version"] = data_version

        state["updated_at"] = now
        self._timestamps[key] = now
        self.prune()
        return state

    def note_search(self, key: str, signature: str, response: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """移植 upstream completed_search_calls 语义：
        当相同的搜索特征已在此会话中完成过且上游应复用时，返回复用提示信息；否则记录并返回 None。
        """
        state = self.begin_session(key, None)
        completed = state.setdefault("completed_search_calls", {})

        if signature in completed:
            cached = completed[signature]
            return {
                "reused": True,
                "signature": signature,
                "response": cached.get("response") if isinstance(cached, dict) else None,
                "guidance": "该搜索请求在当前会话中已有完成的结果，可直接复用上方工具结果，避免重复检索。",
            }

        completed[signature] = {
            "signature": signature,
            "response": response,
            "timestamp": time.time(),
        }
        # 控制完成调用缓存大小
        if len(completed) > 256:
            oldest_key = next(iter(completed))
            completed.pop(oldest_key, None)

        return None

    def apply_read(
        self,
        key: str,
        *,
        data_version: str | None,
        document_id: str,
        line_start: int,
        line_end: int,
        history_lines: set[int] | None,
    ) -> dict[str, Any]:
        """计算请求区间中与模型可见历史重叠与需新读取的部分。

        # Difference:
        # upstream 检查渲染后的各行字符串是否完整停留在模型的可见 surface 中；
        # 在 AstrBot 环境中无法低开销截获渲染后各行 AST，故这里以 history_lines（对话历史中
        # 仍存在的 L{n} 行号集合）作为可见性判定依据。
        # 若 history_lines is None（无法获取上下文历史），采取保守策略：不复用任何行
        # （reused_ranges=[], new_ranges=[完整请求区间]）。
        # 历史已读区间在 ≥90% 行号仍在 history_lines 中时判定为“仍然可见”（<=8 行的小区间需 100% 存在）。
        """
        state = self.begin_session(key, data_version)

        if line_start > line_end:
            return {
                "reused_ranges": [],
                "new_ranges": [],
                "drop": True,
                "guidance": None,
                "document_id": document_id,
                "integrity": None,
                "data_version": data_version,
            }

        doc_entry = state["documents"].setdefault(
            document_id,
            {"lines": set(), "integrity": {}, "data_version": data_version},
        )
        doc_entry["data_version"] = data_version

        # 1. 计算当前依然在历史中可见的已读行号
        still_visible_lines: set[int] = set()

        if history_lines is not None:
            # 遍历所有针对该篇章的先前已记录区间
            for cov in state.get("read_coverage", []):
                if cov.get("document_id") != document_id:
                    continue
                cov_start = cov["line_start"]
                cov_end = cov["line_end"]
                cov_total = cov_end - cov_start + 1
                if cov_total <= 0:
                    continue
                present_lines = {ln for ln in range(cov_start, cov_end + 1) if ln in history_lines}
                # ≤8 行需全量可见；大区间 ≥90% 可见
                is_visible = (len(present_lines) == cov_total) if cov_total <= 8 else (len(present_lines) / cov_total >= 0.9)
                if is_visible:
                    still_visible_lines.update(range(cov_start, cov_end + 1))

        # 2. 划分请求行区间
        requested_set = set(range(line_start, line_end + 1))
        reused_set = requested_set & still_visible_lines
        new_set = requested_set - still_visible_lines

        reused_ranges = _lines_to_ranges(reused_set)
        new_ranges = _lines_to_ranges(new_set)

        # 3. 产生 guidance 与 drop 标识
        drop = False
        guidance = None

        if history_lines is not None and len(reused_set) == len(requested_set):
            # 全量命中且在可见历史中
            drop = True
            guidance = f"该范围（第 {line_start}-{line_end} 行）已在当前模型可见的上文工具结果中完整出现；这里复用已核验的本地原文，不要再次读取相同范围。"
        elif reused_ranges:
            # 部分重叠
            reused_str = _format_range_summary(reused_ranges)
            guidance = f"本次只展示尚未覆盖的行；第 {reused_str} 行已在上文可见工具结果中。"

        # 4. 更新已读覆盖与文档记录
        doc_entry["lines"].update(requested_set)
        self._merge_read_coverage(state, document_id, line_start, line_end)

        # 5. 篇章容量防爆截断
        while len(state["documents"]) > MAX_DOCUMENTS:
            oldest_doc = next(iter(state["documents"]))
            state["documents"].pop(oldest_doc, None)
            state["read_coverage"] = [c for c in state["read_coverage"] if c.get("document_id") != oldest_doc]

        return {
            "reused_ranges": reused_ranges,
            "new_ranges": new_ranges,
            "drop": drop,
            "guidance": guidance,
            "document_id": document_id,
            "integrity": doc_entry.get("integrity"),
            "data_version": doc_entry.get("data_version"),
        }

    def remember_document_integrity(
        self,
        key: str,
        document_id: str,
        integrity: dict[str, Any],
        data_version: str | None = None,
    ) -> None:
        """记录篇章完整性校验元数据（local_integrity.sha256 与 lines_count）。"""
        state = self.begin_session(key, data_version)
        doc_entry = state["documents"].setdefault(
            document_id,
            {"lines": set(), "integrity": {}, "data_version": data_version},
        )
        doc_entry["integrity"] = dict(integrity)
        if data_version:
            doc_entry["data_version"] = data_version

    def _merge_read_coverage(self, state: dict[str, Any], document_id: str, line_start: int, line_end: int) -> None:
        coverage: list[dict[str, Any]] = state.setdefault("read_coverage", [])
        overlaps = [
            c for c in coverage
            if c.get("document_id") == document_id
            and c["line_end"] + 1 >= line_start
            and line_end + 1 >= c["line_start"]
        ]
        if overlaps:
            merged_start = min(line_start, *(c["line_start"] for c in overlaps))
            merged_end = max(line_end, *(c["line_end"] for c in overlaps))
            coverage[:] = [c for c in coverage if c not in overlaps]
            coverage.append({
                "document_id": document_id,
                "line_start": merged_start,
                "line_end": merged_end,
            })
        else:
            coverage.append({
                "document_id": document_id,
                "line_start": line_start,
                "line_end": line_end,
            })

    def invalidate(self, key: str) -> None:
        """显式丢弃指定会话的所有证据状态。"""
        self.sessions.pop(key, None)
        self._timestamps.pop(key, None)

    def prune(self) -> int:
        """清理已过期及超出最大容量（LRU）的会话。"""
        now = time.time()
        expired_keys = [k for k, ts in self._timestamps.items() if (now - ts) > self.ttl_seconds]
        for k in expired_keys:
            self.sessions.pop(k, None)
            self._timestamps.pop(k, None)

        if len(self.sessions) > self.max_sessions:
            sorted_keys = sorted(self._timestamps.keys(), key=lambda k: self._timestamps[k])
            excess = len(self.sessions) - self.max_sessions
            for k in sorted_keys[:excess]:
                self.sessions.pop(k, None)
                self._timestamps.pop(k, None)
            return len(expired_keys) + excess
        return len(expired_keys)
