# Future AGI 后端源码学习地图

本文是 `guardrails`、`agentic_eval`、`simulate`、`tracer` 四个后端核心模块的总入口。四篇详细源码学习手册已经按“模块定位、源码链接、执行链路、扩展点、排错方向”展开：

- [Guardrails 后端源码学习手册](./guardrails.md)
- [Agentic Eval 后端源码学习手册](./agentic-eval.md)
- [Simulate 后端源码学习手册](./simulate.md)
- [Tracer 后端源码学习手册](./tracer.md)

## 1. 总体闭环

Future AGI 的后端可以按一条可靠性闭环理解：

```text
simulate 生成/执行测试场景
  -> agentic_eval 判断结果质量
  -> tracer 记录运行、评测和错误
  -> guardrails 在生产链路实时防护
  -> trace/eval/simulation 数据反馈下一轮优化
```

这四个模块不是孤立的：

- `simulate` 负责把 agent 放进场景里跑，生成 call/test execution。
- `agentic_eval` 负责对输出、调用结果、数据集样本或模拟 transcript 做评测。
- `tracer` 负责保存 trace、span、eval、annotation、错误聚类和 ClickHouse 分析数据。
- `guardrails` 负责在 gateway 请求前后做实时安全检查，并把命中结果写入上下文和 trace。

## 2. 一页源码入口

| 模块 | 详细文档 | 最关键代码入口 |
|---|---|---|
| Guardrails | [guardrails.md](./guardrails.md) | [Guardrail 接口](../../agentcc-gateway/internal/guardrails/guardrail.go#L48)、[GuardrailPlugin](../../agentcc-gateway/internal/guardrails/plugin.go#L60)、[Engine](../../agentcc-gateway/internal/guardrails/engine.go#L35) |
| Agentic Eval | [agentic-eval.md](./agentic-eval.md) | [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25)、[EvalResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37)、[LLM](../../futureagi/agentic_eval/core/llm/llm.py#L171) |
| Simulate | [simulate.md](./simulate.md) | [RunTest](../../futureagi/simulate/models/run_test.py#L16)、[TestExecution](../../futureagi/simulate/models/test_execution.py#L24)、[TestExecutionWorkflow](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L65) |
| Tracer | [tracer.md](./tracer.md) | [Trace](../../futureagi/tracer/models/trace.py#L21)、[ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92)、[TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27) |

## 3. 推荐阅读顺序

如果目标是快速读懂后端主线，建议按这个顺序：

1. 先读本文的“核心原理与面试回答”部分，掌握高频问题的回答抓手。
2. 再读 [tracer.md](./tracer.md)，理解 Future AGI 如何统一记录运行过程。
3. 然后读 [simulate.md](./simulate.md)，看测试定义如何变成真实执行和评测输入。
4. 接着读 [agentic-eval.md](./agentic-eval.md)，理解 evaluator 如何把输出变成结构化分数。
5. 最后读 [guardrails.md](./guardrails.md)，把评测、安全检查和生产 gateway 链路连起来。

如果目标是面试或快速讲清项目价值，可以按业务闭环讲：

```text
我们先用 simulate 生成和执行场景，
再用 agentic_eval 自动评测结果，
所有过程由 tracer 记录和分析，
上线后由 guardrails 在 gateway 中实时保护。
```

## 4. 核心原理与面试回答

这一节只保留面试高概率会问的原理。具体 vendor adapter 参数、每个 Django 字段 help_text、历史 migration 原因、前端页面细节，不建议优先背。

### 4.1 一句话讲清后端设计

Future AGI 后端不是训练大模型的系统，而是让 AI Agent 在生产环境里可测试、可评测、可观测、可防护、可持续优化。

可以这样答：

```text
上线前用 simulate 批量模拟真实场景，
再用 agentic_eval 自动评测输出质量，
线上和离线过程都由 tracer 记录成 trace/span/eval，
生产请求再由 guardrails 在 gateway 链路中实时防护，
最终把 trace/eval/simulation 数据反馈给下一轮 Agent 优化。
```

面试抓手：

- `simulate` 解决上线前测试。
- `agentic_eval` 解决质量判断。
- `tracer` 解决观测和分析。
- `guardrails` 解决实时安全。
- 四个模块形成闭环，而不是四个孤立工具。

### 4.2 为什么要把 Agent 可靠性拆成四个模块？

面试可能问：这个系统相比普通 LLM 网关多了什么？

普通 LLM 网关通常关注模型调用、鉴权、路由、限流和成本。Future AGI 在此基础上补了生产可靠性闭环：

- 测试阶段：用 `simulate` 主动构造边界场景。
- 判断阶段：用 `agentic_eval` 把质量变成结构化指标。
- 观测阶段：用 `tracer` 记录真实运行链路。
- 防护阶段：用 `guardrails` 在线阻断高风险输入或输出。

回答重点不是“功能很多”，而是“这些模块共享一条数据反馈链路”。

### 4.3 为什么模型定义、执行记录、分析记录要分层？

面试可能问：为什么不把所有信息都塞到一张表？

可以按三层回答：

- 定义层：例如 [RunTest](../../futureagi/simulate/models/run_test.py#L16)，描述“要测什么”。
- 执行层：例如 [TestExecution](../../futureagi/simulate/models/test_execution.py#L24)、[CallExecution](../../futureagi/simulate/models/test_execution.py#L184)，描述“这次运行发生了什么”。
- 分析层：例如 [Trace](../../futureagi/tracer/models/trace.py#L21)、[ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92)、[EvalLogger](../../futureagi/tracer/models/observation_span.py#L292)，描述“如何追踪、评测和聚合”。

这样设计的好处：

- 同一个定义可以多次执行。
- 每次执行可以保留独立历史。
- 分析查询可以面向性能优化，不污染业务模型。

### 4.4 为什么同步链路只做轻决策？

面试可能问：生产链路上怎么兼顾安全和延迟？

可以这样答：

Guardrails 插在 gateway 请求链路里，必须快速做 block/warn/log 决策，不能把复杂分析放进同步请求。命中结果先写入 `RequestContext`，由 logging/tracer 后续记录；复杂聚合、错误分析、dashboard 查询放到 Tracer 和 ClickHouse 层。

相关源码：

- [GuardrailPlugin.ProcessRequest](../../agentcc-gateway/internal/guardrails/plugin.go#L60)
- [storeGuardrailResults](../../agentcc-gateway/internal/guardrails/plugin.go#L493)
- [TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27)

### 4.5 Guardrails 面试高频

问题：Guardrail 怎么接入请求链路？

回答要点：

- Gateway pipeline 调 [ProcessRequest](../../agentcc-gateway/internal/guardrails/plugin.go#L60) 做 pre-stage 检查。
- Provider 返回后做 post-stage 检查。
- 每个 scanner 实现统一 [Guardrail 接口](../../agentcc-gateway/internal/guardrails/guardrail.go#L48)。
- scanner 返回 `CheckResult`，engine/plugin 根据分数、阈值和 action 统一处理。
- action 分为 block、warn、log。

问题：为什么区分 pre-stage 和 post-stage？

回答要点：

- pre-stage 看输入，防 prompt injection、PII、恶意工具参数。
- post-stage 看输出，防敏感信息泄露、不合规内容、幻觉风险。
- 两个阶段输入不同，风险类型也不同。

问题：流式输出怎么检查？

回答要点：

- 不能等完整响应结束，否则风险内容可能已经展示给用户。
- [StreamGuardrailChecker](../../agentcc-gateway/internal/guardrails/stream_checker.go#L15) 累积 chunk 文本，达到间隔后运行 post-stage 检查。
- 流结束时再用 [Finish](../../agentcc-gateway/internal/guardrails/stream_checker.go#L86) 做最终检查，避免跨 chunk 漏检。

### 4.6 Agentic Eval 面试高频

问题：Evaluator 的统一抽象是什么？

回答要点：

- [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25) 定义输入契约和执行生命周期。
- `required_args` 声明必须输入。
- `_evaluate()` 执行评测。
- 输出统一成 [EvalResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37)。
- `failure` 表示是否不合格，`metrics` 表示可聚合指标。

问题：LLM-as-judge 的结果怎么进入系统？

回答要点：

- evaluator 调 LLM 或规则生成判断。
- 输出统一成 `EvalResult`。
- 批量结果用 [BatchRunResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L59) 包装。
- 需要分析时写入 Fi/Tracer，最终通过 `EvalLogger` 和 ClickHouse 查询展示。

问题：批量并发如何保持结果顺序？

回答要点：

- [run_batch](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L240) 支持并发。
- [_run_batch_generator_async](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L161) 用 `future_to_index` 记录原始索引。
- future 完成后写回预分配数组对应位置，所以并发不影响输出顺序。

### 4.7 Simulate 面试高频

问题：`RunTest`、`TestExecution`、`CallExecution` 怎么区分？

回答要点：

```text
RunTest 是测试定义：要测什么。
TestExecution 是一次运行：这次跑得怎么样。
CallExecution 是单个场景/通话/聊天：每个 case 的细节。
```

相关源码：

- [RunTest](../../futureagi/simulate/models/run_test.py#L16)
- [TestExecution](../../futureagi/simulate/models/test_execution.py#L24)
- [CallExecution](../../futureagi/simulate/models/test_execution.py#L184)

问题：为什么用 Temporal？

回答要点：

Simulate 是长任务编排，不是一次简单 HTTP 调用。它要创建很多 call，每个 call 可能长时间运行，还要处理成功、失败、取消、analyzing、重试和恢复。[TestExecutionWorkflow](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L65) 用父 workflow 管理整体生命周期。

问题：为什么父 workflow 不轮询数据库？

回答要点：

子 workflow 通过 [call_completed](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L336) 和 [call_analyzing](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L351) 发 signal，父 workflow 在内存状态里维护计数。这样比轮询数据库更符合事件驱动模型。

问题：为什么需要 continue-as-new？

回答要点：

大规模测试会产生很多 Temporal event。[_checkpoint](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L527) 保存当前状态后 continue-as-new，避免 workflow history 过大。

### 4.8 Tracer 面试高频

问题：Trace 和 ObservationSpan 的关系是什么？

回答要点：

```text
Trace 是整条链路，ObservationSpan 是链路里的每个可观测节点。
```

[Trace](../../futureagi/tracer/models/trace.py#L21) 表示一次完整请求或运行；[ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92) 表示其中的 LLM 调用、tool 调用、agent 节点、guardrail、evaluator 等步骤。

问题：为什么要做语义约定归一？

回答要点：

不同 SDK 或 tracing 标准字段名不同。如果不归一，dashboard 和查询层会到处写兼容逻辑。[AttributeRegistry](../../futureagi/tracer/utils/semantic_conventions.py#L740) 把 OTEL GenAI、OpenInference、OpenLLMetry 等字段别名统一起来，上层只按统一字段查询。

问题：为什么 Trace List 查询要分阶段？

回答要点：

Trace list 既要分页，又可能展示 input/output/eval/metadata。如果一次性扫宽表重字段，会慢且容易 OOM。[TraceListQueryBuilder.build](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L107) 先查轻量 root span 做分页；[build_content_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L222) 只对当前页 trace_ids 查重列；[build_eval_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L373) 再查当前页 eval 分数。

问题：为什么 Postgres + ClickHouse 双存储？

回答要点：

- Postgres 适合事务、关系和主业务写入。
- ClickHouse 适合大规模筛选、聚合、dashboard 和 trace list。
- Tracer 通过 CDC、宽表、rollup 把写入模型和分析模型分开。

## 5. 面试不建议优先背的内容

这些内容不是面试重点，除非对方明确追问：

- 每个第三方 guardrail vendor adapter 的参数。
- 每个 Django 字段的完整 help_text。
- 每个 dashboard widget 的前端展示细节。
- 每个 migration 的历史原因。
- 每个 provider 的全部 payload 分支。
- `RunPrompt` 超长文件里的所有多模态边角逻辑。

优先掌握：模块边界、数据模型关系、执行链路、为什么这样设计、出问题时从哪里排查。

## 6. 四个模块的核心问题

学习每个模块时，可以带着这些问题读源码：

### Guardrails

- 一个 scanner 如何接入 gateway pipeline？
- pre-stage 和 post-stage 的输入有什么不同？
- 静态 config 和 org 动态 config 如何合并？
- block/warn/log 的处理路径分别是什么？
- 命中结果如何传给 tracer？

### Agentic Eval

- evaluator 的输入契约在哪里声明？
- `EvalResult.failure` 和 guard 模式是什么关系？
- 批量并发如何保持输出顺序？
- LLM provider fallback 在哪里处理？
- 评测结果如何写回 Fi/Tracer？

### Simulate

- `RunTest`、`TestExecution`、`CallExecution` 的边界是什么？
- Temporal 父 workflow 如何启动和等待子 workflow？
- 为什么需要 signal，而不是父 workflow 轮询数据库？
- 为什么需要 continue-as-new？
- call 完成后如何触发评测？

### Tracer

- Trace 和 ObservationSpan 如何建模一次 agent 运行？
- 不同 instrumentation 的属性如何归一？
- EvalLogger 如何同时支持 span/trace/session target？
- trace list 为什么分 Phase 1/Phase 2 查询？
- Error Feed 如何从 trace/eval/scanner 数据中提炼问题模式？

## 7. 源码学习方法

不要从目录树第一行开始逐文件读。这个项目代码量很大，更适合按“实体 -> 执行链路 -> 分析查询 -> 扩展点”读：

1. 先读模型，确定数据边界。
2. 再读 service/workflow/plugin，确定状态怎么流动。
3. 再读 query builder/schema，确定数据如何被分析。
4. 最后读具体 scanner/evaluator/provider，理解扩展方式。

## 8. 关键协作链路

### 模拟到评测

```text
RunTest
  -> TestExecution
  -> CallExecution
  -> transcript/provider data
  -> BaseEvaluator
  -> EvalResult
  -> EvalLogger/summary
```

相关入口：

- [RunTest](../../futureagi/simulate/models/run_test.py#L16)
- [CallExecution](../../futureagi/simulate/models/test_execution.py#L184)
- [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25)
- [EvalLogger](../../futureagi/tracer/models/observation_span.py#L292)

### 生产请求到防护

```text
gateway request
  -> GuardrailPlugin.ProcessRequest
  -> provider
  -> GuardrailPlugin.ProcessResponse
  -> RequestContext.GuardrailResults
  -> logging/tracer
```

相关入口：

- [GuardrailPlugin.ProcessRequest](../../agentcc-gateway/internal/guardrails/plugin.go#L60)
- [GuardrailPlugin.runOrgGuardrails](../../agentcc-gateway/internal/guardrails/plugin.go#L125)
- [storeGuardrailResults](../../agentcc-gateway/internal/guardrails/plugin.go#L493)
- [ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92)

### Trace 到分析

```text
Trace / ObservationSpan / EvalLogger
  -> PeerDB CDC
  -> ClickHouse spans/eval tables
  -> TraceListQueryBuilder
  -> dashboard / trace list / Error Feed
```

相关入口：

- [Trace](../../futureagi/tracer/models/trace.py#L21)
- [ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92)
- [ClickHouse schema](../../futureagi/tracer/services/clickhouse/schema.py#L67)
- [TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27)

## 9. 学习检查清单

读完后，你应该能回答：

- 新增一个 guardrail scanner 应该改哪些文件？
- 新增一个 evaluator 应该返回什么结构？
- 一次 simulation 为什么会有多个 call execution？
- Temporal parent workflow 如何知道子 call 完成？
- EvalLogger 的 span/trace/session target 有什么区别？
- Trace list 为什么不一次性查 input/output？
- Guardrail 命中后为什么不是直接写数据库，而是先写 RequestContext？

如果这些问题能答上来，四个模块的主线就基本通了。
