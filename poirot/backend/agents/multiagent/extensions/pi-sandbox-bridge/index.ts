/**
 * pi-sandbox-bridge — Pi extension：转发 8 个工具到 Poirot SpecialistMcpServer。
 *
 * 【整体职责】
 * 作为 pi CLI 的 extension，注册 8 个 poirot_* 工具，全部通过 stdio MCP 协议转发到
 * Poirot SpecialistMcpServer。配合 `pi --no-builtin-tools` 使用，禁用 pi 自带工具，
 * 强制所有文件操作走 Poirot 沙箱（经 PathTranslator + SecurityGuard）。
 *
 * 【内容摘要】
 * - POIROT_TOOLS          : 8 个工具的 name + description + JSON Schema（bash / read_file /
 *                           write_file / list_dir / str_replace / glob / grep / download_file）。
 * - default export        : extension 入口，遍历 POIROT_TOOLS，逐个 pi.registerTool。
 * - callPoirotMcp()       : 通过 stdio MCP 协议调 Poirot SpecialistMcpServer。
 *
 * 【职责边界】
 * - 只负责：注册工具、转发调用、解析 MCP 响应。
 * - 不负责：沙箱路径翻译（Poirot SpecialistMcpServer 负责）、安全校验（同上）、
 *   pi 主流程（pi 自己负责）。
 * - 不持有状态：每次调用启动新子进程（MVP）。

 * 【INVARIANT】
 * - 8 个工具的 schema 与 Poirot SpecialistMcpServer 暴露的接口保持一致。
 * - 工具名统一加 `poirot_` 前缀（避免与 pi 其他工具冲突）。
 * - 所有工具经 stdio MCP 转发，不直接操作文件系统。
 * - POIROT_SANDBOX_MCP_ENDPOINT 由 PiRuntime._build_env 注入，未设置则抛错。
 * - MVP：每次调用启动新 SpecialistMcpServer 子进程，不复用连接。
 * - 物理无隔离：pi 写的文件 lead agent 立即可见（沙箱是逻辑约束，非物理隔离）。
 * - jiti 加载，跳过 tsc 类型检查；用 // @ts-nocheck 声明。
 * - 不依赖 typebox：pi.registerTool 接受 plain JSON Schema object。
 *
 * 【运行时环境】
 * 此文件放在 Python 仓库（Poirot）内，但由 pi 用 jiti 加载执行。
 */

// @ts-nocheck — pi 用 jiti 加载，跳过 tsc 类型检查；Poirot 仓库无 npm 依赖

// 8 个工具名 + 参数 schema（与 Poirot SpecialistMcpServer 暴露的接口一致）。
// 用 plain JSON Schema object，不依赖 @sinclair/typebox。
const POIROT_TOOLS = [
  {
    name: "bash",
    description: "Execute a bash command in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        command: { type: "string", description: "The bash command to execute." },
      },
      required: ["command"],
    },
  },
  {
    name: "read_file",
    description: "Read a file from the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path to read." },
      },
      required: ["path"],
    },
  },
  {
    name: "write_file",
    description: "Write content to a file in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path to write." },
        content: { type: "string", description: "Content to write." },
      },
      required: ["path", "content"],
    },
  },
  {
    name: "list_dir",
    description: "List directory contents in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path to list." },
      },
      required: ["path"],
    },
  },
  {
    name: "str_replace",
    description: "Replace string in a file in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path." },
        old_str: { type: "string", description: "String to replace." },
        new_str: { type: "string", description: "Replacement string." },
      },
      required: ["path", "old_str", "new_str"],
    },
  },
  {
    name: "glob",
    description: "Find files matching glob pattern in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        pattern: { type: "string", description: "Glob pattern." },
        path: { type: "string", description: "Base path (optional)." },
      },
      required: ["pattern"],
    },
  },
  {
    name: "grep",
    description: "Search file contents with regex in the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        pattern: { type: "string", description: "Regex pattern." },
        path: { type: "string", description: "File or directory path." },
      },
      required: ["pattern", "path"],
    },
  },
  {
    name: "download_file",
    description: "Download a file to the Poirot sandbox (security-guarded).",
    parameters: {
      type: "object",
      properties: {
        url: { type: "string", description: "URL to download." },
        path: { type: "string", description: "Destination path." },
      },
      required: ["url", "path"],
    },
  },
];

/**
 * Extension 入口。
 *
 * pi 加载此 extension 后，agent 看到 poirot_bash / poirot_read_file / poirot_write_file
 * 等 8 个工具。所有工具通过 stdio MCP 协议调 Poirot SpecialistMcpServer。
 *
 * @param pi - pi 注入的 extension API，提供 registerTool 方法
 */
export default function (pi) {
  for (const toolDef of POIROT_TOOLS) {
    const toolName = toolDef.name;
    const poirotToolName = `poirot_${toolName}`;
    pi.registerTool({
      name: poirotToolName,
      label: `Poirot ${toolName}`,
      description: toolDef.description,
      parameters: toolDef.parameters,
      execute: async (_toolCallId, params) => {
        const result = await callPoirotMcp(toolName, params);
        return {
          content: [{ type: "text", text: result }],
          details: {},
        };
      },
    });
  }
}

/**
 * 通过 stdio MCP 协议调 Poirot SpecialistMcpServer。
 *
 * POIROT_SANDBOX_MCP_ENDPOINT env 由 PiRuntime._build_env 注入，格式：
 *   python -m poirot.backend.agents.multiagent.mcp.specialist_mcp_server --sandbox-id <id>
 *
 * 流程：
 * 1. 解析 endpoint 为命令 + 参数。
 * 2. spawn SpecialistMcpServer 子进程。
 * 3. 通过 stdio 发送 JSON-RPC：initialize → tools/call。
 * 4. 解析 stdout 里的 JSON-RPC response，提取 result.content 的 text 部分。
 *
 * MVP：每次调用启动新子进程（与 codex / claude 的 SpecialistMcpServer 调用模式一致）。
 * 进阶：可改为持久 MCP client 连接。
 *
 * @param tool - 工具名（不带 poirot_ 前缀，如 "bash" / "read_file"）
 * @param args - 工具参数对象
 * @returns 工具执行结果的文本
 */
async function callPoirotMcp(tool, args) {
  const endpoint = process.env.POIROT_SANDBOX_MCP_ENDPOINT;
  if (!endpoint) {
    throw new Error(
      "POIROT_SANDBOX_MCP_ENDPOINT not set. PiRuntime._build_env should inject it."
    );
  }

  // 解析 endpoint：python -m poirot.backend.agents.multiagent.mcp.specialist_mcp_server --sandbox-id <id>
  const cmdParts = endpoint.split(" ");

  // 启动 SpecialistMcpServer 子进程
  const { spawn } = require("child_process");
  const proc = spawn(cmdParts[0], cmdParts.slice(1), {
    stdio: ["pipe", "pipe", "pipe"],
  });

  return new Promise((resolve, reject) => {
    let stdoutBuffer = "";
    let stderrBuffer = "";

    proc.stdout.on("data", (data) => {
      stdoutBuffer += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderrBuffer += data.toString();
    });

    proc.on("error", (err) => {
      reject(new Error(`SpecialistMcpServer spawn failed: ${err.message}`));
    });

    proc.on("close", (code) => {
      if (code !== 0) {
        reject(
          new Error(
            `SpecialistMcpServer exited with code ${code}: ${stderrBuffer}`
          )
        );
        return;
      }
      // 尝试从 stdout 解析 MCP JSON-RPC response
      // MCP stdio 协议：每行一个 JSON-RPC message
      const lines = stdoutBuffer.split("\n").filter((l) => l.trim());
      for (const line of lines) {
        try {
          const msg = JSON.parse(line);
          // JSON-RPC response 含 result 或 error
          if (msg.result && msg.result.content) {
            // MCP tool result: { content: [{ type: "text", text: "..." }] }
            const textParts = msg.result.content
              .filter((c) => c.type === "text")
              .map((c) => c.text);
            resolve(textParts.join("\n") || "(empty result)");
            return;
          }
          if (msg.error) {
            reject(new Error(`MCP error: ${msg.error.message || JSON.stringify(msg.error)}`));
            return;
          }
        } catch {
          // 非 JSON 行跳过
          continue;
        }
      }
      // 无有效 MCP response，返原始 stdout
      resolve(stdoutBuffer || "(empty result)");
    });

    // 发送 MCP JSON-RPC 请求：initialize → callTool
    // MCP stdio 协议需先 initialize 再 callTool
    const initRequest = {
      jsonrpc: "2.0",
      id: 0,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "pi-sandbox-bridge", version: "0.1.0" },
      },
    };
    const callRequest = {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: tool,
        arguments: args,
      },
    };
    proc.stdin.write(JSON.stringify(initRequest) + "\n");
    proc.stdin.write(JSON.stringify(callRequest) + "\n");
    proc.stdin.end();
  });
}