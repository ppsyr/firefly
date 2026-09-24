"""MemoryManager Protocol — 四操作编排契约（工具里无 LLM）。

【整体职责】
定义记忆四操作（Encode / Associate / Consolidate / Reconsolidate）的接口。
是 memory 模块的 7 个 Protocol 之一，约束 manager 实现（默认 DefaultMemoryManager）。

【四操作】
- Encode        ：创建新记忆。
- Associate     ：建立两条记忆的关联（扩散激活）。
- Consolidate   ：多条零散记忆合并为一条稳定知识。
- Reconsolidate ：更新已有记忆内容。

【核心原则】
工具里无 LLM：四操作都是纯存储操作，LLM 生成的内容（如 consolidate 的合并文本）
由外部传入。

【职责边界】
- 本模块只定义接口签名，零实现（Protocol 纯契约）。
- 检索不在本 Protocol —— retrieve 统一走 Retriever（MemoryProvider.retriever() 直给）。
- retrieve 强化（lazy decay + access_count+1）放 Retriever 实现里。
- 默认实现：DefaultMemoryManager（strategies/default/manager.py，Layer 2）。
- 可替换：其他满足本 Protocol 的 manager 实现。

【INVARIANT】
- 四操作无 LLM（consolidate / reconsolidate 的 merged_content / new_content 外部传入）。
- Retrieve 移至 Retriever（不在本 Protocol）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.memory.schema import MemoryTrace, MemoryType


@runtime_checkable
class MemoryManager(Protocol):
    """四种原子操作编排（Retrieve 移至 Retriever）。

    核心原则：工具里无 LLM。Encode / Associate / Consolidate / Reconsolidate
    都是纯存储操作，LLM 生成的内容（如 consolidate 时的合并文本）由外部传入。

    检索不在本 Protocol —— retrieve 统一走 Retriever（MemoryProvider.retriever() 直给）。
    retrieve 强化（lazy decay + access_count+1）放 Retriever 实现里。

    默认实现 DefaultMemoryManager（strategies/default/manager.py，Layer 2）。
    """

    def encode(
        self,
        content: str,
        type: MemoryType,
        *,
        importance: float = 0.5,
        source: str | None = None,
        metadata: dict | None = None,
    ) -> MemoryTrace:
        """Encode（编码）：创建新记忆。

        场景：用户说「我下周去东京出差」→ encode 为 episodic 记忆。

        Args:
            content: 记忆内容（自然语言，外部传入）。
            type: 记忆类型（episodic / semantic / procedural）。
            importance: 语义重要性 0.0~1.0（默认 0.5）。
            source: 来源（thread_id / run_id / user_input）。
            metadata: 扩展（tags / project / specialist_id）。

        Returns:
            MemoryTrace（新建 或 已存在，实现决定幂等语义）。

        Raises:
            不主动抛异常；实现内部异常按实现原逻辑传播。

        组装规则：
            1. 按 content + type 生成 id（实现决定算法）。
            2. 构造 trace（初始 strength / base_strength 由 type 决定）。
            3. 落库并返回。
        """
        ...

    def associate(
        self,
        trace_id_a: str,
        trace_id_b: str,
        *,
        strength: float = 0.5,
        type: str = "related",
    ) -> None:
        """Associate（关联）：建立两条记忆的关联。

        场景：新记忆「东京出差」与「用户喜欢日料」建立关联。
        下次检索到其中一条时，另一条也被激活（扩散激活）。

        Args:
            trace_id_a: 记忆 A 的 id。
            trace_id_b: 记忆 B 的 id。
            strength: 关联强度 0.0~1.0（默认 0.5）。
            type: 关联类型（related / causal / temporal / contrast，默认 "related"）。

        Returns:
            None。

        Raises:
            MemoryNotFoundError: trace_id_a 或 trace_id_b 不存在。

        组装规则：
            1. 双向建立关联（a→b 与 b→a）。
            2. 写入两条 trace（frozen 语义：替换）。
            3. 记录操作日志 + 发事件（实现决定细节）。
        """
        ...

    def consolidate(self, trace_ids: list[str], merged_content: str) -> MemoryTrace:
        """Consolidate（巩固）：多条零散记忆合并为一条稳定知识。

        场景：多条东京相关 episodic → 一条 semantic「用户经常去日本出差」。
        merged_content 由外部 LLM 生成后传入（工具里无 LLM）。

        Args:
            trace_ids: 待合并的旧记忆 id 列表（实现决定数量范围）。
            merged_content: 合并后的内容（外部 LLM 生成）。

        Returns:
            新创建的 MemoryTrace（通常为 semantic）。

        Raises:
            MemoryNotFoundError: trace_ids 中任一不存在。
            ValueError: trace_ids 数量不在实现允许范围。

        组装规则：
            1. 数量校验（实现决定 min / max）。
            2. 取旧 trace，计算合并后 importance。
            3. 构造新 trace 并落库。
            4. 标记旧 trace forgotten（不删除）。
            5. 返回新 trace。
        """
        ...

    def reconsolidate(self, trace_id: str, new_content: str) -> MemoryTrace:
        """Reconsolidate（重编码）：更新已有记忆内容。

        场景：用户说「其实改去大阪了」→ 更新出差记忆内容。
        依赖 retrieve 结果作为输入（agent 需先回忆起旧记忆才能修正）。
        new_content 由外部 LLM 生成后传入（工具里无 LLM）。

        Args:
            trace_id: 待更新的记忆 id。
            new_content: 新内容（外部 LLM 生成）。

        Returns:
            更新后的 MemoryTrace（id 不变，content 替换，strength 保留）。

        Raises:
            MemoryNotFoundError: trace_id 不存在。

        组装规则：
            1. 取旧 trace，不存在 → 抛 MemoryNotFoundError。
            2. 替换 content + last_accessed（strength 保留，视为一次访问）。
            3. 落库并返回。
        """
        ...