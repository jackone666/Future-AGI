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

1. 先读 [tracer.md](./tracer.md)，理解 Future AGI 如何统一记录运行过程。
2. 再读 [simulate.md](./simulate.md)，看测试定义如何变成真实执行和评测输入。
3. 然后读 [agentic-eval.md](./agentic-eval.md)，理解 evaluator 如何把输出变成结构化分数。
4. 最后读 [guardrails.md](./guardrails.md)，把评测、安全检查和生产 gateway 链路连起来。

如果目标是面试或快速讲清项目价值，可以按业务闭环讲：

```text
我们先用 simulate 生成和执行场景，
再用 agentic_eval 自动评测结果，
所有过程由 tracer 记录和分析，
上线后由 guardrails 在 gateway 中实时保护。
```

## 4. 四个模块的核心问题

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

## 5. 源码学习方法

不要从目录树第一行开始逐文件读。这个项目代码量很大，更适合按“实体 -> 执行链路 -> 分析查询 -> 扩展点”读：

1. 先读模型，确定数据边界。
2. 再读 service/workflow/plugin，确定状态怎么流动。
3. 再读 query builder/schema，确定数据如何被分析。
4. 最后读具体 scanner/evaluator/provider，理解扩展方式。

## 6. 关键协作链路

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

## 7. 学习检查清单

读完后，你应该能回答：

- 新增一个 guardrail scanner 应该改哪些文件？
- 新增一个 evaluator 应该返回什么结构？
- 一次 simulation 为什么会有多个 call execution？
- Temporal parent workflow 如何知道子 call 完成？
- EvalLogger 的 span/trace/session target 有什么区别？
- Trace list 为什么不一次性查 input/output？
- Guardrail 命中后为什么不是直接写数据库，而是先写 RequestContext？

如果这些问题能答上来，四个模块的主线就基本通了。
