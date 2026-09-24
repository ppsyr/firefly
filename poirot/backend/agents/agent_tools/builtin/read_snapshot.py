"""read_snapshot 工具 —— 供 LLM 调用，读取「压缩前快照」内容。

【整体职责】
当上下文被压缩（summarize）后，部分细节（工具调用记录、核心思路等）会丢失。
本工具让 LLM 能按路径读回压缩前的快照文件，找回这些丢失的重要上下文。

【组成】
1. 唯一对外工具：read_snapshot(snapshot_path)
   - 按给定路径读取文件内容，原样返回文本。
   - 读取失败时返回一句中文错误提示，不抛异常。

【快照路径来源】
由调用方从 <summary> 标签或上下文提示中获取，作为入参传入。

【职责边界】
- 只做「按路径读文件 + 返回文本」，不做路径校验、不做目录列举、不做写入。
- 读文件异常（OSError）被捕获，转为可读的错误字符串返回，不向上抛。
- 注意：@tool 未指定工具名，工具名默认取函数名 read_snapshot。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def read_snapshot(snapshot_path: str) -> str:
    """读取指定路径的压缩前快照，找回丢失的重要上下文（工具调用/核心思路）。

    行为：
        - 以 UTF-8 编码打开并读取 snapshot_path 指向的文件，返回其完整内容。
        - 若读取过程中发生 OSError（文件不存在、无权限、是目录等），
          返回形如「读取快照失败：<路径>」的字符串，不抛异常。

    参数：
        snapshot_path: 快照文件路径（从 <summary> 标签或上下文提示获取）。

    返回：
        成功 —— 快照文件的完整文本内容；
        失败 —— "读取快照失败：{snapshot_path}"。
    """
    try:
        with open(snapshot_path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return f"读取快照失败：{snapshot_path}"