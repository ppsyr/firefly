"""首次启动配置向导 — 检测 .env 不存在时引导用户完成最小配置。

【整体职责】
在首次启动（.env 不存在）时，以交互式向导引导用户配置最小可运行项：选择 provider、
输入 API Key、选择可选功能（skill / MCP / sandbox），并从模板生成 .env 文件。
若检测到环境变量中已有任意 provider 的 API Key（Docker env_file 模式），跳过向导。

【内容摘要】
- _console                    : rich Console 单例。
- ensure_config               : 入口，检查 .env / env 注入，必要时启动向导。
- _has_any_provider_key       : 检查环境变量中是否已有 provider API Key。
- _run_wizard                 : 交互式向导主流程。
- _ask_yes_no                 : 是/否提问，回车取默认值。
- _ask_sandbox                : 沙箱模式选择，返回 provider 路径或空串。
- _build_env_content          : 从模板填充用户输入，生成 .env 内容。
- _MINIMAL_TEMPLATE           : .env.example 缺失时的最小模板。
- update_env_file             : 更新 .env 中指定 key（供 TUI 配置面板调用）。

【职责边界】
- 只负责：检测配置是否就绪、交互式采集配置、生成/更新 .env 文件。
- 不负责：配置的加载与校验（loader）、provider 的实现与模型构造（provider_config）、
  TUI 配置面板的呈现（tui 模块，仅调用 update_env_file）。

【INVARIANT】
- 接口小：对外仅 ensure_config(project_root) -> bool 与 update_env_file。
- 不依赖 .env：向导运行前 .env 不存在，不读 env 做判断（除 _has_any_provider_key 检查已注入的 env）。
- 无新依赖：仅用 rich + 内置 input()。
- 模板驱动：生成的 .env 从 .env.example 填充，字段完整；缺失时回退 _MINIMAL_TEMPLATE。
- Docker 兼容：env 已注入 provider key 时跳过向导。
- 至少一个 provider：未配置任何 provider 时不允许退出循环。
- 逐行替换：只替换 KEY= 行，注释与其他行原样保留。
- 取消可退：Ctrl+C / EOFError 时返回 False，不写文件。
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_console = Console()


def ensure_config(project_root: Path) -> bool:
    """检查 .env 是否存在，不存在则启动交互式配置向导。

    Docker 模式下 env_file 注入环境变量但容器内无 .env 文件——
    若已检测到任意 provider API key 在环境变量中，跳过向导。

    Args:
        project_root: 项目根目录（.env 所在位置）。

    Returns:
        bool: True = 配置已就绪（文件存在 / env 已注入 / 刚创建），False = 用户取消。
    """
    env_path = project_root / ".env"
    if env_path.exists():
        return True
    # Docker env_file 模式：env 变量已注入但 .env 文件不在容器内
    if _has_any_provider_key():
        return True
    return _run_wizard(project_root, env_path)


def _has_any_provider_key() -> bool:
    """检查环境变量中是否已有任意 provider 的 API key。

    Returns:
        bool: 任一非 fake provider 的 env_key 非空则 True。
    """
    import os
    from poirot.backend.agents.config.provider_profile import PROVIDER_PROFILES

    for profile in PROVIDER_PROFILES:
        if profile.name == "fake":
            continue
        if os.environ.get(profile.env_key, "").strip():
            return True
    return False


def _run_wizard(project_root: Path, env_path: Path) -> bool:
    """交互式配置向导主流程。

    Args:
        project_root: 项目根目录。
        env_path: 目标 .env 路径。

    Returns:
        bool: True = 生成成功；False = 用户取消。
    """
    # 延迟导入，避免在已有 .env 时加载 provider_profile
    from poirot.backend.agents.config.provider_profile import PROVIDER_PROFILES

    _console.print(Panel(
        Text("Welcome to Poirot\n", justify="center", style="bold cyan")
        + Text("首次使用需要配置 LLM API Key。本向导将帮你生成 .env 配置文件。\n", justify="center", style="dim")
        + Text("按 Ctrl+C 可随时取消。", justify="center", style="dim"),
        border_style="cyan",
    ))

    # 可选 provider（排除 fake）
    selectable = [p for p in PROVIDER_PROFILES if p.name != "fake"]

    configs: dict[str, dict[str, str]] = {}  # provider_name → {api_key, base_url, model}

    try:
        while True:
            _console.print("\n[bold]可用 LLM Provider:[/bold]")
            for i, p in enumerate(selectable, 1):
                marker = "[green]✓[/green]" if p.name in configs else " "
                default_tag = " [dim](默认)[/dim]" if p.is_default else ""
                _console.print(f"  {marker} [{i}] [cyan]{p.name}[/cyan]{default_tag} — {p.default_model}")

            choice = input("\n选择 provider 编号（回车跳过）: ").strip()
            if not choice:
                if not configs:
                    _console.print("[yellow]至少配置一个 provider。[/yellow]")
                    continue
                break
            try:
                idx = int(choice) - 1
                profile = selectable[idx]
            except (ValueError, IndexError):
                _console.print("[red]无效选择。[/red]")
                continue

            # 输入 API key
            key = input(f"输入 {profile.name} API Key: ").strip()
            if not key and not profile.no_key_required:
                _console.print("[yellow]API Key 为空，跳过此 provider。[/yellow]")
                continue

            configs[profile.name] = {
                "api_key": key,
                "base_url": "",  # 空则用 .env.example 默认
                "model": "",     # 空则用 .env.example 默认
            }
            _console.print(f"[green]✓ {profile.name} 已配置[/green]")

            more = input("\n继续添加其他 provider？(y/N): ").strip().lower()
            if more != "y":
                break

        # 可选功能
        _console.print("\n[bold]可选功能（回车=跳过）:[/bold]")
        skill_enabled = _ask_yes_no("启用 Skill 系统？", default=False)
        mcp_enabled = _ask_yes_no("启用 MCP 工具？", default=False)
        sandbox = _ask_sandbox()

        # 生成 .env
        content = _build_env_content(project_root, configs, skill_enabled, mcp_enabled, sandbox)
        env_path.write_text(content, encoding="utf-8")

        _console.print(f"\n[green]✓ 配置文件已生成: {env_path}[/green]")
        _console.print("[dim]后续可通过编辑 .env 修改配置，或删除 .env 重新运行向导。[/dim]\n")
        return True

    except (KeyboardInterrupt, EOFError):
        _console.print("\n[yellow]配置已取消。[/yellow]")
        return False


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    """是/否提问，回车取默认值。

    Args:
        prompt: 提问文本。
        default: 回车时的默认值。

    Returns:
        bool: 用户选择。
    """
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _ask_sandbox() -> str:
    """沙箱配置选择。返回 provider 路径或空串。

    Returns:
        str: Local / Docker provider 路径；不启用则空串。
    """
    _console.print("  [1] Local — 本机进程（开发调试）")
    _console.print("  [2] Docker — 容器隔离（需 Docker）")
    _console.print("  [回车] 不启用")
    choice = input("选择沙箱模式: ").strip()
    if choice == "1":
        return "poirot.backend.agents.sandbox.local.local_sandbox_provider:LocalSandboxProvider"
    if choice == "2":
        return "poirot.backend.agents.sandbox.docker.docker_sandbox_provider:DockerSandboxProvider"
    return ""


def _build_env_content(
    project_root: Path,
    configs: dict[str, dict[str, str]],
    skill: bool,
    mcp: bool,
    sandbox: str,
) -> str:
    """从模板 .env.example 读取，填充用户输入的值，生成 .env 内容。

    Args:
        project_root: 项目根目录。
        configs: provider → {api_key, base_url, model} 配置。
        skill: 是否启用 Skill 系统。
        mcp: 是否启用 MCP 工具。
        sandbox: 沙箱 provider 路径；空串表示不启用。

    Returns:
        str: 生成的 .env 内容。
    """
    template_path = project_root / ".env.example"
    if template_path.exists():
        lines = template_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = _MINIMAL_TEMPLATE.splitlines()

    # 构建 key→value 覆盖映射（大写 env 变量名）
    overrides: dict[str, str] = {}
    for provider_name, cfg in configs.items():
        prefix = provider_name.upper()
        if cfg["api_key"]:
            overrides[f"{prefix}_API_KEY"] = cfg["api_key"]
        # base_url 和 model 留空则保留模板默认值

    # 功能开关
    overrides["POIROT_SKILL_ENABLED"] = "true" if skill else "false"
    overrides["POIROT_MCP_ENABLED"] = "true" if mcp else "false"
    if sandbox:
        overrides["POIROT_SANDBOX_USE"] = sandbox

    # 逐行替换：找到 KEY= 行，用 override 值替换
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in overrides:
                result.append(f"{key}={overrides[key]}")
                continue
        result.append(line)
    return "\n".join(result) + "\n"


# 最小模板（.env.example 不存在时的 fallback）
_MINIMAL_TEMPLATE = """\
# Poirot .env
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
POIROT_SKILL_ENABLED=false
POIROT_MCP_ENABLED=false
POIROT_SANDBOX_USE=
"""


def update_env_file(env_path: Path, overrides: dict[str, str]) -> None:
    """更新 .env 文件中指定 key 的值，保留其他行原样。

    供 TUI 配置面板（Ctrl+B）调用：读取当前 .env → 替换 KEY= 行 → 写回。

    Args:
        env_path: .env 路径。
        overrides: key → value 覆盖映射。
    """
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in overrides:
                result.append(f"{key}={overrides[key]}")
                continue
        result.append(line)
    env_path.write_text("\n".join(result) + "\n", encoding="utf-8")