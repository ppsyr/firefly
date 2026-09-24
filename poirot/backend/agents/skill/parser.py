"""SKILL.md YAML frontmatter 解析 + .skill_id sidecar + install。

【整体职责】
把技能目录里的 SKILL.md 解析为 SkillRecord。
负责：
- 解析 SKILL.md 的 YAML frontmatter（name / description / allowed-tools / enabled 等）。
- 通过 .skill_id sidecar 维护持久 skill_id（目录改名 id 不变）。
- 计算内容哈希（sha256 前 16 位）。
- install：把外部技能目录拷贝进目标根目录并解析注册。

上游调用者：SQLiteSkillStore.discover（扫描目录时逐个解析）。
本模块是 store 的输入源，产出 SkillRecord 供 register / _upsert_record 写入。

【内容摘要】
- 模块常量
    - _SKILL_ID_FILE  : ".skill_id" sidecar 文件名。
    - _FRONTMATTER_RE : frontmatter 正则（匹配 `---\\n{yaml}\\n---\\n{body}`）。
- _generate_skill_id(name, origin, generation) : 按 origin 生成 skill_id。
- read_or_create_skill_id(skill_dir, name, origin, generation) : 读/写 sidecar。
- parse_skill_file(skill_file, origin) : SKILL.md → SkillRecord（模块主函数）。
- install(source_dir, name, dest_root) : 拷贝技能目录并解析，返回 skill_id。

【职责边界】
- 只负责：文件读取、frontmatter 解析、字段校验、id 生成/持久化、内容哈希、目录拷贝。
- 不负责：持久化入库（store）、选择（selector）、注入（injector）、打点（middleware）。
- 不写 DB：parse_skill_file 只返回 SkillRecord，落库由 store.register/_upsert_record 完成。
- 不做版本演进：create_version / rollback 属 store；本模块只产出初始记录。

【INVARIANT】
- .skill_id sidecar 持久 id，仅 IMPORTED / EVOLVED 使用：
    首次生成写文件，已存在则读（目录改名 id 不变）。
- id 规则：
    BUILTIN  → {name}__builtin（确定性，无 sidecar，核心技能随包 id 可复现）
    IMPORTED → {name}__imp_{uuid8}
    EVOLVED  → {name}__v{generation}_{uuid8}
- frontmatter 必需 name + description，缺则 ValueError。
- allowed-tools YAML list → tuple；缺省 ()。
- enabled 缺省 True。
- content_hash = sha256(SKILL.md 全文)[:16]。
- SkillRecord.lineage.origin 由 parse_skill_file 的 origin 参数决定
  （IMPORTED / BUILTIN；版本演进走 store.create_version，不在本模块）。
- install 的 name 仅允许 [a-z0-9-]+，拒绝路径逃逸（`../` 或绝对路径）。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path

import yaml

from poirot.backend.agents.skill.types import SkillLineage, SkillRecord

_SKILL_ID_FILE = ".skill_id"
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)", re.DOTALL)


def _generate_skill_id(name: str, origin: str, generation: int) -> str:
    """按 origin 生成 skill_id（不读写 sidecar，纯计算）。

    规则：
    - BUILTIN  → {name}__builtin（确定性，可复现）。
    - IMPORTED → {name}__imp_{uuid8}。
    - EVOLVED  → {name}__v{generation}_{uuid8}。

    Args:
        name:       技能名。
        origin:     IMPORTED / BUILTIN / EVOLVED。
        generation: 代数（仅 EVOLVED 用于 id）。

    Returns:
        生成的 skill_id 字符串。
    """
    if origin == "BUILTIN":
        return f"{name}__builtin"
    short = uuid.uuid4().hex[:8]
    if origin == "IMPORTED":
        return f"{name}__imp_{short}"
    return f"{name}__v{generation}_{short}"


def read_or_create_skill_id(
    skill_dir: Path, name: str, origin: str = "IMPORTED", generation: int = 0
) -> str:
    """读取或创建 .skill_id sidecar，返回持久 skill_id。

    BUILTIN origin：直接返回确定性 id `{name}__builtin`，不读不写 sidecar
    （核心技能随包提交，id 可复现，无需持久化）。
    IMPORTED / EVOLVED：sidecar 持久化到 skill_dir/.skill_id；
    已存在则读取（目录改名后 id 仍不变），否则生成并写入。

    Args:
        skill_dir:  技能目录（sidecar 所在目录）。
        name:       技能名。
        origin:     IMPORTED / BUILTIN / EVOLVED。
        generation: 代数（仅 EVOLVED 使用）。

    Returns:
        持久 skill_id。
    """
    if origin == "BUILTIN":
        return _generate_skill_id(name, origin, generation)
    sidecar = skill_dir / _SKILL_ID_FILE
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    skill_id = _generate_skill_id(name, origin, generation)
    sidecar.write_text(skill_id, encoding="utf-8")
    return skill_id


def parse_skill_file(skill_file: Path, origin: str = "IMPORTED") -> SkillRecord:
    """解析 SKILL.md → SkillRecord（模块主函数）。

    文件格式：
        ---
        {YAML frontmatter}
        ---
        {body}   （body 当前不解析，仅用于定位）

    frontmatter 字段：
    - 必需：name、description（缺失抛 ValueError）。
    - 可选：allowed-tools（YAML list → tuple，缺省 ()）、
            enabled（缺省 True）、related-skills 等（当前未消费）。

    origin 决定 lineage.origin 与 skill_id 生成方式：
    - IMPORTED：用户技能，走 sidecar 持久 id。
    - BUILTIN ：核心技能，确定性 id，无 sidecar。

    Args:
        skill_file: SKILL.md 路径。
        origin:     IMPORTED / BUILTIN。

    Returns:
        SkillRecord（含 skill_id / content_hash / description /
        allowed_tools / enabled / lineage）。

    Raises:
        ValueError: 缺 frontmatter、YAML 解析失败、frontmatter 非 mapping、
                    缺 name 或 description。
    """
    content = skill_file.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError(
            f"SKILL.md {skill_file} missing YAML frontmatter (expected '---\\n...\\n---\\n')"
        )
    fm_raw, _body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md {skill_file} frontmatter YAML parse error: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError(f"SKILL.md {skill_file} frontmatter must be a mapping, got {type(fm).__name__}")

    name = fm.get("name")
    description = fm.get("description")
    if not name:
        raise ValueError(f"SKILL.md {skill_file} frontmatter missing required field 'name'")
    if not description:
        raise ValueError(f"SKILL.md {skill_file} frontmatter missing required field 'description'")

    allowed_tools_raw = fm.get("allowed-tools") or []
    allowed_tools = tuple(allowed_tools_raw) if allowed_tools_raw else ()
    enabled = bool(fm.get("enabled", True))

    skill_dir = skill_file.parent
    skill_id = read_or_create_skill_id(skill_dir, name, origin=origin)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    return SkillRecord(
        skill_id=skill_id,
        name=name,
        path=str(skill_file),
        content_hash=content_hash,
        description=description,
        allowed_tools=allowed_tools,
        enabled=enabled,
        lineage=SkillLineage(origin=origin),
    )


def install(source_dir: Path, name: str, dest_root: Path) -> str:
    """把 source_dir 拷贝到 dest_root/{name}/，解析 SKILL.md，返回 skill_id。

    行为：
    - 校验 name 仅含 [a-z0-9-]，拒绝 `../` 或绝对路径逃逸。
    - dest_dir 已存在则先整体删除（覆盖安装）。
    - 拷贝目录，确认 SKILL.md 存在后解析。
    - 只负责拷贝 + 解析，不写 DB（落库由调用方 / store 完成）。

    Args:
        source_dir: 源技能目录。
        name:       目标技能名（同时作为子目录名）。
        dest_root:  目标根目录。

    Returns:
        解析得到的 skill_id。

    Raises:
        ValueError:          name 不合法。
        FileNotFoundError:   拷贝后目标目录缺 SKILL.md。
    """
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"invalid skill name: {name!r}")
    dest_dir = dest_root / name
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(source_dir, dest_dir)
    skill_file = dest_dir / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"installed skill dir {dest_dir} has no SKILL.md")
    record = parse_skill_file(skill_file)
    return record.skill_id