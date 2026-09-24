```
.
├── .venv                                                            - Python 虚拟环境目录（不建议纳入版本管理）
├── resource                                                         - 资源目录（项目配套资源文件）
├── .dockerignore                                                    - Docker 构建忽略文件配置
├── .env.example                                                     - 环境变量示例文件
├── .gitignore                                                       - Git 忽略文件配置
├── docker-compose.yml                                               - Docker Compose 编排文件
├── Dockerfile                                                       - 容器镜像构建文件
├── LICENSE                                                          - 开源许可证
├── pyproject.toml                                                   - Python 项目配置与依赖声明
├── README.md                                                        - 项目说明文档
├── THIRD_PARTY_LICENSES.md                                          - 第三方许可证说明
├── USAGE.md                                                         - 使用说明文档
│
├── poirot                                                            - 项目主包
│   ├── __init__.py                                                  - 包初始化文件
│   ├── backend                                                      - 后端主目录
│   │   ├── __init__.py                                              - 后端包初始化文件
│   │   ├── agents                                                   - Agent 核心模块
│   │   │   ├── __init__.py                                          - Agent 包初始化文件
│   │   │   ├── agent_tools                                          - Agent 工具模块
│   │   │   │   ├── __init__.py                                      - 工具包初始化文件
│   │   │   │   ├── available.py                                     - 可用工具注册/发现
│   │   │   │   ├── mcp_metadata.py                                  - MCP 元数据定义
│   │   │   │   ├── builtin                                            - 内置工具目录
│   │   │   │   │   ├── __init__.py                                  - 内置工具包初始化文件
│   │   │   │   │   ├── ask_help.py                                  - 请求帮助工具
│   │   │   │   │   ├── ddg_search.py                                - DuckDuckGo 搜索工具
│   │   │   │   │   ├── read_snapshot.py                             - 读取快照工具
│   │   │   │   │   └── skill_search.py                              - 技能搜索工具
│   │   │   ├── artifacts                                            - 产物/工件管理模块
│   │   │   │   ├── __init__.py                                      - 产物包初始化文件
│   │   │   │   ├── local_store.py                                   - 本地产物存储
│   │   │   │   └── server.py                                        - 产物服务端
│   │   │   ├── capabilities                                         - 能力注册与管理模块
│   │   │   │   ├── __init__.py                                      - 能力包初始化文件
│   │   │   │   ├── registry.py                                      - 能力注册表
│   │   │   │   └── models                                           - 能力模型目录
│   │   │   │       └── __init__.py                                  - 能力模型包初始化文件
│   │   │   ├── config                                               - Agent 配置模块
│   │   │   │   ├── __init__.py                                      - 配置包初始化文件
│   │   │   │   ├── defaults.py                                      - 默认配置
│   │   │   │   ├── fallback_model.py                                - 回退模型配置
│   │   │   │   ├── loader.py                                        - 配置加载器
│   │   │   │   ├── model_router.py                                  - 模型路由
│   │   │   │   ├── provider_config.py                               - 模型提供者配置
│   │   │   │   ├── provider_profile.py                              - 模型提供者档案
│   │   │   │   ├── schema.py                                        - 配置 Schema
│   │   │   │   └── profiles                                         - 配置档案目录
│   │   │   │       ├── expert.yaml                                  - 专家模式配置
│   │   │   │       ├── fast.yaml                                    - 快速模式配置
│   │   │   │       └── general.yaml                                 - 通用模式配置
│   │   │   ├── context_engineering                                  - 上下文工程模块
│   │   │   │   ├── __init__.py                                      - 上下文工程包初始化文件
│   │   │   │   ├── builder.py                                       - 上下文构建器
│   │   │   │   ├── contract.py                                      - 上下文契约
│   │   │   │   ├── registry.py                                      - 上下文策略注册表
│   │   │   │   ├── strategy_middleware.py                           - 上下文策略中间件
│   │   │   │   ├── utilities.py                                     - 上下文工具函数
│   │   │   │   └── strategies                                     - 上下文策略目录
│   │   │   │       ├── __init__.py                                  - 策略包初始化文件
│   │   │   │       └── default                                      - 默认上下文策略
│   │   │   │           ├── __init__.py                              - 默认策略包初始化文件
│   │   │   │           ├── _constants.py                            - 默认策略常量
│   │   │   │           ├── budget.py                                - 上下文预算控制
│   │   │   │           ├── externalizer.py                          - 上下文外置化
│   │   │   │           ├── snapshot.py                              - 上下文快照
│   │   │   │           ├── strategy.py                              - 默认策略实现
│   │   │   │           └── summarizer.py                            - 上下文摘要器
│   │   │   ├── intent                                               - 意图识别模块
│   │   │   │   ├── __init__.py                                      - 意图包初始化文件
│   │   │   │   └── engine.py                                        - 意图引擎
│   │   │   ├── journal                                              - 运行日志模块
│   │   │   │   ├── __init__.py                                      - 日志包初始化文件
│   │   │   │   ├── events.py                                        - 日志事件定义
│   │   │   │   └── run_journal.py                                   - 运行日志记录
│   │   │   ├── leader                                               - Leader Agent 模块
│   │   │   │   ├── __init__.py                                      - Leader 包初始化文件
│   │   │   │   ├── agent.py                                         - Leader Agent 实现
│   │   │   │   ├── factory.py                                       - Leader Agent 工厂
│   │   │   │   └── prompts.py                                       - Leader 提示词
│   │   │   ├── mcp                                                  - MCP 协议模块
│   │   │   │   ├── __init__.py                                      - MCP 包初始化文件
│   │   │   │   ├── audit.py                                         - MCP 审计
│   │   │   │   ├── config.py                                        - MCP 配置
│   │   │   │   ├── health.py                                        - MCP 健康检查
│   │   │   │   ├── loader.py                                        - MCP 加载器
│   │   │   │   ├── registry.py                                      - MCP 注册表
│   │   │   │   └── guards                                           - MCP 安全守卫
│   │   │   │       ├── __init__.py                                  - MCP 守卫包初始化文件
│   │   │   │       ├── base.py                                      - 守卫基类
│   │   │   │       ├── credential_sanitizer.py                      - 凭证脱敏守卫
│   │   │   │       ├── description_scanner.py                       - 描述扫描守卫
│   │   │   │       └── env_filter.py                                - 环境变量过滤守卫
│   │   │   ├── memory                                               - 记忆模块
│   │   │   │   ├── __init__.py                                      - 记忆包初始化文件
│   │   │   │   ├── bootstrap.py                                     - 记忆初始化
│   │   │   │   ├── config.py                                        - 记忆配置
│   │   │   │   ├── decay_policy.py                                  - 记忆衰减策略
│   │   │   │   ├── exceptions.py                                    - 记忆异常
│   │   │   │   ├── forget_policy.py                                 - 遗忘策略
│   │   │   │   ├── memory_manager.py                                - 记忆管理器
│   │   │   │   ├── memory_provider.py                               - 记忆提供者
│   │   │   │   ├── memory_store.py                                  - 记忆存储
│   │   │   │   ├── persona_policy.py                                - 人设策略
│   │   │   │   ├── retriever.py                                     - 记忆检索器
│   │   │   │   ├── schema.py                                        - 记忆 Schema
│   │   │   │   ├── types.py                                         - 记忆类型定义
│   │   │   │   ├── worker.py                                        - 记忆后台任务
│   │   │   │   ├── adapters                                         - 记忆适配器目录
│   │   │   │   │   ├── __init__.py                                  - 适配器包初始化文件
│   │   │   │   │   ├── graph_store.py                               - 图存储适配器
│   │   │   │   │   └── vector_store.py                              - 向量存储适配器
│   │   │   │   └── strategies                                     - 记忆策略目录
│   │   │   │       ├── __init__.py                                  - 记忆策略包初始化文件
│   │   │   │       └── default                                      - 默认记忆策略
│   │   │   │           ├── __init__.py                              - 默认策略包初始化文件
│   │   │   │           ├── _constants.py                            - 默认策略常量
│   │   │   │           ├── decay.py                                 - 衰减实现
│   │   │   │           ├── forget.py                                - 遗忘实现
│   │   │   │           ├── manager.py                               - 默认记忆管理器
│   │   │   │           ├── retriever.py                             - 默认检索器
│   │   │   │           ├── store.py                                 - 默认存储
│   │   │   │           └── strategy.py                              - 默认策略实现
│   │   │   ├── middlewares                                        - Agent 中间件目录
│   │   │   │   ├── __init__.py                                      - 中间件包初始化文件
│   │   │   │   ├── _jump_budget.py                                  - 跳转预算控制
│   │   │   │   ├── dangling_tool_call_middleware.py                 - 悬空工具调用处理中间件
│   │   │   │   ├── evidence_middleware.py                           - 证据收集中间件
│   │   │   │   ├── help_request_middleware.py                       - 帮助请求中间件
│   │   │   │   ├── loop_detection_middleware.py                     - 循环检测中间件
│   │   │   │   ├── memory_consolidation_middleware.py               - 记忆巩固中间件
│   │   │   │   ├── memory_recall_middleware.py                      - 记忆召回中间件
│   │   │   │   ├── message_normalizer_middleware.py                 - 消息规范化中间件
│   │   │   │   ├── reflection_middleware.py                         - 反思中间件
│   │   │   │   ├── report_middleware.py                             - 报告中间件
│   │   │   │   ├── run_journal_middleware.py                        - 运行日志中间件
│   │   │   │   ├── sandbox_middleware.py                            - 沙箱中间件
│   │   │   │   ├── skill_activation_middleware.py                   - 技能激活中间件
│   │   │   │   ├── skill_injection_middleware.py                    - 技能注入中间件
│   │   │   │   ├── skill_metrics_middleware.py                      - 技能指标中间件
│   │   │   │   ├── stall_detection_middleware.py                    - 停滞检测中间件
│   │   │   │   ├── summarization_middleware.py                      - 摘要中间件
│   │   │   │   ├── system_context_middleware.py                     - 系统上下文中中间件
│   │   │   │   ├── tagged_context_middleware.py                     - 标签化上下文中中间件
│   │   │   │   ├── title_middleware.py                              - 标题生成中间件
│   │   │   │   ├── todo_middleware.py                               - 待办中间件
│   │   │   │   └── tool_call_middleware.py                          - 工具调用中间件
│   │   │   ├── multiagent                                         - 多 Agent 协作模块
│   │   │   │   ├── __init__.py                                      - 多 Agent 包初始化文件
│   │   │   │   ├── bootstrap.py                                     - 多 Agent 初始化
│   │   │   │   ├── config.py                                        - 多 Agent 配置
│   │   │   │   ├── context_summarizer.py                            - 上下文摘要器
│   │   │   │   ├── credential_provider.py                           - 凭证提供者
│   │   │   │   ├── exceptions.py                                    - 多 Agent 异常
│   │   │   │   ├── metrics.py                                       - 多 Agent 指标
│   │   │   │   ├── middleware.py                                    - 多 Agent 中间件
│   │   │   │   ├── registry.py                                      - 多 Agent 注册表
│   │   │   │   ├── result_summarizer.py                             - 结果摘要器
│   │   │   │   ├── sandbox_binder.py                                - 沙箱绑定
│   │   │   │   ├── specialist.py                                    - 专家 Agent
│   │   │   │   ├── specialist_runtime.py                            - 专家运行时
│   │   │   │   ├── subagent.py                                      - 子 Agent
│   │   │   │   ├── tools.py                                         - 多 Agent 工具
│   │   │   │   ├── types.py                                         - 多 Agent 类型
│   │   │   │   ├── credentials                                    - 凭证目录
│   │   │   │   │   ├── __init__.py                                  - 凭证包初始化文件
│   │   │   │   │   ├── claude_credential.py                         - Claude 凭证
│   │   │   │   │   ├── codex_credential.py                          - Codex 凭证
│   │   │   │   │   └── pi_credential.py                             - Pi 凭证
│   │   │   │   ├── eval                                             - 评估目录
│   │   │   │   │   ├── __init__.py                                  - 评估包初始化文件
│   │   │   │   │   ├── bootstrap.py                                 - 评估初始化
│   │   │   │   │   ├── bridge.py                                    - 评估桥接
│   │   │   │   │   ├── cli.py                                       - 评估 CLI
│   │   │   │   │   ├── db.py                                        - 评估数据库
│   │   │   │   │   ├── decision_log.py                              - 决策日志
│   │   │   │   │   ├── facade.py                                    - 评估门面
│   │   │   │   │   ├── registry.py                                  - 评估注册表
│   │   │   │   │   ├── runtime_tracker.py                           - 运行时追踪
│   │   │   │   │   ├── types.py                                     - 评估类型
│   │   │   │   │   └── adapters                                     - 评估适配器
│   │   │   │   │       ├── __init__.py                              - 适配器包初始化文件
│   │   │   │   │       ├── llm_judge.py                             - LLM 评判适配器
│   │   │   │   │       ├── longitudinal_pairs.py                    - 纵向配对适配器
│   │   │   │   │       └── programmatic.py                          - 程序化评估适配器
│   │   │   │   ├── evolution                                      - 进化目录
│   │   │   │   │   ├── __init__.py                                  - 进化包初始化文件
│   │   │   │   │   ├── bootstrap.py                                 - 进化初始化
│   │   │   │   │   ├── budget_guard.py                              - 预算守卫
│   │   │   │   │   ├── cli.py                                       - 进化 CLI
│   │   │   │   │   ├── db.py                                        - 进化数据库
│   │   │   │   │   ├── evolution_mutator.py                         - 进化变异器
│   │   │   │   │   ├── failure_focuser.py                           - 失败聚焦器
│   │   │   │   │   ├── intent_strengthened.py                       - 意图增强
│   │   │   │   │   ├── metrics_l2.py                                - L2 指标
│   │   │   │   │   ├── metrics_view.py                              - 指标视图
│   │   │   │   │   ├── promotion_gate.py                            - 晋升门
│   │   │   │   │   ├── trigger_manager.py                           - 触发器管理器
│   │   │   │   │   ├── trigger_middleware.py                        - 触发中间件
│   │   │   │   │   ├── types.py                                     - 进化类型
│   │   │   │   │   ├── version_dag.py                               - 版本 DAG
│   │   │   │   │   └── worker.py                                    - 进化 Worker
│   │   │   │   ├── extensions                                     - 扩展目录
│   │   │   │   │   └── pi-sandbox-bridge                            - Pi 沙箱桥接扩展
│   │   │   │   │       └── index.ts                                 - TypeScript 入口
│   │   │   │   ├── installer                                      - 安装器目录
│   │   │   │   │   ├── __init__.py                                  - 安装器包初始化文件
│   │   │   │   │   └── pi_installer.py                              - Pi 安装器
│   │   │   │   ├── mcp                                              - 多 Agent MCP 目录
│   │   │   │   │   ├── __init__.py                                  - MCP 包初始化文件
│   │   │   │   │   └── specialist_mcp_server.py                     - 专家 MCP 服务端
│   │   │   │   ├── runtimes                                       - 运行时目录
│   │   │   │   │   ├── __init__.py                                  - 运行时包初始化文件
│   │   │   │   │   ├── claude_code_runtime.py                       - Claude Code 运行时
│   │   │   │   │   ├── codex_runtime.py                             - Codex 运行时
│   │   │   │   │   ├── pi_runtime.py                                - Pi 运行时
│   │   │   │   │   └── subagent_runtime.py                          - 子 Agent 运行时
│   │   │   │   ├── specialists                                    - 专家目录
│   │   │   │   │   ├── __init__.py                                  - 专家包初始化文件
│   │   │   │   │   ├── claude_code_specialist.py                    - Claude Code 专家
│   │   │   │   │   ├── codex_specialist.py                          - Codex 专家
│   │   │   │   │   ├── pi_specialist.py                             - Pi 专家
│   │   │   │   │   └── subagent_specialist.py                       - 子 Agent 专家
│   │   │   │   └── summarizers                                    - 摘要器目录
│   │   │   │       ├── __init__.py                                  - 摘要器包初始化文件
│   │   │   │       ├── context                                      - 上下文摘要器
│   │   │   │       │   ├── __init__.py                              - 上下文摘要器包初始化文件
│   │   │   │       │   ├── claude_code_context_summarizer.py        - Claude Code 上下文摘要器
│   │   │   │       │   ├── codex_context_summarizer.py              - Codex 上下文摘要器
│   │   │   │       │   ├── pi_context_summarizer.py                 - Pi 上下文摘要器
│   │   │   │       │   └── self_copy_context_summarizer.py          - 自复制上下文摘要器
│   │   │   │       └── result                                       - 结果摘要器
│   │   │   │           ├── __init__.py                              - 结果摘要器包初始化文件
│   │   │   │           ├── base.py                                  - 结果摘要器基类
│   │   │   │           ├── claude_code_result_summarizer.py         - Claude Code 结果摘要器
│   │   │   │           ├── codex_result_summarizer.py               - Codex 结果摘要器
│   │   │   │           ├── pi_result_summarizer.py                  - Pi 结果摘要器
│   │   │   │           └── self_copy_result_summarizer.py           - 自复制结果摘要器
│   │   │   ├── observability                                      - 可观测性目录
│   │   │   │   ├── __init__.py                                      - 可观测性包初始化文件
│   │   │   │   ├── activity_tracker.py                              - 活动追踪器
│   │   │   │   ├── interrupt_protection.py                          - 中断保护
│   │   │   │   ├── situation_report.py                              - 态势报告
│   │   │   │   └── stall_tracker.py                                 - 停滞追踪器
│   │   │   ├── prompts                                            - 提示词目录
│   │   │   │   ├── __init__.py                                      - 提示词包初始化文件
│   │   │   │   ├── manager.py                                       - 提示词管理器
│   │   │   │   └── system                                         - 系统提示词目录
│   │   │   │       ├── cli                                          - CLI 提示词
│   │   │   │       │   └── welcome.md                               - 欢迎提示词
│   │   │   │       ├── context_engineering                          - 上下文工程提示词
│   │   │   │       │   └── default                                  - 默认上下文工程提示词
│   │   │   │       │       └── summarize.md                         - 摘要提示词
│   │   │   │       ├── leader                                       - Leader 提示词
│   │   │   │       │   ├── constraints.md                           - 约束提示词
│   │   │   │       │   ├── decision_guidance.md                     - 决策指导提示词
│   │   │   │       │   ├── extensions.md                            - 扩展示例提示词
│   │   │   │       │   ├── identity.md                              - 身份提示词
│   │   │   │       │   ├── mode_expert.md                           - 专家模式提示词
│   │   │   │       │   └── skill_first_principle.md                 - 技能优先原则提示词
│   │   │   │       ├── reflection                                   - 反思提示词
│   │   │   │       │   └── sufficiency.md                           - 充分性反思提示词
│   │   │   │       ├── reporter                                     - 报告提示词
│   │   │   │       │   └── system.md                                - 报告系统提示词
│   │   │   │       └── todo                                         - 待办提示词
│   │   │   │           ├── completion_reminder.md                   - 完成提醒
│   │   │   │           ├── context_loss_reminder.md                 - 上下文丢失提醒
│   │   │   │           └── nag_reminder.md                          - 催促提醒
│   │   │   ├── reporting                                          - 报告模块
│   │   │   │   ├── __init__.py                                      - 报告包初始化文件
│   │   │   │   ├── markdown_reporter.py                             - Markdown 报告器
│   │   │   │   ├── result.py                                        - 报告结果
│   │   │   │   └── thread_report.py                                 - 线程报告
│   │   │   ├── runtime                                            - 运行时模块
│   │   │   │   ├── __init__.py                                      - 运行时包初始化文件
│   │   │   │   ├── checkpointer.py                                  - 检查点
│   │   │   │   ├── run_context.py                                   - 运行上下文
│   │   │   │   ├── run_manager.py                                   - 运行管理器
│   │   │   │   └── run_record.py                                    - 运行记录
│   │   │   ├── sandbox                                            - 沙箱模块
│   │   │   │   ├── __init__.py                                      - 沙箱包初始化文件
│   │   │   │   ├── exceptions.py                                    - 沙箱异常
│   │   │   │   ├── sandbox.py                                       - 沙箱核心
│   │   │   │   ├── types.py                                         - 沙箱类型
│   │   │   │   ├── contracts                                      - 沙箱契约目录
│   │   │   │   │   ├── __init__.py                                  - 契约包初始化文件
│   │   │   │   │   ├── path_translator.py                           - 路径转换器
│   │   │   │   │   ├── sandbox_backend.py                           - 沙箱后端
│   │   │   │   │   ├── sandbox_provider.py                          - 沙箱提供者
│   │   │   │   │   ├── sandbox_runtime.py                           - 沙箱运行时
│   │   │   │   │   └── security_guard.py                            - 安全守卫
│   │   │   │   ├── docker                                         - Docker 沙箱目录
│   │   │   │   │   ├── __init__.py                                  - Docker 包初始化文件
│   │   │   │   │   ├── cross_process_lock.py                        - 跨进程锁
│   │   │   │   │   ├── docker_sandbox_provider.py                   - Docker 沙箱提供者
│   │   │   │   │   ├── executor.py                                  - Docker 执行器
│   │   │   │   │   ├── local_container_backend.py                   - 本地容器后端
│   │   │   │   │   ├── readiness.py                                 - 就绪检查
│   │   │   │   │   └── remote_container_backend.py                  - 远程容器后端
│   │   │   │   ├── guards                                         - 沙箱守卫目录
│   │   │   │   │   ├── __init__.py                                  - 守卫包初始化文件
│   │   │   │   │   ├── audit_guard.py                               - 审计守卫
│   │   │   │   │   ├── docker_path_guard.py                         - Docker 路径守卫
│   │   │   │   │   ├── local_security_guard.py                      - 本地安全守卫
│   │   │   │   │   └── permissive_guard.py                          - 宽松守卫
│   │   │   │   ├── integration                                    - 沙箱集成目录
│   │   │   │   │   ├── __init__.py                                  - 集成包初始化文件
│   │   │   │   │   ├── bootstrap_sandbox.py                         - 沙箱启动
│   │   │   │   │   ├── config.py                                    - 沙箱配置
│   │   │   │   │   ├── context.py                                   - 沙箱上下文
│   │   │   │   │   └── tools.py                                     - 沙箱工具
│   │   │   │   ├── local                                        - 本地沙箱目录
│   │   │   │   │   ├── __init__.py                                  - 本地沙箱包初始化文件
│   │   │   │   │   └── local_sandbox_provider.py                    - 本地沙箱提供者
│   │   │   │   ├── runtimes                                     - 沙箱运行时目录
│   │   │   │   │   ├── __init__.py                                  - 运行时包初始化文件
│   │   │   │   │   ├── docker_runtime.py                            - Docker 运行时
│   │   │   │   │   └── local_runtime.py                             - 本地运行时
│   │   │   │   ├── translators                                  - 路径转换器目录
│   │   │   │   │   ├── __init__.py                                  - 转换器包初始化文件
│   │   │   │   │   ├── docker_path_translator.py                    - Docker 路径转换器
│   │   │   │   │   ├── identity_translator.py                       - 恒等转换器
│   │   │   │   │   └── local_path_translator.py                     - 本地路径转换器
│   │   │   │   └── utils                                        - 沙箱工具目录
│   │   │   │       ├── __init__.py                                  - 工具包初始化文件
│   │   │   │       ├── file_operation_lock.py                       - 文件操作锁
│   │   │   │       ├── sandbox_id.py                                - 沙箱 ID
│   │   │   │       └── search.py                                    - 搜索工具
│   │   │   ├── skill                                              - 技能模块
│   │   │   │   ├── __init__.py                                      - 技能包初始化文件
│   │   │   │   ├── _ctx.py                                          - 技能上下文
│   │   │   │   ├── config.py                                        - 技能配置
│   │   │   │   ├── injector.py                                      - 技能注入器
│   │   │   │   ├── parser.py                                        - 技能解析器
│   │   │   │   ├── selector.py                                      - 技能选择器
│   │   │   │   ├── store.py                                         - 技能存储
│   │   │   │   ├── types.py                                         - 技能类型
│   │   │   │   ├── builtin_skills                                 - 内置技能目录
│   │   │   │   │   ├── core                                         - 核心技能
│   │   │   │   │   │   ├── bootstrap                                - 启动技能
│   │   │   │   │   │   │   └── SKILL.md                             - 技能定义
│   │   │   │   │   │   ├── find-skills                              - 查找技能
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── github-code-review                       - GitHub 代码审查
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── plan                                     - 计划技能
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── requesting-code-review                   - 请求代码审查
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── simplify-code                            - 简化代码
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── skill-authoring                          - 技能编写
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── skill-creator                            - 技能创建器
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── source-verification                      - 来源验证
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── spike                                      - Spike 技能
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── systematic-debugging                     - 系统化调试
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   └── test-driven-development                  - 测试驱动开发
│   │   │   │   │   │       └── SKILL.md
│   │   │   │   │   ├── creative                                     - 创意技能
│   │   │   │   │   │   ├── architecture-diagram                     - 架构图
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── chart-visualization                      - 图表可视化
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── concept-diagrams                         - 概念图
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   └── frontend-design                          - 前端设计
│   │   │   │   │   │       └── SKILL.md
│   │   │   │   │   ├── productivity                                 - 生产力技能
│   │   │   │   │   │   ├── code-documentation                       - 代码文档
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   └── ppt-generation                           - PPT 生成
│   │   │   │   │   │       └── SKILL.md
│   │   │   │   │   ├── research                                     - 研究技能
│   │   │   │   │   │   ├── academic-paper-review                    - 学术论文评审
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── arxiv                                    - arXiv 技能
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── blogwatcher                              - 博客监控
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── consulting-analysis                      - 咨询分析
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── data-analysis                            - 数据分析
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── deep-research                            - 深度研究
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── github-deep-research                     - GitHub 深度研究
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── newsletter-generation                    - 新闻稿生成
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── osint-investigation                      - OSINT 调查
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   ├── research-paper-writing                   - 研究论文写作
│   │   │   │   │   │   │   └── SKILL.md
│   │   │   │   │   │   └── systematic-literature-review             - 系统文献综述
│   │   │   │   │   │       └── SKILL.md
│   │   │   │   │   └── software-development                         - 软件开发技能
│   │   │   │   │       ├── codebase-inspection                      - 代码库检查
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── github-auth                              - GitHub 认证
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── github-issues                            - GitHub Issues
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── github-pr-workflow                       - GitHub PR 工作流
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── github-repo-management                   - GitHub 仓库管理
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── node-inspect-debugger                    - Node 调试器
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       ├── python-debugpy                           - Python 调试器
│   │   │   │   │       │   └── SKILL.md
│   │   │   │   │       └── subagent-driven-development              - 子 Agent 驱动开发
│   │   │   │   │           └── SKILL.md
│   │   │   │   ├── eval                                           - 技能评估目录
│   │   │   │   │   ├── __init__.py                                  - 评估包初始化文件
│   │   │   │   │   ├── protocols.py                                 - 评估协议
│   │   │   │   │   ├── registry.py                                  - 评估注册表
│   │   │   │   │   ├── runtime_tracker.py                           - 运行时追踪
│   │   │   │   │   ├── types.py                                     - 评估类型
│   │   │   │   │   └── analyzers                                    - 分析器目录
│   │   │   │   │       ├── __init__.py                              - 分析器包初始化文件
│   │   │   │   │       ├── checks.py                                - 检查器
│   │   │   │   │       ├── contract_compiler.py                     - 契约编译器
│   │   │   │   │       ├── response_contract_checker.py             - 响应契约检查器
│   │   │   │   │       ├── skill_judgment_analyzer.py               - 技能判断分析器
│   │   │   │   │       └── task_quality_judge.py                    - 任务质量评判
│   │   │   │   ├── evolution                                      - 技能进化目录
│   │   │   │   │   ├── __init__.py                                  - 进化包初始化文件
│   │   │   │   │   ├── manager.py                                   - 进化管理器
│   │   │   │   │   ├── protocols.py                                 - 进化协议
│   │   │   │   │   ├── types.py                                     - 进化类型
│   │   │   │   │   ├── eval                                         - 进化评估
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── programmatic_bridge.py                   - 程序化桥接
│   │   │   │   │   ├── focus                                        - 聚焦器
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── ive_focuser.py                           - IVE 聚焦器
│   │   │   │   │   ├── gates                                        - 门控
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   ├── git_ratchet.py                           - Git 棘轮
│   │   │   │   │   │   ├── protocols.py                             - 门控协议
│   │   │   │   │   │   └── score_delta_gate.py                      - 分数增量门
│   │   │   │   │   ├── mutators                                     - 变异器
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── llm_mutator.py                           - LLM 变异器
│   │   │   │   │   └── triggers                                     - 触发器
│   │   │   │   │       ├── __init__.py
│   │   │   │   │       ├── capture_trigger.py                       - 捕获触发器
│   │   │   │   │       └── metric_monitor.py                        - 指标监控器
│   │   │   │   └── hub                                            - 技能中心目录
│   │   │   │       ├── __init__.py                                  - 技能中心包初始化文件
│   │   │   │       ├── hub_store.py                                 - 技能中心存储
│   │   │   │       ├── installer.py                                 - 技能安装器
│   │   │   │       ├── search.py                                    - 技能搜索
│   │   │   │       ├── source.py                                    - 技能源
│   │   │   │       └── sources                                      - 技能源目录
│   │   │   │           ├── __init__.py                              - 技能源包初始化文件
│   │   │   │           ├── builtin_source.py                        - 内置源
│   │   │   │           ├── claude_marketplace_source.py             - Claude 市场源
│   │   │   │           ├── github_source.py                         - GitHub 源
│   │   │   │           └── well_known_source.py                     - Well-known 源
│   │   │   └── state                                              - 状态模块
│   │   │       ├── __init__.py                                      - 状态包初始化文件
│   │   │       ├── reducers.py                                      - 状态 reducer
│   │   │       ├── thread_state.py                                  - 线程状态
│   │   │       └── types.py                                         - 状态类型
│   │   ├── app                                                    - 应用层模块
│   │   │   ├── __init__.py                                          - 应用包初始化文件
│   │   │   ├── bootstrap.py                                         - 应用启动
│   │   │   ├── cli                                                  - CLI 目录
│   │   │   │   ├── __init__.py                                      - CLI 包初始化文件
│   │   │   │   ├── banner.py                                        - CLI 横幅
│   │   │   │   ├── command_completer.py                             - 命令补全
│   │   │   │   ├── commands.py                                      - CLI 命令
│   │   │   │   ├── main.py                                          - CLI 入口
│   │   │   │   ├── registry.py                                      - CLI 注册表
│   │   │   │   ├── setup_wizard.py                                  - 设置向导
│   │   │   │   ├── status_bar.py                                    - 状态栏
│   │   │   │   └── stream_handler.py                                - 流处理器
│   │   │   ├── gateway                                              - 网关目录
│   │   │   │   └── __init__.py                                      - 网关包初始化文件
│   │   │   ├── schemas                                              - Schema 目录
│   │   │   │   └── __init__.py                                      - Schema 包初始化文件
│   │   │   ├── services                                             - 服务目录
│   │   │   │   ├── __init__.py                                      - 服务包初始化文件
│   │   │   │   └── stream_service.py                                - 流服务
│   │   │   └── tui                                                  - TUI 目录
│   │   │       ├── __init__.py                                      - TUI 包初始化文件
│   │   │       ├── app.py                                           - TUI 应用
│   │   │       ├── command_palette.py                               - 命令面板
│   │   │       ├── conversation.py                                  - 会话视图
│   │   │       ├── help_screen.py                                   - 帮助屏幕
│   │   │       ├── mcp_panel.py                                     - MCP 面板
│   │   │       ├── settings_screen.py                               - 设置屏幕
│   │   │       ├── side_panel.py                                    - 侧边面板
│   │   │       ├── status_bar.py                                    - 状态栏
│   │   │       └── theme.py                                         - 主题
│   │   └── tests                                                    - 测试目录（后端）
│   └── poirot.egg-info                                              - Python 包元数据目录
│
└── resource                                                         - 资源目录
```
