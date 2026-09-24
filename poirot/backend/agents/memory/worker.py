"""MemoryWorker — 后台异步抽取 episodic + consolidate（L5）。

【整体职责】
daemon 线程消费队列，异步做「抽取 episodic + 合并 semantic」。
主链「写路径」的执行体：
ConsolidationMW.aafter_model → worker.submit(task) → worker 异步处理。

【核心机制】
- daemon 线程 + threading.Queue。
- LLM 构造注入（不反向依赖 app）。
- aafter_model 非阻塞 submit，worker 异步处理。
- 工具里无 LLM 原则不变：worker 在 middleware 层调 LLM，
  manager 仍只接受外部传入 content。

【职责边界】
- 本模块只负责「异步执行」，不负责何时触发（那是 ConsolidationMW 的事）。
- 不生成记忆：抽取靠 LLM prompt；合并靠 LLM prompt；manager 只负责落库。
- LLM 失败 log + 跳过，不重试，不影响主流程。
- 队列无界（L5 MVP，L6 加 maxsize）。

【INVARIANT】
- daemon 线程（进程退出自动结束）。
- 队列无界（L5 MVP）。
- 无重试（LLM 失败 log + 跳过）。
- set_turn_id 标 actor = "worker:{thread_id}:{turn_count}"。
- 工具里无 LLM 原则不变（worker 在 middleware 层调 LLM）。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any

from langchain_core.messages import BaseMessage

from poirot.backend.agents.memory.config import get_memory_config
from poirot.backend.agents.memory.schema import MemoryType
from poirot.backend.agents.memory.strategies.default.manager import (
    DefaultMemoryManager,
    set_turn_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM prompt 模板
# ---------------------------------------------------------------------------

# 抽取 prompt：让 LLM 从对话里抽取值得记住的 episodic 记忆，返回 JSON 数组。
_EXTRACT_PROMPT = """Analyze the following conversation and extract episodic memories worth remembering.
Return JSON array of {{"content": "...", "type": "episodic|semantic|procedural", "importance": 0.0-1.0}}.
Only extract noteworthy facts, decisions, or patterns. Skip trivial messages.
Return ONLY the JSON array, no preamble.

Conversation:
{conversation}
"""

# 合并 prompt：让 LLM 把多条记忆合并成一条 semantic 知识，只返回合并后的文本。
_CONSOLIDATE_PROMPT = """Merge the following {n} memories into one consolidated semantic knowledge.
Return only the merged content (no preamble, no JSON).

Memories:
{memories}
"""

# 单次 consolidate 的最大条数（与 _constants.CONSOLIDATE_PARAMS 的 max 一致）。
_MAX_CONSOLIDATE = 10


# ---------------------------------------------------------------------------
# 任务结构
# ---------------------------------------------------------------------------

@dataclass
class MemoryTask:
    """worker 队列任务结构。

    Args:
        thread_id:  会话 id（用于 actor 标识 + source）。
        messages:   最近 N 轮快照（已截断，由 ConsolidationMW 决定 N*2）。
        turn_count: 用户轮次数（用于 actor 标识）。
    """
    thread_id: str
    messages: list[BaseMessage]  # 最近 N 轮快照（已截断）
    turn_count: int


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class MemoryWorker:
    """后台 worker：异步抽取 episodic + consolidate。

    lifecycle：
    - start()  ：启动 daemon 线程。
    - submit() ：丢任务到队列（非阻塞）。
    - shutdown()：drain + 关闭。

    LLM 注入：构造时传 llm，避免反向依赖 app。
    错误处理：LLM 失败 log + 跳过，不影响主流程。
    """

    def __init__(self, manager: DefaultMemoryManager, llm: Any) -> None:
        """初始化。

        Args:
            manager: memory manager 实例，供 worker 调度 encode / consolidate。
            llm:     语言模型对象，由调用方注入（避免反向依赖 app）。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 存 manager / llm 到字段。
            2. 初始化空队列（无界）。
            3. 初始化 _thread 为 None（start 时创建）。
            4. 初始化 _stop 为 threading.Event（未 set）。
        """
        self._manager = manager
        self._llm = llm
        self._queue: Queue[MemoryTask] = Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """启动 daemon 线程。幂等（已启动返既有）。

        Args:
            无。

        Returns:
            None。

        Raises:
            不主动抛异常；Thread.start 异常按原逻辑传播。

        组装规则：
            1. 线程已存在且存活 → 直接返回（幂等）。
            2. 清除 _stop（允许新线程运行）。
            3. 创建 daemon Thread（target=_run, name="memory-worker"）。
            4. 启动线程。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="memory-worker", daemon=True,
        )
        self._thread.start()

    def submit(self, task: MemoryTask) -> None:
        """丢任务到队列（非阻塞）。

        Args:
            task: MemoryTask 实例。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. _queue.put(task)（无界队列，不阻塞）。
        """
        self._queue.put(task)

    def shutdown(self, timeout: float = 5.0) -> None:
        """drain + 关闭。

        Args:
            timeout: 等待 worker drain 的超时秒数，默认 5.0。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. set _stop（通知 _run 退出循环）。
            2. 线程存活 → join(timeout=timeout)（等剩余任务或超时）。
            3. _thread 置 None。
        """
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        """消费循环：循环取任务，交 _process，异常不崩。

        Args:
            无。

        Returns:
            None。

        Raises:
            不主动抛异常；_process 内部异常被捕获（log error，不中断循环）。

        组装规则：
            1. 循环直到 _stop 被 set。
            2. _queue.get(timeout=1.0)：超时（Empty）→ 继续循环。
            3. 取到 task → _process(task)。
            4. _process 抛异常 → log error（不中断循环）。
        """
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                self._process(task)
            except Exception as exc:
                logger.error(f"MemoryWorker task failed: {exc}", exc_info=True)

    def _process(self, task: MemoryTask) -> None:
        """单任务处理：抽取 + consolidate。

        Args:
            task: MemoryTask 实例。

        Returns:
            None。

        Raises:
            不主动抛异常；内部 try/finally 保证 set_turn_id(None) 清理。

        组装规则：
            1. set_turn_id("worker:{thread_id}:{turn_count}")（traceability C）。
            2. try：
               - _extract_and_encode(task)：LLM 抽取 + manager.encode。
               - _maybe_consolidate(task)：查总量 + LLM 合并 + manager.consolidate。
            3. finally：set_turn_id(None)（清理 actor）。
        """
        set_turn_id(f"worker:{task.thread_id}:{task.turn_count}")
        try:
            self._extract_and_encode(task)
            # consolidate 查 store 总量,不依赖本轮抽取结果
            self._maybe_consolidate(task)
        finally:
            set_turn_id(None)

    def _extract_and_encode(self, task: MemoryTask) -> list[str]:
        """LLM 抽取 episodic + manager.encode。返 encoded trace id 列表。

        Args:
            task: MemoryTask 实例。

        Returns:
            成功 encoded 的 trace id 列表；失败 / 无结果返空列表。

        Raises:
            不主动抛异常；LLM 调用 / JSON 解析 / encode 单项失败都 log + 跳过。

        组装规则：
            1. _format_messages(task.messages) → conversation。
            2. _EXTRACT_PROMPT.format(conversation=...) → prompt。
            3. self._llm.invoke(prompt)：
               - 解析失败（json.JSONDecodeError 或其他 Exception）→ log warning + 返 []。
            4. items 非 list → log warning + 返 []。
            5. 逐条 item（只处理 dict）：
               - manager.encode(content, type, importance, source=f"worker:{thread_id}")。
               - 成功 → ids.append(trace.id)。
               - 失败 → log warning（跳过单条）。
            6. 返回 ids。
        """
        conversation = self._format_messages(task.messages)
        prompt = _EXTRACT_PROMPT.format(conversation=conversation)
        try:
            response = self._llm.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            items = json.loads(content)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(f"worker: LLM extract failed (not JSON): {exc}")
            return []

        if not isinstance(items, list):
            logger.warning(f"worker: LLM extract not list, got {type(items)}")
            return []

        ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                trace = self._manager.encode(
                    content=str(item["content"]),
                    type=MemoryType(item.get("type", "episodic")),
                    importance=float(item.get("importance", 0.5)),
                    source=f"worker:{task.thread_id}",
                )
                ids.append(trace.id)
            except Exception as exc:
                logger.warning(f"worker: encode failed for item {item}: {exc}")
        return ids

    def _maybe_consolidate(self, task: MemoryTask) -> None:
        """检查 consolidate 条件 + LLM 生成 merged + manager.consolidate。

        查 store 中非 forgotten episodic trace 总量，
        总量 ≥ threshold 时取最旧 max=10 条做 consolidate。

        Args:
            task: MemoryTask 实例（本方法只用 thread_id / turn_count 记日志）。

        Returns:
            None。

        Raises:
            不主动抛异常；LLM 调用 / consolidate 失败都 log + 跳过。

        组装规则：
            1. config = get_memory_config()，threshold = phase2.trigger_every_n_turns。
            2. 查 store 中所有非 forgotten episodic trace（不是单轮候选数）。
            3. 总量 < threshold → 直接返回（不触发合并）。
            4. 按 created_at 升序取最旧 max=10 条（旧记忆优先合并）。
            5. 取到的 < 2 条 → 返回（无法合并）。
            6. 拼 memories_text → _CONSOLIDATE_PROMPT.format → prompt。
            7. self._llm.invoke(prompt)：
               - 失败 → log warning + 返回。
            8. manager.consolidate([ids], merged)：
               - 成功 → log info。
               - 失败 → log warning。
        """
        config = get_memory_config()
        threshold = int(config.phase2.get("trigger_every_n_turns", 10))

        # 查 store 中所有非 forgotten episodic trace(不是单轮候选数)
        all_episodic = [
            t for t in self._manager._store.list_by_type(MemoryType.EPISODIC)
            if not t.metadata.get("forgotten")
        ]
        if len(all_episodic) < threshold:
            return

        # 取最旧的 max=10 条(按 created_at 升序,旧记忆优先合并)
        all_episodic.sort(key=lambda t: t.created_at)
        to_consolidate = all_episodic[:_MAX_CONSOLIDATE]
        if len(to_consolidate) < 2:
            return

        memories_text = "\n".join(f"- {t.content}" for t in to_consolidate)
        prompt = _CONSOLIDATE_PROMPT.format(n=len(to_consolidate), memories=memories_text)
        try:
            response = self._llm.invoke(prompt)
            merged = response.content if hasattr(response, "content") else str(response)
        except Exception as exc:
            logger.warning(f"worker: LLM consolidate failed: {exc}")
            return

        try:
            self._manager.consolidate([t.id for t in to_consolidate], merged)
            logger.info(
                f"worker: consolidated {len(to_consolidate)} episodic traces "
                f"into 1 semantic (thread={task.thread_id} turn={task.turn_count})"
            )
        except Exception as exc:
            logger.warning(f"worker: consolidate failed: {exc}")

    @staticmethod
    def _format_messages(messages: list[BaseMessage]) -> str:
        """格式化 messages 为 LLM 输入文本。

        Args:
            messages: BaseMessage 列表。

        Returns:
            拼成 "{role}: {content}" 逐行文本。

        Raises:
            不主动抛异常。

        组装规则：
            1. 遍历 messages，每条：
               - role = m.__class__.__name__（如 HumanMessage / AIMessage）。
               - content = m.content（str 直接用，否则 str(...)）。
               - 拼 "{role}: {content}"。
            2. 用 "\\n" join 返回。
        """
        lines: list[str] = []
        for m in messages:
            role = m.__class__.__name__
            content = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"{role}: {content}")
        return "\n".join(lines)