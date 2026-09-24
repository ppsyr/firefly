"""Configuration loading — 配置模块。

【整体职责】
负责应用配置的定义、加载与模型装配：从默认值出发，按模式叠加、应用 CLI 覆盖、
校验后装配为不可变的 AppConfig；同时提供 provider 声明、解析、角色路由与降级模型构造。

【内容摘要】
- schema                   : 配置数据结构（frozen dataclass），以 AppConfig 为根聚合。
- defaults                 : 默认配置（DEFAULT_CONFIG）与 expert 模式叠加层（EXPERT_PROFILE）。
- loader                   : 配置装载唯一入口（load_config），合并 → 覆盖 → 校验 → 组装。
- provider_profile         : 声明式 provider 定义（ProviderProfile 注册表）。
- provider_config          : provider 解析、选择、角色路由与 ChatModel 构造。
- fallback_model           : 运行时故障降级（FallbackChatModel）。
- model_router             : 角色化智能路由（ModelRouter），模型构造对外入口。

【职责边界】
- 只负责：配置结构定义、默认值、加载与校验、provider 声明与解析、模型构造与降级。
- 不负责：各子系统配置的内部语义（sandbox / skill / memory 由各自模块定义）、
  上下文治理策略实现（context_engineering）、模型调用后的业务处理（中间件 / summarizer）。

【两条装配线】
- 配置线：DEFAULT_CONFIG + EXPERT_PROFILE → CLI 覆盖 → 校验 → AppConfig
  （sandbox / memory 从 env 懒加载注入，skill 走默认工厂）。
- 模型线：discover_available_providers → route_chain_for → build_chat_model → FallbackChatModel。
"""