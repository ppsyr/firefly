.
├── .venv
├── resource
├── .dockerignore
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── LICENSE
├── pyproject.toml
├── README.md
├── THIRD_PARTY_LICENSES.md
├── USAGE.md
│
├── poirot
│   ├── __init__.py
│   ├── backend
│   │   ├── __init__.py
│   │   ├── agents
│   │   │   ├── __init__.py
│   │   │   ├── agent_tools
│   │   │   │   ├── __init__.py
│   │   │   │   ├── available.py
│   │   │   │   ├── mcp_metadata.py
│   │   │   │   ├── builtin
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── ask_help.py
│   │   │   │   │   ├── ddg_search.py
│   │   │   │   │   ├── read_snapshot.py
│   │   │   │   │   └── skill_search.py
│   │   │   ├── artifacts
│   │   │   │   ├── __init__.py
│   │   │   │   ├── local_store.py
│   │   │   │   └── server.py
│   │   │   ├── capabilities
│   │   │   │   ├── __init__.py
│   │   │   │   ├── registry.py
│   │   │   │   └── models
│   │   │   │       └── __init__.py
│   │   │   ├── config
│   │   │   │   ├── __init__.py
│   │   │   │   ├── defaults.py
│   │   │   │   ├── fallback_model.py
│   │   │   │   ├── loader.py
│   │   │   │   ├── model_router.py
│   │   │   │   ├── provider_config.py
│   │   │   │   ├── provider_profile.py
│   │   │   │   ├── schema.py
│   │   │   │   └── profiles
│   │   │   │       ├── expert.yaml
│   │   │   │       ├── fast.yaml
│   │   │   │       └── general.yaml
│   │   │   ├── context_engineering
│   │   │   │   ├── __init__.py
│   │   │   │   ├── builder.py
│   │   │   │   ├── contract.py
│   │   │   │   ├── registry.py
│   │   │   │   ├── strategy_middleware.py
│   │   │   │   ├── utilities.py
│   │   │   │   └── strategies
│   │   │   │       ├── __init__.py
│   │   │   │       └── default
│   │   │   │           ├── __init__.py
│   │   │   │           ├── _constants.py
│   │   │   │           ├── budget.py
│   │   │   │           ├── externalizer.py
│   │   │   │           ├── snapshot.py
│   │   │   │           ├── strategy.py
│   │   │   │           └── summarizer.py
│   │   │   ├── intent
│   │   │   │   ├── __init__.py
│   │   │   │   └── engine.py
│   │   │   ├── journal
│   │   │   │   ├── __init__.py
│   │   │   │   ├── events.py
│   │   │   │   └── run_journal.py
│   │   │   ├── leader
│   │   │   │   ├── __init__.py
│   │   │   │   ├── agent.py
│   │   │   │   ├── factory.py
│   │   │   │   └── prompts.py
│   │   │   ├── mcp
│   │   │   │   ├── __init__.py
│   │   │   │   ├── audit.py
│   │   │   │   ├── config.py
│   │   │   │   ├── health.py
│   │   │   │   ├── loader.py
│   │   │   │   ├── registry.py
│   │   │   │   └── guards
│   │   │   │       ├── __init__.py
│   │   │   │       ├── base.py
│   │   │   │       ├── credential_sanitizer.py
│   │   │   │       ├── description_scanner.py
│   │   │   │       └── env_filter.py
│   │   │   ├── memory
│   │   │   │   ├── __init__.py
│   │   │   │   ├── bootstrap.py
│   │   │   │   ├── config.py
│   │   │   │   ├── decay_policy.py
│   │   │   │   ├── exceptions.py
│   │   │   │   ├── forget_policy.py
│   │   │   │   ├── memory_manager.py
│   │   │   │   ├── memory_provider.py
│   │   │   │   ├── memory_store.py
│   │   │   │   ├── persona_policy.py
│   │   │   │   ├── retriever.py
│   │   │   │   ├── schema.py
│   │   │   │   ├── types.py
│   │   │   │   ├── worker.py
│   │   │   │   ├── adapters
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── graph_store.py
│   │   │   │   │   └── vector_store.py
│   │   │   │   └── strategies
│   │   │   │       ├── __init__.py
│   │   │   │       └── default
│   │   │   │           ├── __init__.py
│   │   │   │           ├── _constants.py
│   │   │   │           ├── decay.py
│   │   │   │           ├── forget.py
│   │   │   │           ├── manager.py
│   │   │   │           ├── retriever.py
│   │   │   │           ├── store.py
│   │   │   │           └── strategy.py
│   │   │   ├── middlewares
│   │   │   │   ├── __init__.py
│   │   │   │   ├── _jump_budget.py
│   │   │   │   ├── dangling_tool_call_middleware.py
│   │   │   │   ├── evidence_middleware.py
│   │   │   │   ├── help_request_middleware.py
│   │   │   │   ├── loop_detection_middleware.py
│   │   │   │   ├── memory_consolidation_middleware.py
│   │   │   │   ├── memory_recall_middleware.py
│   │   │   │   ├── message_normalizer_middleware.py
│   │   │   │   ├── reflection_middleware.py
│   │   │   │   ├── report_hint_middleware.py
│   │   │   │   ├── report_middleware.py
│   │   │   │   ├── run_journal_middleware.py
│   │   │   │   ├── sandbox_middleware.py
│   │   │   │   ├── skill_activation_middleware.py
│   │   │   │   ├── skill_injection_middleware.py
│   │   │   │   ├── skill_metrics_middleware.py
│   │   │   │   ├── stall_detection_middleware.py
│   │   │   │   ├── summarization_middleware.py
│   │   │   │   ├── system_context_middleware.py
│   │   │   │   ├── tagged_context_middleware.py
│   │   │   │   ├── title_middleware.py
│   │   │   │   ├── todo_middleware.py
│   │   │   │   └── tool_call_middleware.py
│   │   │   ├── multiagent
│   │   │   │   ├── __init__.py
│   │   │   │   ├── bootstrap.py
│   │   │   │   ├── config.py
│   │   │   │   ├── context_summarizer.py
│   │   │   │   ├── credential_provider.py
│   │   │   │   ├── exceptions.py
│   │   │   │   ├── metrics.py
│   │   │   │   ├── middleware.py
│   │   │   │   ├── registry.py
│   │   │   │   ├── result_summarizer.py
│   │   │   │   ├── sandbox_binder.py
│   │   │   │   ├── specialist.py
│   │   │   │   ├── specialist_runtime.py
│   │   │   │   ├── subagent.py
│   │   │   │   ├── tools.py
│   │   │   │   ├── types.py
│   │   │   │   ├── credentials
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── claude_credential.py
│   │   │   │   │   ├── codex_credential.py
│   │   │   │   │   └── pi_credential.py
│   │   │   │   ├── eval
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── bootstrap.py
│   │   │   │   │   ├── bridge.py
│   │   │   │   │   ├── cli.py
│   │   │   │   │   ├── db.py
│   │   │   │   │   ├── decision_log.py
│   │   │   │   │   ├── facade.py
│   │   │   │   │   ├── registry.py
│   │   │   │   │   ├── runtime_tracker.py
│   │   │   │   │   ├── types.py
│   │   │   │   │   └── adapters
│   │   │   │   │       ├── __init__.py
│   │   │   │   │       ├── llm_judge.py
│   │   │   │   │       ├── longitudinal_pairs.py
│   │   │   │   │       └── programmatic.py
│   │   │   │   ├── evolution
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── bootstrap.py
│   │   │   │   │   ├── budget_guard.py
│   │   │   │   │   ├── cli.py
│   │   │   │   │   ├── db.py
│   │   │   │   │   ├── evolution_mutator.py
│   │   │   │   │   ├── failure_focuser.py
│   │   │   │   │   ├── intent_strengthened.py
│   │   │   │   │   ├── metrics_l2.py
│   │   │   │   │   ├── metrics_view.py
│   │   │   │   │   ├── promotion_gate.py
│   │   │   │   │   ├── trigger_manager.py
│   │   │   │   │   ├── trigger_middleware.py
│   │   │   │   │   ├── types.py
│   │   │   │   │   ├── version_dag.py
│   │   │   │   │   └── worker.py
│   │   │   │   ├── extensions
│   │   │   │   │   └── pi-sandbox-bridge
│   │   │   │   │       └── index.ts
│   │   │   │   ├── installer
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   └── pi_installer.py
│   │   │   │   ├── mcp
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   └── specialist_mcp_server.py
│   │   │   │   ├── runtimes
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── claude_code_runtime.py
│   │   │   │   │   ├── codex_runtime.py
│   │   │   │   │   ├── pi_runtime.py
│   │   │   │   │   └── subagent_runtime.py
│   │   │   │   ├── specialists
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── claude_code_specialist.py
│   │   │   │   │   ├── codex_specialist.py
│   │   │   │   │   ├── pi_specialist.py
│   │   │   │   │   └── subagent_specialist.py
│   │   │   │   └── summarizers
│   │   │   │       ├── __init__.py
│   │   │   │       ├── context
│   │   │   │       │   ├── __init__.py
│   │   │   │       │   ├── claude_code_context_summarizer.py
│   │   │   │       │   ├── codex_context_summarizer.py
│   │   │   │       │   ├── pi_context_summarizer.py
│   │   │   │       │   └── self_copy_context_summarizer.py
│   │   │   │       └── result
│   │   │   │           ├── __init__.py
│   │   │   │           ├── base.py
│   │   │   │           ├── claude_code_result_summarizer.py
│   │   │   │           ├── codex_result_summarizer.py
│   │   │   │           ├── pi_result_summarizer.py
│   │   │   │           └── self_copy_result_summarizer.py
│   │   │   ├── observability
│   │   │   │   ├── __init__.py
│   │   │   │   ├── activity_tracker.py
│   │   │   │   ├── interrupt_protection.py
│   │   │   │   ├── situation_report.py
│   │   │   │   └── stall_tracker.py
│   │   │   ├── prompts
│   │   │   │   ├── __init__.py
│   │   │   │   ├── manager.py
│   │   │   │   └── system
│   │   │   │       ├── cli
│   │   │   │       │   └── welcome.md
│   │   │   │       ├── context_engineering
│   │   │   │       │   └── default
│   │   │   │       │       └── summarize.md
│   │   │   │       ├── leader
│   │   │   │       │   ├── constraints.md
│   │   │   │       │   ├── decision_guidance.md
│   │   │   │       │   ├── extensions.md
│   │   │   │       │   ├── identity.md
│   │   │   │       │   ├── mode_expert.md
│   │   │   │       │   └── skill_first_principle.md
│   │   │   │       ├── reflection
│   │   │   │       │   └── sufficiency.md
│   │   │   │       ├── reporter
│   │   │   │       │   └── system.md
│   │   │   │       └── todo
│   │   │   │           ├── completion_reminder.md
│   │   │   │           ├── context_loss_reminder.md
│   │   │   │           └── nag_reminder.md
│   │   │   ├── reporting
│   │   │   │   ├── __init__.py
│   │   │   │   ├── markdown_reporter.py
│   │   │   │   ├── result.py
│   │   │   │   └── thread_report.py
│   │   │   ├── runtime
│   │   │   │   ├── __init__.py
│   │   │   │   ├── checkpointer.py
│   │   │   │   ├── run_context.py
│   │   │   │   ├── run_manager.py
│   │   │   │   └── run_record.py
│   │   │   ├── sandbox
│   │   │   │   ├── __init__.py
│   │   │   │   ├── exceptions.py
│   │   │   │   ├── sandbox.py
│   │   │   │   ├── types.py
│   │   │   │   ├── contracts
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── path_translator.py
│   │   │   │   │   ├── sandbox_backend.py
│   │   │   │   │   ├── sandbox_provider.py
│   │   │   │   │   ├── sandbox_runtime.py
│   │   │   │   │   └── security_guard.py
│   │   │   │   ├── docker
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── cross_process_lock.py
│   │   │   │   │   ├── docker_sandbox_provider.py
│   │   │   │   │   ├── executor.py
│   │   │   │   │   ├── local_container_backend.py
│   │   │   │   │   ├── readiness.py
│   │   │   │   │   └── remote_container_backend.py
│   │   │   │   ├── guards
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── audit_guard.py
│   │   │   │   │   ├── docker_path_guard.py
│   │   │   │   │   ├── local_security_guard.py
│   │   │   │   │   └── permissive_guard.py
│   │   │   │   ├── integration
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── bootstrap_sandbox.py
│   │   │   │   │   ├── config.py
│   │   │   │   │   ├── context.py
│   │   │   │   │   └── tools.py
│   │   │   │   ├── local
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   └── local_sandbox_provider.py
│   │   │   │   ├── runtimes
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── docker_runtime.py
│   │   │   │   │   └── local_runtime.py
│   │   │   │   ├── translators
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── docker_path_translator.py
│   │   │   │   │   ├── identity_translator.py
│   │   │   │   │   └── local_path_translator.py
│   │   │   │   └── utils
│   │   │   │       ├── __init__.py
│   │   │   │       ├── file_operation_lock.py
│   │   │   │       ├── sandbox_id.py
│   │   │   │       └── search.py
│   │   │   ├── skill
│   │   │   │   ├── __init__.py
│   │   │   │   ├── _ctx.py
│   │   │   │   ├── config.py
│   │   │   │   ├── injector.py
│   │   │   │   ├── parser.py
│   │   │   │   ├── selector.py
│   │   │   │   ├── store.py
│   │   │   │   ├── types.py
│   │   │   │   ├── builtin_skills
│   │   │   │   ├── eval
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── protocols.py
│   │   │   │   │   ├── registry.py
│   │   │   │   │   ├── runtime_tracker.py
│   │   │   │   │   ├── types.py
│   │   │   │   │   └── analyzers
│   │   │   │   │       ├── __init__.py
│   │   │   │   │       ├── checks.py
│   │   │   │   │       ├── contract_compiler.py
│   │   │   │   │       ├── response_contract_checker.py
│   │   │   │   │       ├── skill_judgment_analyzer.py
│   │   │   │   │       └── task_quality_judge.py
│   │   │   │   ├── evolution
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   ├── manager.py
│   │   │   │   │   ├── protocols.py
│   │   │   │   │   ├── types.py
│   │   │   │   │   ├── eval
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── programmatic_bridge.py
│   │   │   │   │   ├── focus
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── ive_focuser.py
│   │   │   │   │   ├── gates
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   ├── git_ratchet.py
│   │   │   │   │   │   ├── protocols.py
│   │   │   │   │   │   └── score_delta_gate.py
│   │   │   │   │   ├── mutators
│   │   │   │   │   │   ├── __init__.py
│   │   │   │   │   │   └── llm_mutator.py
│   │   │   │   │   └── triggers
│   │   │   │   │       ├── __init__.py
│   │   │   │   │       ├── capture_trigger.py
│   │   │   │   │       └── metric_monitor.py
│   │   │   │   └── hub
│   │   │   │       ├── __init__.py
│   │   │   │       ├── hub_store.py
│   │   │   │       ├── installer.py
│   │   │   │       ├── search.py
│   │   │   │       ├── source.py
│   │   │   │       └── sources
│   │   │   │           ├── __init__.py
│   │   │   │           ├── builtin_source.py
│   │   │   │           ├── claude_marketplace_source.py
│   │   │   │           ├── github_source.py
│   │   │   │           └── well_known_source.py
│   │   │   └── state
│   │   │       ├── __init__.py
│   │   │       ├── reducers.py
│   │   │       ├── thread_state.py
│   │   │       └── types.py
│   │   ├── app
│   │   │   ├── __init__.py
│   │   │   ├── bootstrap.py
│   │   │   ├── cli
│   │   │   │   ├── __init__.py
│   │   │   │   ├── banner.py
│   │   │   │   ├── command_completer.py
│   │   │   │   ├── commands.py
│   │   │   │   ├── main.py
│   │   │   │   ├── registry.py
│   │   │   │   ├── setup_wizard.py
│   │   │   │   ├── status_bar.py
│   │   │   │   └── stream_handler.py
│   │   │   ├── gateway
│   │   │   │   └── __init__.py
│   │   │   ├── schemas
│   │   │   │   └── __init__.py
│   │   │   ├── services
│   │   │   │   ├── __init__.py
│   │   │   │   └── stream_service.py
│   │   │   └── tui
│   │   │       ├── __init__.py
│   │   │       ├── app.py
│   │   │       ├── command_palette.py
│   │   │       ├── conversation.py
│   │   │       ├── help_screen.py
│   │   │       ├── mcp_panel.py
│   │   │       ├── settings_screen.py
│   │   │       ├── side_panel.py
│   │   │       ├── status_bar.py
│   │   │       └── theme.py
│   │   └── tests
│   └── poirot.egg-info
│
└── resource