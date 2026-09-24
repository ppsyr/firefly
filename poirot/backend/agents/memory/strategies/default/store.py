"""MarkdownFileStore — Markdown 持久化 truth source。

【整体职责】
实现 MemoryStore Protocol，提供记忆的持久化后端。
traces.md 是 truth source，内存索引是 derived（可重建）。
是 memory 模块唯一的持久化实现，被 manager / retriever / worker 共用。

【存储模型】
- 单文件：所有 trace 存于 traces.md。
- 分隔符：`<!-- trace: {id} -->`（方案 B）。
- 每条 trace：YAML frontmatter（除 content 外所有字段）+ content 正文。
- 内存索引：dict[str, MemoryTrace]，启动加载 + 增量维护。

【职责边界】
- 本模块只负责持久化与内存索引维护，不感知 forgotten / strength 精算
  （forgotten 过滤在 Retriever，strength 精算由调用方）。
- 无事务（2A）：逐个操作，失败 log，接受最终一致（Phase 2 cron 修复）。
- 文件锁（6B）：threading.Lock 保护写操作（单进程）；跨进程锁留后续。
- 解析容错（2A）：frontmatter 损坏 log + 跳过，不崩。

【INVARIANT】
- Markdown-as-Truth：traces.md 是 truth source，内存索引是 derived（可重建）。
- 单文件 + 分隔符：所有 trace 在 traces.md，用 `<!-- trace: {id} -->` 分隔。
- storage_path 锚定：相对路径用 cwd fallback
  （Layer 4 bootstrap 传绝对路径锚定 _PROJECT_ROOT）。
- 写操作由 threading.Lock 保护（单进程）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from poirot.backend.agents.memory.exceptions import (
    MemoryConflictError,
    MemoryNotFoundError,
)
from poirot.backend.agents.memory.schema import (
    Association,
    MemoryTrace,
    MemoryType,
    OperationLog,
)
from poirot.backend.agents.memory.types import MemoryFilter

logger = logging.getLogger(__name__)

# <!-- trace: {id} --> 分隔符，捕获 id + body（到下一个分隔符或文件尾）
_TRACE_SEPARATOR = re.compile(
    r"<!-- trace: ([a-f0-9]+) -->\n(.*?)(?=<!-- trace:|$)", re.DOTALL
)


class MarkdownFileStore:
    """Markdown 文件持久化（MemoryStore 的默认实现）。

    单文件 traces.md + 内存索引 dict[str, MemoryTrace]。
    构造即就绪：读 storage_path/traces.md，解析所有 trace 建内存索引。
    文件锁：threading.Lock 保护写操作（6B 单进程）。
    """

    def __init__(self, storage_path: str | Path) -> None:
        """初始化：解析 storage_path + 加载 traces.md 建内存索引。

        Args:
            storage_path: Markdown 持久化根目录。
                          相对路径用 cwd fallback；
                          Layer 4 bootstrap 传绝对路径锚定 _PROJECT_ROOT。

        Returns:
            None。

        Raises:
            不主动抛异常；文件系统异常按原逻辑传播。

        组装规则：
            1. 解析 storage_path → self._root（_resolve_storage_path）。
            2. 创建根目录（mkdir parents=True, exist_ok=True）。
            3. 定位 traces.md（self._root / "traces.md"）。
            4. 初始化文件锁（threading.Lock）。
            5. 初始化内存索引 dict。
            6. 调 _load() 启动加载。
        """
        self._root = self._resolve_storage_path(storage_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._traces_file = self._root / "traces.md"
        self._lock = threading.Lock()  # 6B 文件锁（单进程）
        # 内存索引：trace_id → MemoryTrace（启动加载 + 增量维护）
        self._traces: dict[str, MemoryTrace] = {}
        self._load()

    def _resolve_storage_path(self, storage_path: str | Path) -> Path:
        """解析 storage_path（相对路径锚定 cwd fallback）。

        Layer 4 bootstrap 负责传绝对路径（_resolve_relative_paths 锚定 _PROJECT_ROOT）。
        L3 fallback：相对路径用 Path.resolve() 锚定 cwd。

        Args:
            storage_path: 传入的路径（字符串或 Path）。

        Returns:
            解析后的绝对 Path。

        Raises:
            不主动抛异常。

        组装规则：
            1. 把 storage_path 转 Path。
            2. 绝对路径 → 直接返回。
            3. 相对路径 → Path.resolve() 锚定 cwd。
        """
        p = Path(storage_path)
        if p.is_absolute():
            return p
        return p.resolve()  # 相对路径锚定 cwd

    def _load(self) -> None:
        """启动加载：读 traces.md 解析所有 trace + 建内存索引。

        traces.md 不存在时创建空文件。
        解析容错（2A）：单条 frontmatter 损坏 log + 跳过，不崩。

        Args:
            无。

        Returns:
            None。

        Raises:
            不主动抛异常；文件读写异常按原逻辑传播。

        组装规则：
            1. traces.md 不存在 → 创建空文件（写标题）+ 返回。
            2. 存在 → 读全文。
            3. 用 _TRACE_SEPARATOR 找出所有 trace 块。
            4. 逐条调 _parse_trace 解析，成功则入内存索引。
            5. 记 info 日志（加载条数）。
        """
        if not self._traces_file.exists():
            self._traces_file.write_text("# Memory Traces\n\n", encoding="utf-8")
            return
        content = self._traces_file.read_text(encoding="utf-8")
        for match in _TRACE_SEPARATOR.finditer(content):
            trace_id = match.group(1)
            trace_body = match.group(2).strip()
            trace = self._parse_trace(trace_id, trace_body)
            if trace is not None:
                self._traces[trace.id] = trace
        logger.info(
            "MarkdownFileStore loaded %d traces from %s",
            len(self._traces), self._traces_file,
        )

    def _parse_trace(self, trace_id: str, body: str) -> MemoryTrace | None:
        """解析单条 trace（frontmatter + content）→ MemoryTrace。

        格式：
            ---
            {yaml frontmatter：所有字段除 content}
            ---
            {content 正文}

        Args:
            trace_id: 分隔符里捕获的 trace id（用于日志）。
            body:     单条 trace 的正文块（frontmatter + content）。

        Returns:
            解析成功的 MemoryTrace；解析失败返 None（跳过）。

        Raises:
            不主动抛异常；内部 try/except 兜底，失败只 log warning。

        组装规则：
            1. body 不以 "---\\n" 开头 → log warning + 返 None。
            2. 拆 frontmatter 与 content，拆分失败 → log warning + 返 None。
            3. yaml.safe_load 解析 frontmatter，非 dict → log warning + 返 None。
            4. 校验必填字段 id / type，缺失 → log warning + 返 None。
            5. 类型还原：
               - type string → MemoryType 枚举。
               - associations list[dict] → tuple[Association]。
               - operation_log list[dict] → tuple[OperationLog]。
               - embedding list → tuple 或 None。
            6. 构造 MemoryTrace 并返回；构造异常 → log warning + 返 None（容错）。
        """
        try:
            # 分离 frontmatter + content
            if not body.startswith("---\n"):
                logger.warning("trace %s missing frontmarker, skipped", trace_id)
                return None
            parts = body[4:].split("\n---\n", 1)
            if len(parts) != 2:
                logger.warning("trace %s malformed frontmatter, skipped", trace_id)
                return None
            frontmatter_text, content = parts
            data = yaml.safe_load(frontmatter_text)
            if not isinstance(data, dict):
                logger.warning("trace %s frontmatter not dict, skipped", trace_id)
                return None

            # 必填字段校验
            if "id" not in data or "type" not in data:
                logger.warning("trace %s missing id/type, skipped", trace_id)
                return None

            # type string → MemoryType 枚举
            type_val = data.pop("type")
            mem_type = MemoryType(type_val) if not isinstance(type_val, MemoryType) else type_val

            # associations list[dict] → tuple[Association]
            assocs_data = data.pop("associations", [])
            associations = tuple(
                Association(**a) for a in assocs_data
            ) if assocs_data else ()

            # operation_log list[dict] → tuple[OperationLog]
            log_data = data.pop("operation_log", [])
            operation_log = tuple(
                OperationLog(**log) for log in log_data
            ) if log_data else ()

            # embedding list → tuple 或 None
            embedding = data.pop("embedding", None)
            if embedding is not None:
                embedding = tuple(embedding)

            return MemoryTrace(
                content=content,
                type=mem_type,
                associations=associations,
                operation_log=operation_log,
                embedding=embedding,
                **data,
            )
        except Exception as exc:
            logger.warning("Failed to parse trace %s: %s", trace_id, exc)
            return None

    def _serialize_trace(self, trace: MemoryTrace) -> str:
        """序列化 MemoryTrace → frontmatter + content 字符串。

        frontmatter（YAML）：所有字段除 content（content 放正文）。
        手动构建 dict 避免 yaml python/tuple tag（safe_load 不认）：
        - MemoryType → value string。
        - associations / operation_log tuple → list。
        - embedding tuple → list 或 None。
        - diff tuple → list。

        Args:
            trace: 待序列化的 MemoryTrace。

        Returns:
            字符串：`---\\n{frontmatter}\\n---\\n{content}`。

        Raises:
            不主动抛异常。

        组装规则：
            1. 构造 data dict（除 content 外所有字段）。
            2. associations / operation_log 逐项转普通 dict。
            3. embedding tuple → list（None 保持 None）。
            4. operation_log.diff 通过 _diff_to_serializable 转 list。
            5. yaml.dump 输出 frontmatter（不排序键、允许 unicode、不用 flow style）。
            6. 拼成 `---\\n{frontmatter}\\n---\\n{content}` 返回。
        """
        data = {
            "id": trace.id,
            "type": trace.type.value,
            "strength": trace.strength,
            "base_strength": trace.base_strength,
            "decay_rate": trace.decay_rate,
            "access_count": trace.access_count,
            "last_accessed": trace.last_accessed,
            "importance": trace.importance,
            "associations": [
                {"target_id": a.target_id, "strength": a.strength, "type": a.type}
                for a in trace.associations
            ],
            "embedding": list(trace.embedding) if trace.embedding is not None else None,
            "source": trace.source,
            "created_at": trace.created_at,
            "metadata": trace.metadata,
            "operation_log": [
                {
                    "timestamp": log.timestamp,
                    "operation": log.operation,
                    "actor": log.actor,
                    "diff": self._diff_to_serializable(log.diff),
                }
                for log in trace.operation_log
            ],
        }
        frontmatter = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).strip()
        return f"---\n{frontmatter}\n---\n{trace.content}"

    @staticmethod
    def _diff_to_serializable(diff: dict[str, Any] | None) -> dict[str, Any] | None:
        """OperationLog.diff tuple → list（yaml safe_load 兼容）。

        Args:
            diff: OperationLog.diff，可能为 None。

        Returns:
            转换后的 dict（tuple 值转 list）；None 原样返回。

        Raises:
            不主动抛异常。

        组装规则：
            1. diff 为 None → 返回 None。
            2. 遍历 diff，值为 tuple → 转 list；否则原样保留。
            3. 返回转换后的 dict。
        """
        if diff is None:
            return None
        result: dict[str, Any] = {}
        for k, v in diff.items():
            if isinstance(v, tuple):
                result[k] = list(v)
            else:
                result[k] = v
        return result

    def add(self, trace: MemoryTrace) -> None:
        """新增记忆。trace.id 已存在抛 MemoryConflictError。

        6B 文件锁保护：并发 add 序列化。

        Args:
            trace: 待新增的 MemoryTrace。

        Returns:
            None。

        Raises:
            MemoryConflictError: trace.id 已存在。

        组装规则：
            1. 加 _lock。
            2. trace.id 已在内存索引 → 抛 MemoryConflictError。
            3. 写入内存索引。
            4. 调 _append_to_file 增量追加到 traces.md。
        """
        with self._lock:
            if trace.id in self._traces:
                raise MemoryConflictError(
                    f"trace already exists: {trace.id}",
                    old_id=trace.id, new_id=trace.id,
                )
            self._traces[trace.id] = trace
            self._append_to_file(trace)

    def get(self, trace_id: str) -> MemoryTrace | None:
        """按 id 取记忆，不存在返 None。

        Args:
            trace_id: 记忆 id。

        Returns:
            对应 MemoryTrace；不存在返 None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 直接查内存索引并返回（读操作无锁，内存 dict 读安全）。
        """
        return self._traces.get(trace_id)

    def _append_to_file(self, trace: MemoryTrace) -> None:
        """追加单条 trace 到 traces.md（增量写）。

        Args:
            trace: 待追加的 MemoryTrace。

        Returns:
            None。

        Raises:
            不主动抛异常；文件写异常按原逻辑传播。

        组装规则：
            1. 拼分隔符 + 序列化结果 + 两个换行。
            2. 以追加模式打开 traces.md 并写入。
        """
        block = f"<!-- trace: {trace.id} -->\n{self._serialize_trace(trace)}\n\n"
        with open(self._traces_file, "a", encoding="utf-8") as f:
            f.write(block)

    def _rewrite_file(self) -> None:
        """全量重写 traces.md（update / remove / batch_update 后）。

        Layer 3 先用全量重写（简化），增量改留后续优化（记忆量大时）。

        Args:
            无。

        Returns:
            None。

        Raises:
            不主动抛异常；文件写异常按原逻辑传播。

        组装规则：
            1. 以写模式打开 traces.md。
            2. 先写标题 "# Memory Traces\\n\\n"。
            3. 遍历内存索引，逐条写分隔符 + 序列化结果 + 两个换行。
        """
        with open(self._traces_file, "w", encoding="utf-8") as f:
            f.write("# Memory Traces\n\n")
            for trace in self._traces.values():
                f.write(f"<!-- trace: {trace.id} -->\n{self._serialize_trace(trace)}\n\n")

    def update(self, trace: MemoryTrace) -> None:
        """更新记忆（frozen 语义：替换）。trace.id 不存在抛 MemoryNotFoundError。

        6B 文件锁保护：并发 update 序列化，防丢更新。

        Args:
            trace: 替换后的 MemoryTrace（id 必须已存在）。

        Returns:
            None。

        Raises:
            MemoryNotFoundError: trace.id 不在内存索引。

        组装规则：
            1. 加 _lock。
            2. trace.id 不在内存索引 → 抛 MemoryNotFoundError。
            3. 替换内存索引里的 trace。
            4. 调 _rewrite_file 全量重写 traces.md。
        """
        with self._lock:
            if trace.id not in self._traces:
                raise MemoryNotFoundError(trace.id)
            self._traces[trace.id] = trace
            self._rewrite_file()

    def batch_update(self, traces: list[MemoryTrace]) -> None:
        """批量更新（consolidate 标记 N 条旧 trace forgotten 用）。

        原子性：任一 trace.id 不存在抛 MemoryNotFoundError（全成功或全失败）。
        一次 _rewrite_file 全量重写（非 N 次 O(N²)）。
        6B 文件锁保护（与 update 同锁）。

        Args:
            traces: 待批量替换的 MemoryTrace 列表。

        Returns:
            None。

        Raises:
            MemoryNotFoundError: 任一 trace.id 不在内存索引。

        组装规则：
            1. 加 _lock。
            2. 先全量校验：任一 id 不在内存索引 → 抛 MemoryNotFoundError（不修改）。
            3. 全部合法 → 逐条替换内存索引。
            4. 调一次 _rewrite_file 全量重写。
        """
        with self._lock:
            for trace in traces:
                if trace.id not in self._traces:
                    raise MemoryNotFoundError(trace.id)
            for trace in traces:
                self._traces[trace.id] = trace
            self._rewrite_file()

    def remove(self, trace_id: str) -> None:
        """删除记忆。不存在静默（幂等）。

        Args:
            trace_id: 待删除的记忆 id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 加 _lock。
            2. trace_id 在内存索引 → 删除 + 调 _rewrite_file 重写。
            3. 不在 → 静默返回（幂等）。
        """
        with self._lock:
            if trace_id in self._traces:
                del self._traces[trace_id]
                self._rewrite_file()

    def list_by_type(self, type: MemoryType) -> list[MemoryTrace]:
        """按类型列出。

        Args:
            type: 记忆类型（MemoryType 或可转成字符串的值）。

        Returns:
            该类型下的所有 MemoryTrace 列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. type 转字符串 key（MemoryType → value，其他 → str）。
            2. 遍历内存索引，过滤 type.value == type_key 的 trace。
        """
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        return [t for t in self._traces.values() if t.type.value == type_key]

    def list_by_filter(self, filter: MemoryFilter) -> list[MemoryTrace]:
        """按过滤器列出（粗筛 + 调用方精算 strength）。

        7A：store 只按 max_age_hours / type / metadata 粗筛（内存索引），
        strength 精算由调用方（forget_policy）逐条 compute_strength。

        Args:
            filter: MemoryFilter（type_filter / max_age_hours / metadata_filter）。

        Returns:
            过滤后的 MemoryTrace 列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. 从内存索引取全部作为初始结果。
            2. type_filter 非 None → 按 type 过滤。
            3. max_age_hours 非 None → 按 last_accessed（≤0 用 created_at）过滤。
            4. metadata_filter 非空 → 全匹配过滤（所有 k/v 都满足）。
            5. 返回过滤后的列表。
        """
        result = list(self._traces.values())
        # type 过滤
        if filter.type_filter is not None:
            type_key = (
                filter.type_filter.value
                if isinstance(filter.type_filter, MemoryType)
                else str(filter.type_filter)
            )
            result = [t for t in result if t.type.value == type_key]
        # max_age_hours 粗筛（按 last_accessed，<=0 用 created_at，7A）
        if filter.max_age_hours is not None:
            now = time.time()
            max_age_seconds = filter.max_age_hours * 3600.0
            result = [
                t for t in result
                if (now - (t.last_accessed if t.last_accessed > 0 else t.created_at)) <= max_age_seconds
            ]
        # metadata 过滤（全匹配）
        if filter.metadata_filter:
            result = [
                t for t in result
                if all(t.metadata.get(k) == v for k, v in filter.metadata_filter.items())
            ]
        return result

    def list_all(self) -> list[MemoryTrace]:
        """列出全部。

        Args:
            无。

        Returns:
            内存索引中所有 MemoryTrace 的列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. 返回 list(self._traces.values())。
        """
        return list(self._traces.values())