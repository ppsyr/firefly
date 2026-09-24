"""Observability layer — runtime activity tracking, stall detection, situation reports.

【整体职责】
监控运行健康度：追踪运行期活动（是否在活动）、检测停滞（是否卡住）、保护原子任务
（卡住时能否暂停）、组织求助态势（如何向用户求助）。构成一条"活动 → 停滞 → 保护 →
求助"的因果链，为 HITL 与界面展示提供信号。

【内容摘要】
- activity_tracker     : 活动追踪器（RunActivityTracker），记录模型/工具/沙箱/specialist
                         调用的生命周期与心跳，供 TUI/CLI 轮询与事件播报。
- stall_tracker        : 停滞追踪器（StallTracker），四类信号判定 stuck 并给出原因，
                         含成功衰减窗口防误报。
- interrupt_protection : 中断保护，线程局部标志，供 middleware 暂停前检查，保护原子任务。
- situation_report     : 态势报告（SituationReport），两段式构建（程序化提取 + LLM 补方案），
                         渲染为求助报告。

【职责边界】
- 只负责：活动记录、停滞判定、中断保护标志、求助态势组织。
- 不负责：停滞的处置决策（stall_detection_middleware / HITL）、受保护任务本身的执行
  （上下文压缩 / 证据持久化 / 报告生成）、界面展示（TUI / CLI）。

【四层因果链】
- activity_tracker   ：还活着吗（记录活动 + 心跳）。
- stall_tracker      ：卡住了吗（四类信号判定停滞）。
- interrupt_protection：卡住后怎么办（暂停前检查保护）。
- situation_report   ：对外怎么呈现（求助报告）。
"""
