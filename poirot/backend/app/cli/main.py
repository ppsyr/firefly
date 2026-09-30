"""CLI 入口 — 命令行参数解析、启动配置与主循环。

【整体职责】
Poirot 的进程入口：加载 .env、解析命令行（chat / cli / run 三种模式）、引导首次配置，
启动后进入交互主循环（TUI 或传统 CLI）。主循环负责用户输入 → 命令处理 → 意图识别 →
流式研究 → 报告触发，并维护 cli_state（模式 / 模型 / token 展示）。

【内容摘要】
- main                 : 入口，解析参数并分发到 run / cli / 默认 chat 三种模式。
- run_chat             : 启动 bootstrap，进入 TUI 或传统 CLI。
- _build_stream_config : 构造 graph config（供 stream 使用）。
- _run_chat_async      : 传统 CLI 主循环（prompt_toolkit + rich）。
- run_chat 内嵌：_print_status / _print_welcome / _handle_report_intent / _trigger_report。

【职责边界】
- 只负责：参数解析、启动编排、交互主循环、呈现（banner / welcome / Markdown 渲染）。
- 不负责：运行时装配（bootstrap）、Agent 执行与图逻辑（leader/agent）、
  报告合成逻辑（agents/reporting）、命令实现细节（commands）。

【INVARIANT】
- .env 显式从项目根加载：load_dotenv(_PROJECT_ROOT / ".env")，避免从非项目根启动时
  POIROT_SKILL_* 等配置缺失导致 skill 模块被误跳过。
- 首次启动引导：.env 不存在时先跑 ensure_config，失败返回 1。
- bootstrap 在 asyncio.run 之前（同步阶段）：避免 MCP 的 asyncio.run 嵌套。
- 三种模式：
  - 无子命令 → 默认 TUI（textual）；
  - cli       → 传统 CLI（prompt_toolkit + rich）；
  - run       → 单次提问，直接输出报告与 run_id / events / artifact。
- run 模式默认 expert：--expert 默认 True，--no-expert 关闭。
- 意图先于 graph：intent_tree.detect_and_dispatch 命中则不进 graph。
- run 生命周期：create_run → mark_running → stream → mark_success / mark_failed。
- 模式/模型热切换复用 thread_id + checkpointer state。
- Ctrl+C / Ctrl+D 在输入时退出进程；/exit、/quit 退出。
"""
from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

# 显式从项目根加载 .env——load_dotenv() 默认只查 CWD，从非项目根启动时
# POIROT_SKILL_* 等配置不进 env（CWD-relative），导致 skill 模块被误跳过。
# main.py 位于 poirot/backend/app/cli/，parents[4] 即项目根。
_PROJECT_ROOT = Path(__file__).parents[4]
load_dotenv(_PROJECT_ROOT / ".env")

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from poirot.backend.app.bootstrap import bootstrap_runtime, AppRuntime
from poirot.backend.app.cli.banner import render_banner
from poirot.backend.app.cli.command_completer import SlashCommandCompleter
from poirot.backend.app.cli.commands import get_registry, handle_command
from poirot.backend.app.cli.status_bar import build_bottom_toolbar
from poirot.backend.app.cli.stream_handler import StreamRenderer
from poirot.backend.app.services.stream_service import PoirotStreamClient
from poirot.backend.agents.intent import default_intent_tree
from poirot.backend.agents.leader.agent import _resolve_actual_model_name
from poirot.backend.agents.prompts import get_prompt_manager
from poirot.backend.agents.runtime.file_access import ThreadFileAccess
from poirot.backend.agents.runtime.thread_quote import thread_candidates


def main(argv: Sequence[str] | None = None) -> int:
    """入口：解析参数并分发到各模式。

    Args:
        argv: 命令行参数；None 时取 sys.argv。

    Returns:
        int: 退出码（0 成功，1 失败）。
    """
    # 首次启动配置向导：.env 不存在时引导用户配置
    from poirot.backend.app.cli.setup_wizard import ensure_config
    if not ensure_config(_PROJECT_ROOT):
        return 1
    load_dotenv(_PROJECT_ROOT / ".env", override=True)

    parser = argparse.ArgumentParser(prog="poirot")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dir", dest="project_dir", default=None)
    parser.add_argument("--project_name", dest="project_name", default=None)
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("question")
    run_parser.add_argument("--expert", action="store_true", default=True, help="enable expert mode (deep research, default for run)")
    run_parser.add_argument("--no-expert", action="store_false", dest="expert", help="disable expert mode (lightweight)")
    run_parser.add_argument("--thread-id", default="default-thread")
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--logs-root", default=None)
    run_parser.add_argument("--no-artifact", action="store_true")
    run_parser.add_argument("--dir", dest="project_dir", default=argparse.SUPPRESS)
    run_parser.add_argument("--project_name", dest="project_name", default=argparse.SUPPRESS)

    cli_parser = subparsers.add_parser("cli", help="traditional scrolling CLI (prompt_toolkit + rich)")
    cli_parser.add_argument("--dir", dest="project_dir", default=argparse.SUPPRESS)
    cli_parser.add_argument("--project_name", dest="project_name", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if args.command is None:
        return run_chat(provider=args.provider, model=args.model, legacy=False, project_dir=args.project_dir, project_name=args.project_name)

    if args.command == "cli":
        return run_chat(provider=args.provider, model=args.model, legacy=True, project_dir=args.project_dir, project_name=args.project_name)

    if args.command == "run":
        overrides: dict = {}
        if args.logs_root:
            overrides["logs_root"] = args.logs_root
        if args.no_artifact:
            overrides["save_artifact"] = False
        runtime = bootstrap_runtime(
            expert_mode=args.expert,
            provider=args.provider,
            model=args.model,
            cli_overrides=overrides,
            thread_id=args.thread_id,
            project_dir=args.project_dir,
            project_name=args.project_name,
        )
        try:
            try:
                result = runtime.run_question(
                    question=args.question,
                    thread_id=args.thread_id,
                    run_id=args.run_id,
                )
            except Exception as exc:
                from poirot.backend.agents.runtime.file_access import FileAccessError
                if isinstance(exc, FileAccessError):
                    print(f"File reference failed: {exc}", file=sys.stderr)
                    return 1
                raise
        finally:
            runtime.close()
        print(result.final_report)
        print(f"run_id: {result.run_id}")
        print(f"events_jsonl: {result.events_path}")
        if result.artifact_path:
            print(f"final_report_md: {result.artifact_path}")
        return 0

    return 1


def run_chat(provider: str | None = None, model: str | None = None, legacy: bool = False,
             project_dir: str | None = None, project_name: str | None = None) -> int:
    """启动 bootstrap 并进入 TUI 或传统 CLI。

    Args:
        provider: 指定 provider。
        model: 指定模型。
        legacy: True 走传统 CLI，False 走 TUI。

    Returns:
        int: 退出码。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    # bootstrap 在 asyncio.run 之前（同步阶段），避免 MCP 的 asyncio.run 嵌套
    runtime = bootstrap_runtime(provider=provider, model=model, project_dir=project_dir, project_name=project_name)

    try:
        if not legacy:
            from poirot.backend.app.tui import PoirotTUI
            app = PoirotTUI(runtime=runtime, provider=provider, model=model)
            app.run()
            return 0
        return asyncio.run(_run_chat_async(runtime, provider, model))
    finally:
        runtime.close()


def _build_stream_config(runtime: AppRuntime, run_context: Any) -> dict:
    """构建 graph config（stream 用，与 LeaderAgent.run 一致）。

    Args:
        runtime: 应用运行时。
        run_context: 运行上下文。

    Returns:
        dict: graph 调用配置（configurable + recursion_limit）。
    """
    rc = run_context.config.runtime
    return {
        "configurable": {
            "expert_mode": rc.expert_mode,
            "run_id": run_context.run_id,
            "thread_id": run_context.thread_id,
            "thread_title": runtime.thread_store.require(run_context.thread_id).title if getattr(runtime, "thread_store", None) else None,
            "journal": run_context.journal,
            "output_dir": str(run_context.output_dir),
            "plan_enabled": rc.plan_enabled,
            "timezone": rc.timezone,
            "model": _resolve_actual_model_name(runtime.capability_registry),
        },
        "recursion_limit": rc.max_loop_steps * rc.graph_node_multiplier,
    }

def _resolve_provider_and_model(model_obj: Any) -> tuple[str, str]:
    """从 ChatModel 对象解析 (provider, model_name)。

    - FallbackChatModel：链首 provider + 链首 model 名；
    - 单 ChatModel：provider 回退 "?"，model 从对象取；
    - None → ("?", "?")。

    Returns:
        (provider, model_name)：两个字符串。
    """
    if model_obj is None:
        return ("?", "?")

    # FallbackChatModel：有 models + provider_names
    models_list = getattr(model_obj, "models", []) or []
    provider_names = getattr(model_obj, "provider_names", []) or []

    if models_list:
        first_cm = models_list[0]
        provider = provider_names[0] if provider_names else "?"
        # 取 model 名
        model_name = "?"
        params = getattr(first_cm, "_identifying_params", None)
        if callable(params):
            params = params()
        if isinstance(params, dict):
            model_name = params.get("model") or params.get("model_name") or "?"
        if model_name == "?":
            model_name = (
                getattr(first_cm, "model_name", None)
                or getattr(first_cm, "model", None)
                or "?"
            )
        return (provider, model_name)

    # 单 ChatModel
    model_name = "?"
    params = getattr(model_obj, "_identifying_params", None)
    if callable(params):
        params = params()
    if isinstance(params, dict):
        model_name = params.get("model") or params.get("model_name") or "?"
    if model_name == "?":
        model_name = (
            getattr(model_obj, "model_name", None)
            or getattr(model_obj, "model", None)
            or "?"
        )
    return ("?", model_name)

async def _run_chat_async(runtime: AppRuntime, provider: str | None, model: str | None) -> int:
    """传统 CLI 主循环：prompt_toolkit 输入 + rich 渲染 + 流式研究。

    Args:
        runtime: 应用运行时。
        provider: 指定 provider。
        model: 指定模型。

    Returns:
        int: 退出码。
    """
    console = Console()
    # cli_state：主循环共享状态——mode/model 供 bottom_toolbar 显示，current_tokens/fraction/window
    # 由 renderer 收到 budget_update 事件时回填（见 stream_handler._update_budget）


    _p, _m = _resolve_provider_and_model(runtime.capability_registry.get_model("researcher"))

    cli_state: dict[str, Any] = {
        "pending_expert_mode": None,
        "pending_mcp_reload": None,
        "skill_override": [],
        "mode": "expert" if runtime.config.runtime.expert_mode else "default",
        "model": _m,           # ← 字符串（如 "gpt-4.1-mini"）
        "model_provider": _p,     # ← 字符串（如 "openai"）
        "current_tokens": 0,
        "current_fraction": 0.0,
        "current_window": 0,
        "thread_title": runtime.thread_store.require(runtime.thread_id).title if runtime.thread_store else runtime.thread_id,
    }
    # skill_provider：惰性取当前 runtime 的 active skill 名（闭包读最新 runtime，
    # switch/reload 后 runtime 重绑定，闭包见新值）。供 /skill <name> 补全。
    def _skill_names_provider():
        mgr = getattr(runtime, "skill_manager", None)
        if mgr is None:
            return []
        return [s["name"] for s in mgr.list_skills()]

    file_access_cache: tuple[tuple[str, str | None, tuple[str, ...]], ThreadFileAccess] | None = None

    def _file_candidates(fragment: str):
        """Provide live, thread-scoped ``@`` completion candidates."""
        nonlocal file_access_cache
        try:
            item = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
            key = (runtime.thread_id, item.cwd if item else None, item.extra_dirs if item else ())
            if file_access_cache is None or file_access_cache[0] != key:
                file_access_cache = (key, ThreadFileAccess(item.cwd if item else None, item.extra_dirs if item else ()))
            access = file_access_cache[1]
            candidates = []
            for path in access.suggest_paths(fragment):
                try:
                    label = path.relative_to(access.cwd).as_posix()
                except ValueError:
                    label = path.as_posix()
                candidates.append((path, label))
            return candidates
        except Exception:
            return []

    def _thread_candidates(fragment: str, quoted: bool):
        try:
            current = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
            if current is None:
                return []
            return [(item.thread_id, item.display, item.insert_text) for item in thread_candidates(
                fragment, current=current, thread_store=runtime.thread_store, quoted_title=quoted
            )]
        except Exception:
            return []

    def _reference_directories():
        try:
            current = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
            return list(current.extra_dirs) if current else []
        except Exception:
            return []

    def _cd_directory_candidates(fragment: str):
        """Complete directories relative to the current thread cwd."""
        try:
            item = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
            base = Path(item.cwd) if item and item.cwd else None
            if base is None:
                return []
            expanded = Path(fragment).expanduser()
            parent = expanded.parent if str(expanded.parent) not in (".", "") else base
            if not expanded.is_absolute():
                parent = (base / parent).resolve(strict=False)
            if not parent.is_dir():
                return []
            prefix = expanded.name if str(expanded.parent) not in (".", "") else ""
            results = []
            for child in sorted(parent.iterdir(), key=lambda value: value.name.lower()):
                if not child.is_dir() or (prefix and not child.name.lower().startswith(prefix.lower())):
                    continue
                value = str(child)
                results.append((shlex.quote(value) + " ", value))
            return results
        except Exception:
            return []

    completion_keys = KeyBindings()

    @completion_keys.add("enter", filter=Condition(
        lambda: bool(getattr(session, "app", None)
                     and session.app.current_buffer.complete_state is not None)
    ), eager=True)
    def _accept_completion(event):
        # Enter accepts the highlighted @/slash candidate and leaves the
        # prompt open.  A second Enter, after the completion menu closes,
        # submits the question.
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is not None:
            completion = state.current_completion
            if completion is None and state.completions:
                completion = state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)

    session: PromptSession = PromptSession(
        completer=ThreadedCompleter(SlashCommandCompleter(
            get_registry(), skill_provider=_skill_names_provider, file_provider=_file_candidates,
            thread_provider=_thread_candidates, directory_provider=_reference_directories,
            cd_directory_provider=_cd_directory_candidates,
        )),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        key_bindings=completion_keys,
        bottom_toolbar=lambda: build_bottom_toolbar(cli_state),
        style=Style([
            ("completion-menu.completion.current", "bg:#6A5ACD fg:#ffffff"),
            ("completion-menu.completion", "bg:#2b2b2b fg:#aaaaaa"),
            ("bottom-toolbar", "bg:#2b2b2b fg:#aaaaaa"),
        ]),
    )
    renderer = StreamRenderer(console=console, cli_state=cli_state)

    def _print_status() -> None:
        mode_label = "expert" if runtime.config.runtime.expert_mode else "default"
        model_name = _resolve_actual_model_name(runtime.capability_registry)
        console.print(f"[dim]Poirot v1.0.0 | mode: {mode_label} | {model_name} | thread: {runtime.thread_id[:20]}[/dim]")

    def _print_welcome() -> None:
        """硬编码开场白（从 prompts/system/cli/welcome.md 加载），不调 LLM。"""
        mode_label = "expert" if runtime.config.runtime.expert_mode else "default"
        model_name = _resolve_actual_model_name(runtime.capability_registry)
        try:
            welcome = get_prompt_manager().load(
                "cli", "welcome",
                mode_label=mode_label, model_name=model_name, thread_id=runtime.thread_id[:20],
            )
            console.print(welcome)
        except Exception:
            # welcome.md 加载失败时 fallback 简短文本
            console.print(f"[bold]你好，我是 Poirot。[/bold] mode: {mode_label}")
            console.print("[dim]/report 生成报告 | /expert 深度研究 | /default 轻量对话 | /help 全部命令[/dim]\n")

    def _handle_report_intent(intent: Any, rt: Any) -> bool:
        """报告意图 handler：触发报告合成。"""
        topic = intent.payload.get("topic") if intent.payload else None
        _trigger_report(topic, rt, console)
        return True

    def _trigger_report(topic: str | None, rt: AppRuntime, con: Console) -> None:
        """Generate a durable user-level report and render its Markdown."""
        try:
            result = rt.generate_report(topic=topic)
        except Exception as exc:
            con.print(f"[red]✗ 报告生成失败: {exc}[/red]\n")
            return
        from rich.markdown import Markdown
        con.print(Markdown(result.final_report))
        con.print(f"[dim]report saved: {result.report_path}[/dim]")
        con.print(f"[dim]conversation saved: {result.conversation_path}[/dim]\n")
        if result.index_error:
            con.print(f"[yellow]report index unavailable: {result.index_error}[/yellow]\n")

    intent_tree = default_intent_tree(report_handler=_handle_report_intent)

    provider_label = provider or "default"
    console.print(render_banner("POIROT"))
    # MCP 工具数量徽标——bootstrap 后统计已加载的 MCP 工具
    try:
        from poirot.backend.agents.agent_tools.available import get_available_tools
        from poirot.backend.agents.agent_tools.mcp_metadata import is_mcp_tool
        tools = get_available_tools(include_mcp=True)
        mcp_count = sum(1 for t in tools if is_mcp_tool(t))
        console.print(f"[green]●[/green] {mcp_count} MCP tools loaded\n")
    except Exception:
        # 工具加载失败不阻塞 CLI 启动
        pass
    _print_welcome()
    console.print()

    # 主循环
    while True:
        try:
            with patch_stdout():
                user_input = await session.prompt_async("> ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D 输入时退出进程
            console.print()
            return 0

        prompt = user_input.strip()
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        if prompt.startswith("/"):
            previous_thread = runtime.thread_id
            should_exit = handle_command(prompt, console, renderer, cli_state, runtime)
            if should_exit:
                return 0

            if cli_state.pop("pending_thread_new", False):
                try:
                    runtime = runtime.new_thread()
                    cli_state["thread_title"] = runtime.thread_store.require(runtime.thread_id).title
                    console.print(f"New thread: {cli_state['thread_title']} [{runtime.thread_id}]", style="green", markup=False)
                except Exception as exc:
                    console.print(f"[red]Thread creation failed: {exc}[/red]")
            pending_cd = cli_state.pop("pending_cd", None)
            if pending_cd is not None:
                try:
                    changed = runtime.cd(pending_cd)
                    runtime = changed
                    cli_state["thread_title"] = runtime.thread_store.require(runtime.thread_id).title
                    console.print(
                        f"Switched to directory: {runtime.project.dir} "
                        f"(project: {runtime.project.project_name}) [{runtime.thread_id}]",
                        style="green", markup=False,
                    )
                except Exception as exc:
                    console.print(f"[red]Directory switch failed: {exc}[/red]")
            if cli_state.pop("pending_project_list", False):
                from poirot.backend.app.cli.thread_selector import select_project
                if runtime.project_store is None:
                    console.print("[red]Project storage is unavailable[/red]")
                    continue
                with patch_stdout():
                    selected_project = await select_project(
                        runtime.project_store.list(), runtime.project.project_name if runtime.project else None, console
                    )
                if selected_project:
                    try:
                        changed = runtime.switch_project(selected_project)
                        if changed.thread_store is None:
                            raise RuntimeError("Thread storage is unavailable")
                        cli_state["thread_title"] = changed.thread_store.require(changed.thread_id).title
                        runtime = changed
                        console.print(f"Switched to project: {selected_project}", style="green")
                    except Exception as exc:
                        console.print(f"[red]Project switch failed: {exc}[/red]")
            if cli_state.pop("pending_project_thread_list", False):
                from poirot.backend.app.cli.thread_selector import select_thread
                if runtime.project is None or runtime.thread_store is None:
                    console.print("[dim]Current thread is not bound to a project[/dim]")
                else:
                    with patch_stdout():
                        selected = await select_thread(runtime.thread_store.list_project(runtime.project.project_name), runtime.thread_id, console)
                    if selected:
                        try:
                            changed = runtime.switch_project_thread(selected)
                            if changed.thread_store is None:
                                raise RuntimeError("Thread storage is unavailable")
                            cli_state["thread_title"] = changed.thread_store.require(changed.thread_id).title
                            runtime = changed
                            console.print(f"Restored: {cli_state['thread_title']} [{selected}]", style="green", markup=False)
                        except Exception as exc:
                            console.print(f"[red]Project thread restore failed: {exc}[/red]")
            if cli_state.pop("pending_thread_list", False):
                from poirot.backend.app.cli.thread_selector import select_thread
                with patch_stdout():
                    selected = await select_thread(runtime.thread_store.list(), runtime.thread_id, console)
                if selected:
                    cli_state["pending_thread_switch"] = selected
            selected = cli_state.pop("pending_thread_switch", None)
            if selected:
                try:
                    runtime = runtime.switch_thread(selected)
                    cli_state["thread_title"] = runtime.thread_store.require(runtime.thread_id).title
                    console.print(f"Restored: {cli_state['thread_title']} [{runtime.thread_id}]", style="green", markup=False)
                except Exception as exc:
                    console.print(f"[red]Thread switch failed: {exc}[/red]")
            elif prompt.startswith("/thread rename "):
                cli_state["thread_title"] = runtime.thread_store.require(runtime.thread_id).title
            if previous_thread != runtime.thread_id:
                renderer = StreamRenderer(console=console, cli_state=cli_state)
                for key, value in {"skill_override": [], "current_tokens": 0, "current_fraction": 0.0, "current_window": 0}.items():
                    cli_state[key] = value
                cli_state.pop("sandbox_id", None)

            # /expert /default 切换：下轮重建 agent（复用 thread_id + checkpointer state）
            pending = cli_state.get("pending_expert_mode")
            if pending is not None:
                runtime = runtime.switch_expert_mode(expert_mode=pending)
                cli_state["pending_expert_mode"] = None
                # 同步 bottom_toolbar 显示的 mode/model
                cli_state["mode"] = "expert" if pending else "default"
                _p, _m = _resolve_provider_and_model(runtime.capability_registry.get_model("researcher"))
                cli_state["model_provider"] = provider or _p     # ← 看这里
                cli_state["model"] = model or _m
                _print_status()
                label = "expert" if pending else "default"
                console.print(f"[green]Switched to {label} mode[/green]\n")

            # /report 命令：触发报告合成
            pending_report = cli_state.get("pending_report")
            if pending_report is not None:
                cli_state["pending_report"] = None
                _trigger_report(pending_report, runtime, console)

            # /mcp reload 命令：重建 leader_agent graph（复用 reload_mcp_tools）
            if cli_state.get("pending_mcp_reload"):
                cli_state["pending_mcp_reload"] = None
                runtime = runtime.reload_mcp_tools()
                console.print("[green]MCP tools reloaded[/green]\n")

            # /model <provider> [model] 命令：热切换 LLM（重建 model+registry+leader_agent，保留 thread）
            pending_model = cli_state.get("pending_model_switch")
            if pending_model is not None:
                cli_state["pending_model_switch"] = None
                provider, model = pending_model
                try:
                    runtime = runtime.switch_model(provider=provider, model=model)
                    _p, _m = _resolve_provider_and_model(runtime.capability_registry.get_model("researcher"))
                    cli_state["model_provider"] = provider or _p     # ← 看这里
                    cli_state["model"] = model or _m
                    _print_status()
                    console.print(f"[green]Switched to {provider}/{model or 'default'}[/green]\n")
                except Exception as exc:
                    console.print(f"[red]Model switch failed: {exc}[/red]\n")
            continue

        # 用户输入卡片化回显（/命令不套卡片——它们是系统操作，不是对话）
        renderer.render_user_input(prompt)

        # 意图识别（graph 之前）：命中则不进 graph
        if intent_tree.detect_and_dispatch(prompt, runtime):
            continue

        # 流式研究
        ctx = None
        try:
            # A freshly opened thread is only materialized when the user
            # submits the first chat message.
            runtime.ensure_thread_persisted()
            async def choose_file(reference, candidates):
                from poirot.backend.app.cli.thread_selector import select_file
                item = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
                return await select_file(candidates, item.cwd if item else ".", console)

            prepared = await runtime.aprepare_question(prompt, choose=choose_file)
            current_thread = runtime.thread_store.require(runtime.thread_id) if runtime.thread_store else None
            ctx = runtime.run_manager.create_run(
                thread_id=runtime.thread_id,
                user_id="default-user",
                run_id=None,
                model_name=runtime.researcher_model_name,
                thread_dir=runtime.thread_dir,
                project=current_thread.project if current_thread else None,
                cwd=current_thread.cwd if current_thread else None,
            )
            runtime.run_manager.mark_running(ctx.run_id)
            runtime.begin_turn(prompt)
            config = _build_stream_config(runtime, ctx)
            # /skill override：cli_state → configurable，SkillInjectionMiddleware 读取
            config["configurable"]["skill_override"] = cli_state.get("skill_override") or []
            client = PoirotStreamClient(graph=runtime.leader_agent.graph, config=config)

            # 注入 round 起始时间 + 模型名，供 _render_done 输出耗时尾行
            import time as _time
            renderer.state["round_t0"] = _time.monotonic()
            _p, _m = _resolve_provider_and_model(runtime.capability_registry.get_model("researcher"))
            renderer.state["model"] = provider or _p     # ← 看这里
            renderer.state["model_provider"] = model or _m

            async for event in client.stream(prepared.enriched, title_question=prepared.original):
                renderer.render(event)

            runtime.run_manager.mark_success(ctx.run_id)
            console.print(f"\n[dim]run_id: {ctx.run_id} | events: {ctx.events_path}[/dim]\n")
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[yellow]⚠ Interrupted[/yellow]\n")
            if ctx is not None:
                runtime.run_manager.mark_failed(ctx.run_id, "interrupted")
            continue
        except Exception as exc:
            renderer._stop_spinner()
            console.print(f"\n[red]✗ Error: {exc}[/red]\n")
            if ctx is not None:
                runtime.run_manager.mark_failed(ctx.run_id, str(exc))
            continue
        finally:
            try:
                runtime.end_turn()
                cli_state["thread_title"] = runtime.thread_store.require(runtime.thread_id).title
            except Exception as exc:
                console.print(f"[red]Thread metadata update failed: {exc}[/red]")


if __name__ == "__main__":
    raise SystemExit(main())
