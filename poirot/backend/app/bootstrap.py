"""app/bootstrap.py — 应用启动与运行时装配。

【整体职责】
Poirot 应用层的启动入口：加载配置、构造 LLM、装配工具（builtin / MCP / sandbox /
skill）、装配 multiagent、构造 CapabilityRegistry 与 LeaderAgent，产出 AppRuntime。
同时提供运行时热切换能力（expert 模式 / MCP 工具 / model），全部保持不可变语义。

【内容摘要】
- _PROJECT_ROOT                           : 项目根路径。
- _resolve_relative_paths()               : 把 config 里相对路径锚定到项目根。
- _build_chat_model()                     : 按 provider 构造 chat model。
- _check_node_available()                 : 检测 npx 是否可用（MCP 前置条件）。
- AppRuntime                              : 运行时容器（config + registry + leader + setup）。
- AppRuntime.run_question()               : 执行一次提问。
- AppRuntime.switch_expert_mode()         : 热切换 expert 模式（保留 thread）。
- AppRuntime.reload_mcp_tools()           : MCP 工具变更后重建 Leader。
- AppRuntime.switch_model()               : 热切换 LLM provider/model。
- _load_sandbox_provider()                : 反射加载 sandbox provider。
- _load_memory_provider()                 : 反射加载 memory provider。
- _build_path_mappings()                  : 从 config 构造 PathMapping 列表。
- _build_evolution_manager()              : 装配 skill 自进化管理器（Layer 2a）。
- _build_eval_layer()                     : 装配 skill L3 评估层。
- bootstrap_runtime()                     : ★ 应用启动主入口。

【职责边界】
- 只负责：配置加载、组件装配、运行时容器构造、热切换。
- 不负责：Agent 内部逻辑（leader / middleware 负责）、具体工具实现（各模块负责）、
  CLI 交互（app/cli 负责）。
- 不持有可变状态：AppRuntime 用 replace 语义重建，不原地修改。

【INVARIANT】
- 路径锚定：logs_root / externalize_dir / memory.storage_path 的相对路径统一锚到 _PROJECT_ROOT。
- thread 连续性：switch_expert_mode / reload_mcp_tools / switch_model 保留
  thread_id / thread_dir / thread_journal / capability_registry（checkpointer state 跨重建连续）。
- 不可变语义：三个 switch_* 方法都返回新 AppRuntime，不原地修改。
- 装配顺序固定：config → thread journal → LLM → builtin tools → MCP → sandbox →
  skill → multiagent → memory → CapabilityRegistry → LeaderAgent。
- subagent leaf 限制：_subagent_factory 构造的 leaf agent 不传 specialist_tools 与
  orchestration_middleware，从工具层面杜绝无限递归。
- multiagent 可选：enabled=false 时 setup_multiagent 返空 setup，Leader 行为不变。
- sandbox 可选：config.sandbox.use 为空时 sandbox_provider=None，不注册 sandbox 工具。
- skill / memory / MCP 均可选：未启用时相应组件为 None，不阻塞启动。
- 所有可选组件装配失败都记 journal，不抛异常（除 LLM 构造失败）。
- MCP 加载兼容运行中的 event loop：检测到 running loop 时用线程池跑 asyncio.run。
"""
from __future__ import annotations

import shutil
import sys
from contextvars import ContextVar
from dataclasses import dataclass, replace, field
from pathlib import Path
from uuid import uuid4
from typing import Any

from langchain_core.language_models import BaseChatModel

from poirot.backend.agents.artifacts.local_store import LocalArtifactStore
from poirot.backend.agents.capabilities.registry import CapabilityRegistry
from poirot.backend.agents.config.loader import load_config
from poirot.backend.agents.memory.bootstrap import (
    get_memory_worker,
    shutdown_memory_worker,
    start_memory_worker,
)
from poirot.backend.agents.memory.config import set_memory_config
from poirot.backend.agents.config.provider_config import ProviderConfig, get_provider_config, select_provider_config
from poirot.backend.agents.config.schema import AppConfig
from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.journal.run_journal import RunJournal
from poirot.backend.agents.leader.agent import AgentRunResult, LeaderAgent
from poirot.backend.agents.leader.factory import make_lead_agent
from poirot.backend.agents.reporting.markdown_reporter import MarkdownReporter
from poirot.backend.agents.reporting.agent_search import build_run_scope
from poirot.backend.agents.runtime.run_manager import RunManager
from poirot.backend.agents.agent_tools.available import get_available_tools, select_search_tool
from poirot.backend.agents.multiagent.bootstrap import MultiAgentSetup, setup_multiagent
from poirot.backend.agents.multiagent.config import load_multiagent_config
from poirot.backend.agents.runtime.checkpointer import SQLiteCheckpointer, SessionCheckpointer
from poirot.backend.agents.runtime.threads import ThreadStore, validate_thread_id
from poirot.backend.agents.runtime.projects import ProjectMetadata, ProjectStore

# 项目根路径（app/bootstrap.py 的上三级）。
_PROJECT_ROOT = Path(__file__).parents[3]
_SANDBOX_THREAD_STORE: ContextVar[ThreadStore | None] = ContextVar(
    "sandbox_thread_store", default=None
)


def _resolve_relative_paths(config: AppConfig) -> AppConfig:
    """把 config 里的相对路径锚定到 _PROJECT_ROOT。

    覆盖：
    - context_governance.params.externalize_dir（默认 .poirot/externalized）
    - memory.storage_path

    目的：避免用户在不同 CWD 启动时，这些目录被解析到意外位置（如用户家目录），
    导致项目目录下看不到外化记录 / 记忆文件。
    """
    params = dict(config.context_governance.params)
    ext_dir = params.get("externalize_dir", ".poirot/externalized")
    p = Path(ext_dir)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / p).resolve()
    params["externalize_dir"] = str(p)
    # L4 memory.storage_path 锚定 _PROJECT_ROOT（与 externalize_dir 同款）
    memory_path = Path(config.memory.storage_path)
    if not memory_path.is_absolute():
        memory_path = (_PROJECT_ROOT / memory_path).resolve()
    config = replace(config, memory=replace(config.memory, storage_path=str(memory_path)))
    return replace(
        config,
        context_governance=replace(config.context_governance, params=params),
    )


def _build_chat_model(config: ProviderConfig) -> BaseChatModel:
    """按 provider 构造 chat model。

    - deepseek → ChatDeepSeek
    - openai / qwen → ChatOpenAI（qwen 走 OpenAI 兼容接口，可带 base_url）
    - 其他 → ValueError
    """
    config.require_api_key()
    if config.provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(model=config.model, api_key=config.api_key)
    if config.provider in ("openai", "qwen"):
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {"model": config.model, "api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOpenAI(**kwargs)
    raise ValueError(f"unsupported provider: {config.provider}")


def _check_node_available() -> bool:
    """检测 npx 是否可用（MCP 加载的前置条件）。"""
    return shutil.which("npx") is not None


@dataclass
class AppRuntime:
    """应用运行时容器。

    持有 config / registry / leader_agent / multiagent_setup 等组件；
    提供 run_question 与三种热切换方法。
    """

    config: AppConfig
    capability_registry: CapabilityRegistry
    run_manager: RunManager
    researcher_model_name: str
    thread_id: str
    thread_dir: Path
    thread_journal: RunJournal
    leader_agent: LeaderAgent
    mcp_manager: Any = None
    artifact_server: Any = None
    skill_manager: Any = None
    multiagent_setup: MultiAgentSetup | None = None
    thread_store: ThreadStore | None = None
    checkpointer: SQLiteCheckpointer | SessionCheckpointer | None = None
    active_threads: set[str] = field(default_factory=set)
    project: ProjectMetadata | None = None
    project_store: ProjectStore | None = None
    thread_persisted: bool = True

    def ensure_thread_persisted(self) -> None:
        """Commit a newly opened interactive thread before graph execution."""
        if self.thread_persisted or self.thread_store is None:
            return
        item = self.thread_store.materialize(self.thread_id)
        self.thread_dir = self.thread_store.session_dir(self.thread_id)
        self.thread_journal = RunJournal(self.thread_id, self.thread_dir / "thread-events.jsonl")
        self.thread_journal.append("thread.started", {"project": item.project, "cwd": item.cwd})
        self.thread_persisted = True

    def begin_turn(self, question: str) -> None:
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        self.ensure_thread_persisted()
        if self.thread_store is not None:
            self.thread_store.update(self.thread_id, first_message=question)
        self.active_threads.add(self.thread_id)

    def end_turn(self) -> None:
        self.active_threads.discard(self.thread_id)
        if self.thread_store is not None:
            self.thread_store.update(self.thread_id)

    def prepare_question(self, question: str, *, choose=None, thread_id: str | None = None):
        """Resolve one-turn file and same-directory thread references."""
        from poirot.backend.agents.runtime.file_access import (
            PreparedQuestion,
            ThreadFileAccess,
            extract_references,
            prepare_question,
        )
        from poirot.backend.agents.runtime.thread_quote import parse_thread_references, quote_threads

        effective_thread_id = thread_id or self.thread_id
        if not parse_thread_references(question) and not extract_references(question):
            return PreparedQuestion(question, question)
        current = self.thread_store.require(effective_thread_id) if self.thread_store else None
        access = None
        if self.thread_store and self.checkpointer:
            access = ThreadFileAccess(current.cwd if current else None, current.extra_dirs if current else ()) if parse_thread_references(question) else None
            thread_result = quote_threads(
                question,
                current=current,
                thread_store=self.thread_store,
                checkpointer=self.checkpointer,
                file_name_conflict=lambda value: bool(access.search_filename(value)),
            )
        else:
            thread_result = None
        parse_question = thread_result.masked_question if thread_result else question
        if not extract_references(parse_question):
            suffix = thread_result.enriched_suffix if thread_result else ""
            enriched = question + ("\n\n" + suffix if suffix else "")
            return PreparedQuestion(question, enriched, thread_references=thread_result.quotes if thread_result else ())
        if access is None:
            access = ThreadFileAccess(current.cwd if current else None, current.extra_dirs if current else ())
        prepared_files = prepare_question(parse_question, access, choose=choose)
        file_suffix = prepared_files.enriched[len(parse_question):]
        enriched = question + file_suffix
        if thread_result and thread_result.enriched_suffix:
            enriched += "\n\n" + thread_result.enriched_suffix
        return PreparedQuestion(question, enriched, prepared_files.references, thread_result.quotes if thread_result else ())

    async def aprepare_question(self, question: str, *, choose=None, thread_id: str | None = None):
        """Async ``@`` preparation, allowing the UI to select duplicate files."""
        from poirot.backend.agents.runtime.file_access import (
            PreparedQuestion,
            ThreadFileAccess,
            extract_references,
            prepare_question_async,
        )
        from poirot.backend.agents.runtime.thread_quote import parse_thread_references, quote_threads

        effective_thread_id = thread_id or self.thread_id
        if not parse_thread_references(question) and not extract_references(question):
            return PreparedQuestion(question, question)
        current = self.thread_store.require(effective_thread_id) if self.thread_store else None
        access = None
        if self.thread_store and self.checkpointer:
            access = ThreadFileAccess(current.cwd if current else None, current.extra_dirs if current else ()) if parse_thread_references(question) else None
            thread_result = quote_threads(
                question,
                current=current,
                thread_store=self.thread_store,
                checkpointer=self.checkpointer,
                file_name_conflict=lambda value: bool(access.search_filename(value)),
            )
        else:
            thread_result = None
        parse_question = thread_result.masked_question if thread_result else question
        if not extract_references(parse_question):
            suffix = thread_result.enriched_suffix if thread_result else ""
            enriched = question + ("\n\n" + suffix if suffix else "")
            return PreparedQuestion(question, enriched, thread_references=thread_result.quotes if thread_result else ())
        if access is None:
            access = ThreadFileAccess(current.cwd if current else None, current.extra_dirs if current else ())
        prepared_files = await prepare_question_async(parse_question, access, choose=choose)
        file_suffix = prepared_files.enriched[len(parse_question):]
        enriched = question + file_suffix
        if thread_result and thread_result.enriched_suffix:
            enriched += "\n\n" + thread_result.enriched_suffix
        return PreparedQuestion(question, enriched, prepared_files.references, thread_result.quotes if thread_result else ())

    def switch_thread(self, thread_id: str) -> AppRuntime:
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None or self.checkpointer is None:
            raise RuntimeError("Thread storage is unavailable")
        self.thread_store.require(thread_id)
        if thread_id == self.thread_id:
            return self
        # Decode before replacing the active runtime; corrupt checkpoints remain untouched.
        self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        item = self.thread_store.require(thread_id)
        target_pending = self.thread_store.is_pending(thread_id)
        # Keep the legacy runtime project label for unbound threads, while all
        # thread-scoped cwd/sandbox decisions continue to use target metadata.
        project = self.project_store.get(item.project) if item.project and self.project_store else self.project
        if item.project and (project is None or project.dir != item.cwd):
            raise ValueError(f"Thread project binding is unavailable: {item.project}")
        if target_pending:
            thread_dir = Path(self.config.runtime.logs_root) / "pending-threads" / thread_id
        else:
            thread_dir = self.thread_store.session_dir(thread_id)
        journal = RunJournal(thread_id, thread_dir / "thread-events.jsonl")
        journal.append("thread.resumed", {"previous_thread_id": self.thread_id})
        if not self.thread_persisted:
            self.thread_store.delete(self.thread_id)
        # ContextVar is process-local; never carry the previous thread's
        # sandbox into a newly selected thread.
        from poirot.backend.agents.sandbox.integration.context import get_sandbox_id, set_sandbox_id
        previous_sandbox_id = get_sandbox_id()
        set_sandbox_id(None)
        if previous_sandbox_id:
            provider = getattr(self.capability_registry, "sandbox_provider", None)
            if provider is not None:
                try:
                    provider.release(previous_sandbox_id)
                except Exception:
                    pass
        return replace(self, thread_id=thread_id, thread_dir=thread_dir, thread_journal=journal,
                       project=project, thread_persisted=not target_pending)

    def cd(self, directory: str | Path) -> AppRuntime:
        """Create and activate a new thread bound to ``directory``.

        The current thread is never mutated. Relative paths are resolved from
        its persisted cwd; an unbound legacy thread must use an absolute path.
        """
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None or self.project_store is None:
            raise RuntimeError("Project storage is unavailable")
        current = self.thread_store.require(self.thread_id)
        raw = Path(directory).expanduser()
        if not raw.is_absolute() and current.cwd is None:
            raise ValueError("Current thread has no cwd; /cd requires an absolute path")
        from poirot.backend.agents.runtime.projects import normalize_project_dir

        target_dir = normalize_project_dir(
            raw,
            base=Path(current.cwd) if current.cwd is not None else None,
        )
        target_project = self.project_store.ensure(target_dir)
        item = self.thread_store.create(
            project=target_project.project_name,
            cwd=target_project.dir,
            persist=False,
        )
        try:
            changed = self.switch_thread(item.thread_id)
            return replace(changed, project=target_project)
        except BaseException:
            self.thread_store.delete(item.thread_id)
            raise

    def new_thread(self) -> AppRuntime:
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None:
            raise RuntimeError("Thread storage is unavailable")
        item = self.thread_store.create(
            project=self.project.project_name if self.project else None,
            cwd=self.project.dir if self.project else None,
            persist=False,
        )
        try:
            return self.switch_thread(item.thread_id)
        except BaseException:
            self.thread_store.delete(item.thread_id)
            raise

    def switch_project(self, project_name: str) -> AppRuntime:
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.project_store is None or self.thread_store is None:
            raise RuntimeError("Project storage is unavailable")
        target = self.project_store.get(project_name)
        if target is None:
            raise KeyError(f"Project not found: {project_name}")
        from poirot.backend.agents.runtime.projects import normalize_project_dir
        normalize_project_dir(target.dir)
        item = self.thread_store.create(project=target.project_name, cwd=target.dir, persist=False)
        try:
            changed = self.switch_thread(item.thread_id)
            return replace(changed, project=target)
        except BaseException:
            self.thread_store.delete(item.thread_id)
            raise

    def switch_project_thread(self, thread_id: str) -> AppRuntime:
        if self.project is None or self.thread_store is None:
            raise RuntimeError("No current project")
        item = self.thread_store.require(thread_id)
        if item.project != self.project.project_name or item.cwd != self.project.dir:
            raise ValueError("Thread does not belong to the current project")
        return self.switch_thread(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        if thread_id in self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if thread_id == self.thread_id:
            raise ValueError("Cannot delete current thread; use /thread new or switch first")
        if self.thread_store is None or self.checkpointer is None:
            raise RuntimeError("Thread storage is unavailable")
        self.thread_store.require(thread_id)
        self.checkpointer.delete_thread(thread_id)
        try:
            self.thread_store.delete(thread_id)
        except Exception as exc:
            raise RuntimeError(f"Delete incomplete; checkpoint cleared but metadata remains: {exc}") from exc

    def rename_thread(self, title: str):
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None:
            raise RuntimeError("Thread storage is unavailable")
        return self.thread_store.update(self.thread_id, title=title)

    def add_reference_dir(self, directory: str | Path):
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None:
            raise RuntimeError("Thread storage is unavailable")
        return self.thread_store.add_extra_dir(self.thread_id, directory)

    def remove_reference_dir(self, directory: str | Path):
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is None:
            raise RuntimeError("Thread storage is unavailable")
        return self.thread_store.remove_extra_dir(self.thread_id, directory)

    def close(self) -> None:
        if self.checkpointer is not None:
            self.checkpointer.close()

    def generate_report(self, topic: str | None = None):
        """Create a user-level manual report snapshot for the current thread.

        ``topic`` is the report title for the ``/report`` command.  The current
        thread, project, cwd, checkpoint and sandbox are left unchanged.
        """
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        from poirot.backend.agents.reporting import persist_report_from_thread

        return persist_report_from_thread(self, title=topic)

    def build_search_scope(self, thread_id: str | None, user_text: str | None) -> dict[str, Any]:
        """Build this run's report retrieval scope for ``search_reports``.

        Every graph entry point (one-shot ``run``, interactive CLI, TUI) must
        call this so the tool reads the *current* thread metadata instead of a
        cached or missing scope.  Cross-project authorization comes from the raw
        user message only, never from model arguments.
        """
        thread = self.thread_store.require(thread_id) if (self.thread_store and thread_id) else None
        return build_run_scope(
            thread_id=thread_id,
            project=thread.project if thread else None,
            cwd=thread.cwd if thread else None,
            storage_root=self.config.runtime.storage_root,
            user_text=user_text,
        )

    def run_question(
        self,
        question: str,
        thread_id: str | None = None,
        user_id: str | None = "default-user",
        run_id: str | None = None,
    ) -> AgentRunResult:
        """执行一次提问：建 run context → 调 leader_agent.run → 标记成功/失败。"""
        effective_thread_id = thread_id or self.thread_id
        validate_thread_id(effective_thread_id)
        if self.active_threads:
            raise RuntimeError("A conversation is running; wait for it to finish")
        if self.thread_store is not None and self.thread_store.get(effective_thread_id) is None:
            self.thread_store.create(effective_thread_id, project=self.project.project_name if self.project else None,
                                     cwd=self.project.dir if self.project else None)
        if effective_thread_id == self.thread_id:
            self.ensure_thread_persisted()
        thread = self.thread_store.require(effective_thread_id) if self.thread_store else None
        prepared = self.prepare_question(question, thread_id=effective_thread_id)
        context = self.run_manager.create_run(
            thread_id=effective_thread_id,
            user_id=user_id,
            run_id=run_id,
            model_name=self.researcher_model_name,
            thread_dir=self.thread_store.session_dir(effective_thread_id) if self.thread_store else self.thread_dir,
            project=thread.project if thread else None,
            cwd=thread.cwd if thread else None,
        )
        self.run_manager.mark_running(context.run_id)
        try:
            if self.thread_store is not None:
                self.thread_store.update(effective_thread_id, first_message=prepared.original)
            self.active_threads.add(effective_thread_id)
            # search_reports 的范围每轮从当前 thread 元数据重建：project / cwd 不缓存，
            # 跨项目授权只看原始用户问题，模型无法自行扩大范围。
            search_scope = self.build_search_scope(effective_thread_id, prepared.original)
            if self.thread_store is None:
                result = self.leader_agent.run(
                    prepared.enriched, context, title_question=prepared.original, search_scope=search_scope,
                )
            else:
                result = self.leader_agent.run(
                    prepared.enriched,
                    context,
                    thread_title=self.thread_store.require(effective_thread_id).title,
                    title_question=prepared.original,
                    search_scope=search_scope,
                )
            self.run_manager.mark_success(context.run_id)
            return result
        except Exception as exc:
            self.run_manager.mark_failed(context.run_id, str(exc))
            raise
        finally:
            self.active_threads.discard(effective_thread_id)
            if self.thread_store is not None:
                self.thread_store.update(effective_thread_id)

    def switch_expert_mode(self, expert_mode: bool) -> AppRuntime:
        """切换 expert 模式，精准重建受影响部分，保留 thread 连续性。

        重建：config + run_manager + leader_agent（依赖 expert_mode 编译参数）。
        保留：thread_id / thread_dir / thread_journal / capability_registry /
        researcher_model_name。

        注意：必须传 context_governance，否则治理层中间件不挂，budget / 压缩全部失效。

        返回新 AppRuntime 实例（不可变语义）。
        """
        new_config = load_config(expert_mode=expert_mode)
        logs_root = Path(new_config.runtime.logs_root)
        if not logs_root.is_absolute():
            logs_root = _PROJECT_ROOT / logs_root
        new_config = replace(
            new_config,
            runtime=replace(
                new_config.runtime,
                logs_root=self.config.runtime.logs_root,
                storage_root=self.config.runtime.storage_root,
            ),
        )
        # 锚定 externalize_dir 等治理层相对路径到项目根
        new_config = _resolve_relative_paths(new_config)
        # 必须传 context_governance，否则 _build_middlewares 跳过治理层
        new_leader = make_lead_agent(
            expert_mode=expert_mode,
            capability_registry=self.capability_registry,
            context_governance=new_config.context_governance,
            sandbox_provider=getattr(self.capability_registry, "sandbox_provider", None),
            artifact_server=self.artifact_server,
            mcp_audit_middleware=self.mcp_manager.get_audit_middleware() if self.mcp_manager else None,
            skill_injection_middleware=self.skill_manager.get_injection_middleware() if self.skill_manager else None,
            skill_metrics_middleware=self.skill_manager.get_metrics_middleware() if self.skill_manager else None,
            specialist_tools=list(self.multiagent_setup.specialist_tools) if self.multiagent_setup else None,
            orchestration_middleware=self.multiagent_setup.orchestration_middleware if self.multiagent_setup else None,
            memory_provider=getattr(self.capability_registry, "memory_provider", None),
            memory_config=self.config.memory,
            memory_worker=get_memory_worker(),
            checkpointer=self.checkpointer,
        )
        self.thread_journal.append("mode.switched", {
            "expert_mode": expert_mode,
            "thread_id": self.thread_id,
        })
        return AppRuntime(
            config=new_config,
            capability_registry=self.capability_registry,
            run_manager=RunManager(new_config),
            researcher_model_name=self.researcher_model_name,
            thread_id=self.thread_id,
            thread_dir=self.thread_dir,
            thread_journal=self.thread_journal,
            leader_agent=new_leader,
            mcp_manager=self.mcp_manager,
            artifact_server=self.artifact_server,
            skill_manager=self.skill_manager,
            multiagent_setup=self.multiagent_setup,
            thread_store=self.thread_store,
            checkpointer=self.checkpointer,
            active_threads=self.active_threads,
            project=self.project,
            project_store=self.project_store,
            thread_persisted=self.thread_persisted,
        )

    def reload_mcp_tools(self) -> AppRuntime:
        """MCP 工具变更后重建 LeaderAgent graph。

        复用 switch_expert_mode 模式：重建 leader_agent（新工具注入），
        保留 thread_id / thread_dir / thread_journal / capability_registry。
        同步完成（<1s），下轮可用（当前轮用旧 graph 跑完）。
        """
        expert_mode = self.config.runtime.expert_mode if hasattr(self.config.runtime, "expert_mode") else False
        new_leader = make_lead_agent(
            expert_mode=expert_mode,
            capability_registry=self.capability_registry,
            context_governance=self.config.context_governance,
            sandbox_provider=getattr(self.capability_registry, "sandbox_provider", None),
            artifact_server=self.artifact_server,
            mcp_audit_middleware=self.mcp_manager.get_audit_middleware() if self.mcp_manager else None,
            skill_injection_middleware=self.skill_manager.get_injection_middleware() if self.skill_manager else None,
            skill_metrics_middleware=self.skill_manager.get_metrics_middleware() if self.skill_manager else None,
            specialist_tools=list(self.multiagent_setup.specialist_tools) if self.multiagent_setup else None,
            orchestration_middleware=self.multiagent_setup.orchestration_middleware if self.multiagent_setup else None,
            memory_provider=getattr(self.capability_registry, "memory_provider", None),
            memory_config=self.config.memory,
            memory_worker=get_memory_worker(),
            checkpointer=self.checkpointer,
        )
        self.thread_journal.append("mcp.tools_reloaded", {"thread_id": self.thread_id})
        return AppRuntime(
            config=self.config,
            capability_registry=self.capability_registry,
            run_manager=self.run_manager,
            researcher_model_name=self.researcher_model_name,
            thread_id=self.thread_id,
            thread_dir=self.thread_dir,
            thread_journal=self.thread_journal,
            leader_agent=new_leader,
            mcp_manager=self.mcp_manager,
            artifact_server=self.artifact_server,
            skill_manager=self.skill_manager,
            multiagent_setup=self.multiagent_setup,
            thread_store=self.thread_store,
            checkpointer=self.checkpointer,
            active_threads=self.active_threads,
            project=self.project,
            project_store=self.project_store,
            thread_persisted=self.thread_persisted,
        )

    def switch_model(self, provider: str, model: str | None = None) -> AppRuntime:
        """热切换 LLM provider/model。

        重建 researcher+reporter model + capability_registry + leader_agent，
        保留 thread_id / thread_dir / thread_journal / mcp_manager / artifact_server /
        skill_manager / sandbox_provider（checkpointer state 跨切换连续）。

        等价于 CLI --provider X --model Y 重启，但不丢 thread。
        从指定 provider 开始走 researcher 的 FallbackChatModel 路由链；reporter
        仍使用 reporter 自己的默认 FallbackChatModel 路由。

        provider 必须是 MODEL_PROVIDERS 里 enabled 的项；model=None 用 provider 默认 model。

        返回新 AppRuntime（不可变语义）。
        """
        from poirot.backend.agents.config.model_router import ModelRouter

        router = ModelRouter()
        new_model = router.build_from(provider, model, role="researcher")
        new_reporter = router.build_model("reporter")
        new_registry = CapabilityRegistry(
            models={"researcher": new_model, "reporter": new_reporter},
            tools=self.capability_registry.tools,
            reporter=self.capability_registry.reporter,
            artifact_store=self.capability_registry.artifact_store,
            sandbox_provider=self.capability_registry.sandbox_provider,
            skill_store=self.capability_registry.skill_store,
            specialist_registry=self.capability_registry.specialist_registry,
            subagent_provider=self.capability_registry.subagent_provider,
            memory_provider=self.capability_registry.memory_provider,
        )
        expert_mode = self.config.runtime.expert_mode
        new_leader = make_lead_agent(
            expert_mode=expert_mode,
            capability_registry=new_registry,
            context_governance=self.config.context_governance,
            sandbox_provider=getattr(new_registry, "sandbox_provider", None),
            artifact_server=self.artifact_server,
            mcp_audit_middleware=self.mcp_manager.get_audit_middleware() if self.mcp_manager else None,
            skill_injection_middleware=self.skill_manager.get_injection_middleware() if self.skill_manager else None,
            skill_metrics_middleware=self.skill_manager.get_metrics_middleware() if self.skill_manager else None,
            specialist_tools=list(self.multiagent_setup.specialist_tools) if self.multiagent_setup else None,
            orchestration_middleware=self.multiagent_setup.orchestration_middleware if self.multiagent_setup else None,
            memory_provider=getattr(self.capability_registry, "memory_provider", None),
            memory_config=self.config.memory,
            memory_worker=get_memory_worker(),
            checkpointer=self.checkpointer,
        )
        self.thread_journal.append("model.switched", {
            "provider": provider,
            "model": model or "default",
            "thread_id": self.thread_id,
        })
        return AppRuntime(
            config=self.config,
            capability_registry=new_registry,
            run_manager=self.run_manager,
            researcher_model_name=model or provider,
            thread_id=self.thread_id,
            thread_dir=self.thread_dir,
            thread_journal=self.thread_journal,
            leader_agent=new_leader,
            mcp_manager=self.mcp_manager,
            artifact_server=self.artifact_server,
            skill_manager=self.skill_manager,
            multiagent_setup=self.multiagent_setup,
            thread_store=self.thread_store,
            checkpointer=self.checkpointer,
            active_threads=self.active_threads,
            project=self.project,
            project_store=self.project_store,
            thread_persisted=self.thread_persisted,
        )


def _load_sandbox_provider(config: AppConfig, thread_store: ThreadStore | None = None) -> Any:
    """反射加载 sandbox provider。config.sandbox.use 为空则返回 None。"""
    sandbox_config = config.sandbox
    if not sandbox_config.use:
        return None
    thread_store = thread_store or _SANDBOX_THREAD_STORE.get()
    import importlib

    module_path, _, class_name = sandbox_config.use.partition(":")
    module = importlib.import_module(module_path)
    provider_cls = getattr(module, class_name)
    path_mappings = _build_path_mappings(sandbox_config)
    def resolve_thread_cwd(thread_id: str) -> str | None:
        item = thread_store.get(thread_id) if thread_store is not None else None
        return item.cwd if item is not None else None

    return provider_cls(
        path_mappings=path_mappings,
        sandbox_config=sandbox_config,
        thread_cwd_resolver=resolve_thread_cwd,
    )


def _load_memory_provider(config: AppConfig) -> Any:
    """反射加载 memory provider。config.memory.use 为空则返回 None。

    config.memory.use="default" 时调 get_memory_provider()（内部 build_default_provider）。
    """
    memory_config = config.memory
    if not memory_config.use:
        return None
    from poirot.backend.agents.memory.bootstrap import get_memory_provider

    return get_memory_provider()


def _build_path_mappings(sandbox_config: Any) -> list:
    """从 config 构造 PathMapping 列表。路径锚定 .poirot/sandbox/local/（类型分层）。"""
    from poirot.backend.agents.sandbox.types import PathMapping

    sandbox_root = _PROJECT_ROOT / ".poirot" / "sandbox" / "local"
    mappings = [
        PathMapping("/mnt/poirot/user-data/workspace", str(sandbox_root / "workspace")),
        PathMapping("/mnt/poirot/user-data/uploads", str(sandbox_root / "uploads")),
        PathMapping("/mnt/poirot/user-data/outputs", str(sandbox_root / "outputs")),
    ]
    for mount in sandbox_config.mounts:
        mappings.append(PathMapping(mount.container_path, mount.host_path, mount.read_only))
    return mappings


def _build_evolution_manager(skill_manager: Any, llm: Any, journal: Any) -> Any:
    """建 EvolutionManager（Layer 2a）注入 SkillManager。

    lazy import evolution 模块（避免 skill → evolution → skill 循环依赖）。
    """
    from poirot.backend.agents.skill.evolution.focus.ive_focuser import IVEFocuser
    from poirot.backend.agents.skill.evolution.eval.programmatic_bridge import (
        ProgrammaticEvalBridge,
    )
    from poirot.backend.agents.skill.evolution.gates.score_delta_gate import ScoreDeltaGate
    from poirot.backend.agents.skill.evolution.manager import EvolutionManager
    from poirot.backend.agents.skill.evolution.mutators.llm_mutator import LLMMutator
    from poirot.backend.agents.skill.evolution.triggers.capture_trigger import (
        CaptureTrigger,
    )
    from poirot.backend.agents.skill.evolution.triggers.metric_monitor import (
        MetricMonitorTrigger,
    )

    cfg = skill_manager.config
    triggers = [
        MetricMonitorTrigger(
            threshold=cfg.evolve_threshold,
            min_selections=cfg.evolve_min_selections,
            cooldown_turns=cfg.evolve_cooldown_turns,
            llm=llm,
        ),
        CaptureTrigger(),
    ]
    return EvolutionManager(
        store=skill_manager.store,
        triggers=triggers,
        focuser=IVEFocuser(llm=llm),
        mutator=LLMMutator(max_changed_lines=cfg.evolve_mutate_budget, max_steps=cfg.evolve_max_steps, llm=llm),
        eval_bridge=ProgrammaticEvalBridge(),
        gate=ScoreDeltaGate(),
        llm=llm,
        journal=journal,
    )


def _build_eval_layer(skill_manager: Any, llm: Any) -> Any:
    """建 L3 EvalLayer 注入 SkillManager。

    lazy import eval 模块（避免循环依赖）。
    若 EvolutionManager 已装配，替换其 eval_bridge 为 RegistryEvalBridge。
    """
    from poirot.backend.agents.skill.eval import EvalLayer
    from poirot.backend.agents.skill.eval.analyzers.contract_compiler import ContractCompiler
    from poirot.backend.agents.skill.eval.analyzers.response_contract_checker import (
        ResponseContractChecker,
    )
    from poirot.backend.agents.skill.eval.analyzers.skill_judgment_analyzer import (
        SkillJudgmentAnalyzer,
    )
    from poirot.backend.agents.skill.eval.analyzers.task_quality_judge import (
        TaskQualityJudge,
    )
    from poirot.backend.agents.skill.eval.registry import EvalRegistry, RegistryEvalBridge
    from poirot.backend.agents.skill.eval.runtime_tracker import RuntimeTracker

    cfg = skill_manager.config.eval_config
    store = skill_manager.store

    checker = ResponseContractChecker(ContractCompiler())
    registry = EvalRegistry(checker)
    bridge = RegistryEvalBridge(registry)

    judgment_analyzer = SkillJudgmentAnalyzer(llm, store) if cfg.judgment_enabled else None
    task_judge = TaskQualityJudge(llm, store) if cfg.task_judge_enabled else None
    runtime_tracker = RuntimeTracker(store, cfg.degradation_delta)

    # 替换 EvolutionManager 的 eval_bridge（若已装配）
    evo = skill_manager.get_evolution_manager()
    if evo is not None:
        evo._eval_bridge = bridge

    return EvalLayer(
        bridge=bridge,
        judgment_analyzer=judgment_analyzer,
        task_judge=task_judge,
        runtime_tracker=runtime_tracker,
    )


def bootstrap_runtime(
    expert_mode: bool = False,
    provider: str | None = None,
    model: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    thread_id: str | None = None,
    project_dir: str | Path | None = None,
    project_name: str | None = None,
) -> AppRuntime:
    """★ 应用启动主入口：装配所有组件，返回 AppRuntime。

    装配顺序：
    1. 加载 config + 锚定相对路径。
    2. 建 thread journal。
    3. 构造 LLM（默认完整路由，或从指定 provider 开始的降级链）。
    4. 加载 builtin 工具。
    5. 加载 MCP 工具（npx 可用时）。
    6. 装配 sandbox（config.sandbox.use 非空时）。
    7. 装配 skill（含 evolution / eval，可选）。
    8. 装配 multiagent（enabled=true 时；含 subagent factory）。
    9. 装配 memory。
    10. 构造 CapabilityRegistry。
    11. 构造 LeaderAgent。
    12. 打包 AppRuntime。
    """
    config = load_config(expert_mode=expert_mode, cli_overrides=cli_overrides)
    logs_root = Path(config.runtime.logs_root)
    if not logs_root.is_absolute():
        logs_root = _PROJECT_ROOT / logs_root
    config = replace(
        config,
        runtime=replace(config.runtime, logs_root=str(logs_root)),
    )
    # 锚定 externalize_dir 等治理层相对路径到项目根
    config = _resolve_relative_paths(config)

    # ── Thread-level setup：journal 在 MCP/LLM 加载之前创建 ──
    thread_store = ThreadStore(config.runtime.storage_root)
    project_store = thread_store.projects
    initial_project = project_store.ensure(project_dir, project_name)
    project: ProjectMetadata | None = initial_project
    thread_id = validate_thread_id(thread_id) if thread_id else str(uuid4())
    existing_thread = thread_store.get(thread_id)
    thread_persisted = existing_thread is not None
    if existing_thread is None:
        # Reserve the ID in memory. Metadata/index/checkpoint files are only
        # created once the user submits the first chat message.
        thread_store.create(
            thread_id,
            project=initial_project.project_name,
            cwd=initial_project.dir,
            persist=False,
        )
        existing_thread = None
    elif existing_thread.project is not None:
        project = project_store.get(existing_thread.project)
        if project is None or project.dir != existing_thread.cwd:
            raise ValueError(f"Thread project binding is unavailable: {existing_thread.project}")
    else:
        # An old unbound thread must not inherit the startup directory/project.
        project = None
    if thread_persisted:
        thread_dir = thread_store.session_dir(thread_id)
    else:
        # Keep startup diagnostics outside the persisted session tree. The
        # session directory is created only by ensure_thread_persisted().
        thread_dir = Path(config.runtime.logs_root) / "pending-threads" / thread_id
    thread_journal = RunJournal(
        run_id=thread_id,
        events_path=thread_dir / "thread-events.jsonl",
    )
    thread_journal.append("thread.started", {
        "expert_mode": expert_mode,
        "provider": provider or "default",
    })

    # ── LLM 构造：角色化智能路由（deepseek 兜底），或从 CLI provider 开始 ──
    from poirot.backend.agents.config.model_router import ModelRouter

    router = ModelRouter()
    if provider:
        researcher_model = router.build_from(provider, model, role="researcher")
        reporter_model = router.build_model("reporter")
        thread_journal.append("llm.constructed", {
            "mode": "from_provider",
            "provider": provider,
            "model": model or "default",
            "researcher_chain": researcher_model.provider_names,
            "reporter_chain": router.chain_names("reporter"),
        })
        researcher_model_name = model or provider
    else:
        researcher_model = router.build_model("researcher")
        reporter_model = router.build_model("reporter")
        researcher_chain_names = router.chain_names("researcher");
        reporter_chain_names = router.chain_names("reporter");
        
        thread_journal.append("llm.constructed", {
            "mode": "routed",
            "provider": get_provider_config(researcher_chain_names[0]).provider or "default",
            "model": get_provider_config(researcher_chain_names[0]).model or "default",
            "researcher_chain": researcher_chain_names,
            "reporter_chain": reporter_chain_names,
        })
        researcher_model_name = "routed:" + ",".join(researcher_chain_names)

    # ── MCP 工具加载：通过 McpManager 门面，配置化 + 熔断器 + fallback ──
    tools: dict[str, Any] = {}

    # builtin 工具（ddg_search / read_snapshot）——始终注册，MCP 未启用时的唯一搜索来源。
    builtin_tools = get_available_tools(groups=["core"])
    for t in builtin_tools:
        tools[t.name] = t
    thread_journal.append("builtin.tools_loaded", {"tools": list(tools.keys())})

    mcp_manager = None
    mcp_audit_middleware = None
    if _check_node_available():
        try:
            from poirot.backend.agents.mcp import build_mcp_manager

            mcp_manager = build_mcp_manager()
            if mcp_manager is not None:
                import asyncio

                try:
                    asyncio.get_running_loop()
                    # 已在 event loop 中：用线程池跑 asyncio.run
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(asyncio.run, mcp_manager.load_startup()).result()
                except RuntimeError:
                    asyncio.run(mcp_manager.load_startup())
                mcp_tools = mcp_manager.get_tools(["core", "deferred"])
                for tool in mcp_tools:
                    tools[tool.name] = tool
                search_tool = select_search_tool(mcp_tools)
                if search_tool:
                    tools["web_search_mcp"] = search_tool
                mcp_audit_middleware = mcp_manager.get_audit_middleware()
                # 注入 tool_metadata 到 context_governance.params，供外化层按工具调阈值
                tool_metadata = mcp_manager.registry.get_all_metadata()
                if tool_metadata:
                    cg_params = dict(config.context_governance.params)
                    cg_params["tool_metadata"] = tool_metadata
                    config = replace(
                        config,
                        context_governance=replace(config.context_governance, params=cg_params),
                    )
                thread_journal.append("mcp.loaded", {
                    "tools": list(tools.keys()),
                    "count": len(tools),
                })
            else:
                thread_journal.append("mcp.skipped", {"reason": "disabled or no servers"})
        except Exception as exc:
            thread_journal.append("mcp.load_failed", {"error": str(exc)})
    else:
        thread_journal.append("mcp.skipped", {"reason": "npx not found"})
        print(
            "Node.js / npx not found; MCP search disabled.",
            file=sys.stderr,
        )

    # ── Sandbox 装配（config 配了 provider 就加载，不论模式）──
    store_token = _SANDBOX_THREAD_STORE.set(thread_store)
    try:
        sandbox_provider = _load_sandbox_provider(config)
    finally:
        _SANDBOX_THREAD_STORE.reset(store_token)
    sandbox_tools = []
    artifact_server = None
    if sandbox_provider is not None:
        from poirot.backend.agents.artifacts.server import ArtifactServer
        from poirot.backend.agents.sandbox.integration.tools import make_sandbox_tools
        from poirot.backend.agents.sandbox.integration.bootstrap_sandbox import (
            register_sandbox_shutdown,
        )

        sandbox_tools = make_sandbox_tools(
            sandbox_provider,
            allow_host_bash=getattr(config.sandbox, "allow_host_bash", True),
        )
        register_sandbox_shutdown(sandbox_provider)
        artifact_server = ArtifactServer()
        artifact_server.start()

    # ── Skill 模块加载：build_skill_manager 读 .env，enabled=false 或无目录返 None ──
    skill_manager = None
    skill_injection_middleware = None
    skill_metrics_middleware = None
    try:
        from poirot.backend.agents.skill import build_skill_manager

        skill_manager = build_skill_manager()
        if skill_manager is not None:
            skill_manager.load_startup(llm=researcher_model)
            skill_injection_middleware = skill_manager.get_injection_middleware()
            skill_metrics_middleware = skill_manager.get_metrics_middleware()
            thread_journal.append("skill.loaded", {
                "skills": [s["name"] for s in skill_manager.list_skills()],
            })
            # 自进化装配（Layer 2a）：evolve_enabled=true 时建 EvolutionManager 注入
            if skill_manager.config.evolve_enabled:
                try:
                    skill_manager.set_evolution_manager(
                        _build_evolution_manager(skill_manager, researcher_model, thread_journal)
                    )
                    thread_journal.append("skill.evolve_loaded", {})
                except Exception as exc:
                    thread_journal.append("skill.evolve_load_failed", {"error": str(exc)})
                # L3 eval 装配：eval_config.enabled=true 时建 EvalLayer 注入
                if skill_manager.config.eval_config.enabled:
                    try:
                        skill_manager.set_eval_layer(
                            _build_eval_layer(skill_manager, researcher_model)
                        )
                        thread_journal.append("skill.eval_loaded", {})
                    except Exception as exc:
                        thread_journal.append("skill.eval_load_failed", {"error": str(exc)})
        else:
            thread_journal.append("skill.skipped", {"reason": "disabled or no skills dir"})
    except Exception as exc:
        thread_journal.append("skill.load_failed", {"error": str(exc)})

    all_tools = {**tools, **{t.name: t for t in sandbox_tools}}

    # ── Multi-Agent orchestration 装配：enabled=false 时返空 setup（lead agent 行为不变）──
    ma_config = load_multiagent_config()

    def _subagent_factory() -> Any:
        """Leaf-role lead agent factory，供 self-copy subagent 使用。

        复用 lead agent 构造逻辑，但 leaf role 限制：
        - specialist_tools=None（leaf 看不到 delegate_to_*，不能再 spawn）
        - orchestration_middleware=None（leaf 不挂 OrchestrationMiddleware）

        这些限制让子 agent 无法递归 spawn（leaf-only MVP）。
        """
        return make_lead_agent(
            expert_mode=expert_mode,
            capability_registry=registry,
            context_governance=config.context_governance,
            sandbox_provider=sandbox_provider,
            artifact_server=artifact_server,
            mcp_audit_middleware=mcp_audit_middleware,
            skill_injection_middleware=skill_injection_middleware,
            skill_metrics_middleware=skill_metrics_middleware,
            specialist_tools=None,              # leaf 不能再 delegate
            orchestration_middleware=None,     # leaf 不挂 OrchestrationMiddleware
            memory_provider=memory_provider,
            memory_config=config.memory,
            memory_worker=get_memory_worker(),
            checkpointer=checkpointer,
        )

    ma_setup = setup_multiagent(
        ma_config,
        agent_factory=_subagent_factory,
    )

    # ── Memory 装配：同步 AppConfig.memory → 全局单例，启动 worker ──
    set_memory_config(config.memory)
    memory_provider = _load_memory_provider(config)
    memory_worker = None
    if memory_provider is not None:
        memory_worker = start_memory_worker(memory_provider.manager(), researcher_model)
        import atexit
        atexit.register(shutdown_memory_worker)

    # ── CapabilityRegistry 构造：聚合所有能力 ──
    registry = CapabilityRegistry(
        models={"researcher": researcher_model, "reporter": reporter_model},
        tools=all_tools,
        reporter=MarkdownReporter(),
        artifact_store=LocalArtifactStore(),
        sandbox_provider=sandbox_provider,
        skill_store=skill_manager.store if skill_manager else None,
        specialist_registry=ma_setup.specialist_registry,
        subagent_provider=ma_setup.subagent_provider,
        memory_provider=memory_provider,
    )

    # ── LeaderAgent 构造：注入工具 + 中间件 ──
    try:
        checkpointer = SessionCheckpointer(thread_store)
        if existing_thread is not None:
            checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    except BaseException:
        if "checkpointer" in locals():
            checkpointer.close()
        if not thread_persisted:
            thread_store.delete(thread_id)
        raise
    try:
        leader_agent = make_lead_agent(
            expert_mode=expert_mode,
            capability_registry=registry,
            context_governance=config.context_governance,
            sandbox_provider=sandbox_provider,
            artifact_server=artifact_server,
            mcp_audit_middleware=mcp_audit_middleware,
            skill_injection_middleware=skill_injection_middleware,
            skill_metrics_middleware=skill_metrics_middleware,
            specialist_tools=list(ma_setup.specialist_tools) if ma_setup.specialist_tools else None,
            orchestration_middleware=ma_setup.orchestration_middleware,
            memory_provider=memory_provider,
            memory_config=config.memory,
            memory_worker=memory_worker,
            checkpointer=checkpointer,
        )
    except BaseException:
        checkpointer.close()
        if not thread_persisted:
            thread_store.delete(thread_id)
        raise
    thread_journal.append("agent.constructed", {
        "expert_mode": expert_mode,
        "middleware_count": 6,
        "tools_count": len(tools),
    })

    # ── 打包 AppRuntime ──
    return AppRuntime(
        config=config,
        capability_registry=registry,
        run_manager=RunManager(config),
        researcher_model_name=researcher_model_name,
        thread_id=thread_id,
        thread_dir=thread_dir,
        thread_journal=thread_journal,
        leader_agent=leader_agent,
        mcp_manager=mcp_manager,
        artifact_server=artifact_server,
        skill_manager=skill_manager,
        multiagent_setup=ma_setup,
        thread_store=thread_store,
        checkpointer=checkpointer,
        project=project,
        project_store=project_store,
        thread_persisted=thread_persisted,
    )
