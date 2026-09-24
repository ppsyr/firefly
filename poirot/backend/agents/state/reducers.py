"""状态 reducer — ThreadState 字段合并规则。

【整体职责】
定义 Poirot 状态合并的全部规则，分两套入口：
- merge_thread_state：外层的整体 patch 合并（LeaderAgent 用）。
- merge_xxx 系列：字段级 reducer，供 ThreadState 的 Annotated[..., reducer] 使用
  （LangGraph 图内部合并）。

【内容摘要】
- CORE_FIELDS            : 核心字段集合，metadata 不得包含。
- MAX_ERRORS             : errors 列表上限（截断保留最新）。
- ReducerConflictError   : 状态补丁会隐藏/破坏核心状态时抛出。
- merge_thread_state     : 外层整体 patch 合并（按字段分派到具体规则）。
- _merge_sources         : sources 按 source_id 覆盖 / url 去重。
- _merge_by_attr         : 通用按属性 ID 的覆盖或追加合并。
- _merge_metadata        : metadata 合并，禁止核心字段。
- _merge_final_report    : final_report last-write-wins 覆盖。
- _field / _update_same_type : 字段读取与同类型更新辅助。
- merge_sources / merge_citations / merge_artifacts / merge_reflection_items
- merge_observations / merge_errors / merge_metadata / merge_final_report
- merge_todos            : None 保留，否则整体替换。
- merge_governance       : deep-merge，叶子 last-write-wins。
- merge_tagged_context   : last-write-wins 替换。
- merge_sandbox          : 幂等合并 + fail-closed + 不清空。
- merge_orchestration    : specialist_artifacts / active_specialists 去重追加。
- _dedupe_orchestration_artifacts : 按 path 去重追加。
- merge_memory_recalled / merge_memory_updates : 记忆类合并。

【职责边界】
- 只负责：定义并实现状态字段的合并规则。
- 不负责：状态 schema 定义（state/types）、初始状态构造（thread_state）、
  state 的持久化（runtime / checkpointer）、消费状态的业务逻辑（middleware / agent）。

【INVARIANT】
- 两套入口分工：merge_thread_state（外层 patch）+ merge_xxx（图内字段级）。
- None 语义：多数 reducer 中 incoming=None 表示"未触碰"，保留 current 不清空。
- 核心字段保护：metadata 不得含 CORE_FIELDS，否则抛 ReducerConflictError。
- errors 有上限：超过 MAX_ERRORS 截断，保留最新。
- sources 双重去重：同 source_id 覆盖，同 url 去重。
- sandbox fail-closed：不同 sandbox_id 是 bug，抛 ValueError 而非静默选一。
- 记忆只存索引：recalled_memories 只存 id + score + strength，不存全量内容。
- final_report last-write-wins：多轮 run 可覆盖，避免跨 run 报错崩溃。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from typing import Any, Iterable

CORE_FIELDS = {
    "messages",
    "user_input",
    "intent",
    "research_question",
    "plan",
    "current_step_id",
    "observations",
    "sources",
    "citations",
    "artifacts",
    "reflection_items",
    "final_report",
    "errors",
    "recalled_memories",
    "memory_updates",
}

MAX_ERRORS = 100


class ReducerConflictError(ValueError):
    """Raised when a state patch would hide or corrupt core state."""


def merge_thread_state(state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """外层整体 patch 合并（LeaderAgent 用）：按字段分派到对应合并规则。

    Args:
        state: 当前状态。
        patch: 待合并的增量。

    Returns:
        dict[str, Any]: 合并后的新状态（不修改入参）。

    分派规则：
        sources → _merge_sources；citations / artifacts → _merge_by_attr；
        reflection_items → _merge_by_attr(item_id)；errors → 追加截断；
        metadata → _merge_metadata；final_report → _merge_final_report；
        其余字段 → 直接覆盖。
    """
    merged = dict(state)
    for key, value in patch.items():
        if key == "sources":
            merged[key] = _merge_sources(merged.get(key, []), value)
        elif key in {"citations", "artifacts"}:
            merged[key] = _merge_by_attr(merged.get(key, []), value, f"{key[:-1]}_id")
        elif key == "reflection_items":
            merged[key] = _merge_by_attr(merged.get(key, []), value, "item_id")
        elif key == "errors":
            merged[key] = (list(merged.get(key, [])) + list(value))[-MAX_ERRORS:]
        elif key == "metadata":
            merged[key] = _merge_metadata(merged.get(key, {}), value)
        elif key == "final_report":
            merged[key] = _merge_final_report(merged.get(key), value)
        else:
            merged[key] = value
    return merged


def _merge_sources(current: Iterable[Any], incoming: Iterable[Any]) -> list[Any]:
    """sources 合并：同 source_id 覆盖，同 url 去重，其余追加。

    Args:
        current: 现有 sources。
        incoming: 新增 sources。

    Returns:
        list[Any]: 合并后的 sources。
    """
    result = list(current)
    seen_urls = {_field(item, "url") for item in result}
    for item in incoming:
        item_id = _field(item, "source_id")
        url = _field(item, "url")
        replaced = False
        for index, existing in enumerate(result):
            if _field(existing, "source_id") == item_id or _field(existing, "url") == url:
                if _field(existing, "source_id") == item_id:
                    result[index] = item
                    seen_urls.add(url)
                replaced = True
                break
        if not replaced and url not in seen_urls:
            result.append(item)
            seen_urls.add(url)
    return result


def _merge_by_attr(current: Iterable[Any], incoming: Iterable[Any], attr: str) -> list[Any]:
    """通用按属性 ID 合并：同 ID 覆盖（同类型更新），新 ID 追加。

    Args:
        current: 现有列表。
        incoming: 新增列表。
        attr: 用于匹配的 ID 属性名。

    Returns:
        list[Any]: 合并后的列表。
    """
    result = list(current)
    positions = {_field(item, attr): index for index, item in enumerate(result)}
    for item in incoming:
        item_id = _field(item, attr)
        if item_id in positions:
            existing = result[positions[item_id]]
            result[positions[item_id]] = _update_same_type(existing, item)
        else:
            positions[item_id] = len(result)
            result.append(item)
    return result


def _merge_metadata(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """metadata 合并：禁止含核心字段，其余浅合并覆盖。

    Args:
        current: 现有 metadata。
        incoming: 新增 metadata。

    Returns:
        dict[str, Any]: 合并后的 metadata。

    Raises:
        ReducerConflictError: incoming 含 CORE_FIELDS 中字段时。
    """
    forbidden = CORE_FIELDS.intersection(incoming)
    if forbidden:
        field = sorted(forbidden)[0]
        raise ReducerConflictError(f"metadata cannot contain core field: {field}")
    merged = dict(current)
    merged.update(incoming)
    return merged


def _merge_final_report(current: str | None, incoming: str | None) -> str | None:
    """last-write-wins：多轮 chat 每个 run 可覆盖旧报告。

    原冲突报错设计只适用单次 run，checkpointer 跨 run 持久化后
    新 run 写 final_report 必触发 ReducerConflictError 崩溃。

    Args:
        current: 当前报告。
        incoming: 新报告。

    Returns:
        str | None: incoming 非 None 时取 incoming，否则保留 current。
    """
    return incoming if incoming is not None else current


def _field(item: Any, name: str) -> Any:
    """通用字段读取，兼容 dict 与对象属性。

    Args:
        item: 待读取对象。
        name: 字段名。

    Returns:
        Any: 字段值。
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name)


def _update_same_type(existing: Any, incoming: Any) -> Any:
    """同类型更新：类型不同直接替换，dataclass 用 replace 合并字段。

    Args:
        existing: 现有对象。
        incoming: 新对象。

    Returns:
        Any: 更新后的对象。
    """
    if type(existing) is not type(incoming):
        return incoming
    if is_dataclass(existing):
        return replace(existing, **asdict(incoming))
    return incoming


# ---------------------------------------------------------------------------
# Public field-level reducers for Annotated[type, reducer] in ThreadState.
# Used by LangGraph graph-internal state merge. merge_thread_state (above)
# remains for LeaderAgent outer-layer patch merging (design D7).
# ---------------------------------------------------------------------------


def merge_sources(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：sources。incoming None 保留，否则按 source_id/url 合并。"""
    if incoming is None:
        return list(current or [])
    return _merge_sources(current or [], incoming)


def merge_citations(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：citations。按 citation_id 合并。"""
    if incoming is None:
        return list(current or [])
    return _merge_by_attr(current or [], incoming, "citation_id")


def merge_artifacts(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：artifacts。按 artifact_id 合并。"""
    if incoming is None:
        return list(current or [])
    return _merge_by_attr(current or [], incoming, "artifact_id")


def merge_reflection_items(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：reflection_items。按 item_id 合并。"""
    if incoming is None:
        return list(current or [])
    return _merge_by_attr(current or [], incoming, "item_id")


def merge_observations(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：observations。追加（不去重）。"""
    if incoming is None:
        return list(current or [])
    return list(current or []) + list(incoming)


def merge_errors(current: list | None, incoming: list | None) -> list:
    """字段级 reducer：errors。追加并截断至 MAX_ERRORS。"""
    if incoming is None:
        return list(current or [])
    return (list(current or []) + list(incoming))[-MAX_ERRORS:]


def merge_metadata(current: dict | None, incoming: dict | None) -> dict:
    """字段级 reducer：metadata。禁止含核心字段，浅合并覆盖。"""
    if incoming is None:
        return dict(current or {})
    return _merge_metadata(current or {}, incoming)


def merge_final_report(current: str | None, incoming: str | None) -> str | None:
    """字段级 reducer：final_report。last-write-wins。"""
    return _merge_final_report(current, incoming)


def merge_todos(existing: list | None, new: list | None) -> list | None:
    """Reducer for todos list.

    Semantics:
    - new is None (node didn't touch todos) → preserve existing.
    - new is provided (even empty list) → full replacement (explicit update wins).
    """
    if new is None:
        return existing
    return new


def merge_governance(current: dict | None, incoming: dict | None) -> dict | None:
    """Reducer for ThreadState.governance — deep-merge, last-write-wins per leaf key.

    - incoming None → preserve current.
    - current None → return copy of incoming.
    - both dict → recursive deep-merge: dict values merged recursively,
      non-dict (scalar/list) last-write-wins.

    策略 bundle 自管命名空间 ``governance.<strategy_name>.*``，deep-merge 并存，
    同 key last-write-wins。跨轮经 checkpointer 持久化。
    """
    if incoming is None:
        return current
    if current is None:
        return dict(incoming)
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return incoming
    merged = dict(current)
    for key, value in incoming.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_governance(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge_tagged_context(current: dict | None, incoming: dict | None) -> dict | None:
    """Reducer for ThreadState.tagged_context — last-write-wins.

    存每轮 ContextAssembler 渲染的标签序列快照（trace 审计，对比"模型看到的"
    vs"state 存的"）。incoming None 保留 current，否则 incoming 替换。
    """
    if incoming is None:
        return current
    return incoming


def merge_sandbox(
    existing: dict | None, new: dict | None
) -> dict | None:
    """Reducer for ThreadState.sandbox — 幂等合并 + fail-closed + 不清空。

    设计决策（Grill #3 确认）:
    - sandbox_id 是 thread 级持久状态，跨轮保留加速复用（Layer 1 cache 秒级命中）
    - 不需主动清空：容器死时（destroy/idle 超时）隐式重建，同 thread 算出同 ID，state 值不变
    - fail-closed：不同 id 是 bug 不是 race（确定性 ID 排除合法 race），应暴露而非静默
    - 失败传播：reducer 抛 ValueError 保持，graph 外层（LeaderAgent.run / stream_service）捕获转优雅提示

    INVARIANT:
    - 幂等合并：同 sandbox_id 合并 OK（多个工具同轮懒加载，确定性 ID 相同）
    - fail-closed：不同 id 抛 ValueError（生命周期 bug，不静默选一个）
    - 不清空：new=None 返 existing（保留持久状态）；无主动清空语义

    触发场景（全是 bug）:
    - provider 返回 id 与确定性公式不一致
    - user_id 解析不一致（多用户 fallback bug）
    - middleware wrap_tool_call diff 逻辑 bug 写入错误 id
    - subagent 继承父 sandbox 失败（未来风险）
    """
    if new is None:
        return existing
    if existing is None:
        return new
    existing_id = existing.get("sandbox_id")
    new_id = new.get("sandbox_id")
    if existing_id == new_id:
        return existing
    raise ValueError(
        f"Conflicting sandbox state updates: {existing_id!r} != {new_id!r}"
    )


def merge_orchestration(
    existing: dict | None, new: dict | None
) -> dict | None:
    """Reducer for ThreadState.orchestration — 去重追加。

    Multi-Agent 编排层状态合并（design.md §9）:
    - new None → preserve existing（不清空，与 merge_sandbox 一致）
    - existing None → return new
    - both → specialist_artifacts 按 path 去重追加 + active_specialists 按 name 去重追加

    INVARIANT:
    - artifacts 分离：specialist 产物写 orchestration.specialist_artifacts，
      不混入 ThreadState.artifacts（lead agent 产物）
    - 去重追加：同 path 覆盖（last-write-wins），同 name 去重
    - 不清空：new=None 返 existing（保留编排历史，跨轮审计）
    """
    if new is None:
        return existing
    if existing is None:
        return new
    return {
        "specialist_artifacts": _dedupe_orchestration_artifacts(
            existing.get("specialist_artifacts") or [],
            new.get("specialist_artifacts") or [],
        ),
        "active_specialists": list(dict.fromkeys(
            (existing.get("active_specialists") or [])
            + (new.get("active_specialists") or [])
        )),
    }


def _dedupe_orchestration_artifacts(existing: Iterable[Any], incoming: Iterable[Any]) -> list[Any]:
    """specialist_artifacts 按 path 去重追加（last-write-wins per path）。

    支持 ArtifactRef frozen dataclass 和 dict（_field 兼容两种）。

    Args:
        existing: 现有 specialist 产物。
        incoming: 新增 specialist 产物。

    Returns:
        list[Any]: 合并后的 specialist 产物。
    """
    result = list(existing)
    positions = {_field(item, "path"): index for index, item in enumerate(result)}
    for item in incoming:
        path = _field(item, "path")
        if path in positions:
            result[positions[path]] = item
        else:
            positions[path] = len(result)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Long-term memory reducers (Layer 1 接入)
# ---------------------------------------------------------------------------


def merge_memory_recalled(existing: list | None, new: list | None) -> list | None:
    """Reducer for ThreadState.recalled_memories — 去重追加 by id。

    - new None → preserve existing（本轮未召回）
    - existing None → return new
    - both → 按 id 去重合并（同 id 覆盖，新 id 追加）

    INVARIANT: 只存索引(id + score + strength)，不存全量内容
    (内容走 per-call HumanMessage 注入，保护 prompt caching)。

    Args:
        existing: 现有召回索引。
        new: 新增召回索引。

    Returns:
        list | None: 合并后的召回索引。
    """
    if new is None:
        return existing
    if existing is None:
        return list(new)
    merged = {item.get("id"): item for item in existing}
    for item in new:
        merged[item.get("id")] = item  # last-write-wins per id
    return list(merged.values())


def merge_memory_updates(existing: list | None, new: list | None) -> list | None:
    """Reducer for ThreadState.memory_updates — 追加（异步写后清理）。

    - new None → preserve existing
    - existing None → return new
    - both → 追加（不去重，同一轮可能多次 update）

    INVARIANT: 异步 cron 任务消费后清空（写 None）。

    Args:
        existing: 现有更新。
        new: 新增更新。

    Returns:
        list | None: 合并后的更新列表。
    """
    if new is None:
        return existing
    if existing is None:
        return list(new)
    return list(existing) + list(new)