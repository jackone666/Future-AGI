# Tracer 后端源码学习手册

Tracer 是 Future AGI 的观测和分析核心。它把 agent、LLM、tool、retriever、guardrail、evaluator、conversation 等运行片段统一建模为 Trace 和 ObservationSpan，并通过 Postgres + ClickHouse 支撑在线查询、dashboard、错误聚类和评测分析。

## 1. 模块定位

Tracer 解决的是“生产环境发生了什么、为什么发生、影响了谁、下一步该修哪里”的问题。它不只存日志，而是把运行过程结构化成可查询、可聚合、可回放的数据。

核心职责：

- 保存 trace 根记录。
- 保存 trace 内部的 observation spans。
- 统一不同 instrumentation 标准的属性命名。
- 保存 evaluator 结果。
- 将 Postgres 数据同步到 ClickHouse 宽表。
- 为 trace list、dashboard、Error Feed 提供高性能查询。

## 2. 源码入口地图

| 代码 | 作用 |
|---|---|
| [Trace](../../futureagi/tracer/models/trace.py#L21) | 一次完整请求或会话片段的根记录。 |
| [ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92) | Trace 内部的可观测片段，例如 LLM/tool/guardrail/evaluator。 |
| [EvalLogger](../../futureagi/tracer/models/observation_span.py#L292) | 评测结果统一日志表。 |
| [EvalLogger.clean](../../futureagi/tracer/models/observation_span.py#L360) | 校验不同 target_type 的外键组合。 |
| [SemanticConvention](../../futureagi/tracer/utils/semantic_conventions.py#L17) | 支持的语义规范枚举。 |
| [AttributeRegistry](../../futureagi/tracer/utils/semantic_conventions.py#L740) | 属性别名注册和读取中心。 |
| [detect_convention](../../futureagi/tracer/utils/semantic_conventions.py#L974) | 根据 span attributes 推断使用的语义规范。 |
| [normalize_span_kind](../../futureagi/tracer/utils/semantic_conventions.py#L997) | 将不同规范的 span kind 归一。 |
| [ClickHouse schema](../../futureagi/tracer/services/clickhouse/schema.py#L67) | CDC、宽表、预聚合和分析表 DDL。 |
| [TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27) | trace 列表查询构造器。 |
| [TraceListQueryBuilder.build](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L107) | Phase 1 轻量分页查询。 |
| [build_content_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L222) | Phase 2 针对当前页读取 input/output 重列。 |
| [build_eval_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L373) | 当前页 eval 分数查询。 |
| [pivot_eval_results](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L610) | 将 eval 查询结果透视成 trace -> eval config 结构。 |
| [Error Feed list_clusters](../../futureagi/tracer/queries/feed.py#L314) | 错误聚类列表。 |
| [get_cluster_detail](../../futureagi/tracer/queries/feed.py#L401) | 错误聚类详情。 |
| [get_overview](../../futureagi/tracer/queries/feed.py#L1406) | Error Feed overview tab。 |
| [get_traces_tab](../../futureagi/tracer/queries/feed.py#L1555) | Error Feed traces tab。 |
| [get_trends_tab](../../futureagi/tracer/queries/feed.py#L1833) | Error Feed trends tab。 |

## 3. Trace 和 ObservationSpan

[Trace](../../futureagi/tracer/models/trace.py#L21) 是根记录，表示一次完整运行或请求。它保存：

- project 和 project_version。
- trace name。
- metadata。
- input/output/error。
- session。
- external_id。
- tags。
- error analysis 状态。

[ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92) 是 trace 内部片段。它保存：

- 所属 project、project_version、trace。
- parent_span_id，用于树结构。
- observation_type，例如 `llm`、`tool`、`agent`、`guardrail`、`evaluator`。
- operation_name，例如 `chat`、`image_generation`、`speech_to_text`。
- input/output。
- model/provider/token/cost/latency。
- status 和 status_message。
- metadata、span_events、resource_attributes。

一个 trace 可以有很多 span。Trace 负责“这一整次运行是什么”，ObservationSpan 负责“运行里面每一步是什么”。

## 4. ObservationType 为什么重要

`ObservationType` 在 [observation_span.py](../../futureagi/tracer/models/observation_span.py#L34) 中定义。它避免代码里到处写裸字符串。

新增类型时要同步考虑：

- Django model choices。
- ingestion/normalization。
- ClickHouse 宽表字段。
- query builder 过滤条件。
- 前端图标和过滤器。

例如 Guardrails 命中后进入 tracer，应该使用 `guardrail` span 类型；评测结果进入 tracer，则对应 `evaluator` 或 `EvalLogger`。

## 5. EvalLogger

[EvalLogger](../../futureagi/tracer/models/observation_span.py#L292) 是评测结果的统一入口。它可以挂在三个 target 上：

- span：评测某个 observation span。
- trace：评测整个 trace。
- session：评测一段会话。

[clean](../../futureagi/tracer/models/observation_span.py#L360) 会校验 target_type 和外键组合：

- span/trace target 需要 `observation_span + trace`，不能有 `trace_session`。
- session target 需要 `trace_session`，不能有 `observation_span + trace`。

这很重要，因为 ClickHouse 分析层会依赖这些外键形态做聚合。如果这里不严格，后续 dashboard 和 trace list 会出现难查的数据错位。

## 6. 语义约定归一

不同 instrumentation 会用不同字段名描述同一件事。例如模型名可能叫 `gen_ai.request.model`、`llm.model_name` 或其他别名。Tracer 用 [AttributeRegistry](../../futureagi/tracer/utils/semantic_conventions.py#L740) 管理这些别名。

核心方法：

- [get_value](../../futureagi/tracer/utils/semantic_conventions.py#L931)：按规范字段读取值，自动尝试别名。
- [detect_convention](../../futureagi/tracer/utils/semantic_conventions.py#L974)：根据属性判断是 OTEL GenAI、OpenInference、OpenLLMetry 等。
- [normalize_span_kind](../../futureagi/tracer/utils/semantic_conventions.py#L997)：把不同来源的 span kind 归一成内部类型。

学习重点是区分：

- `observation_type` 或 span kind：组件是什么，例如 LLM、tool、agent。
- `operation_name`：组件做什么，例如 chat、embedding、speech_to_text。

这个分离让 dashboard 可以同时回答“LLM 总体表现如何”和“chat 操作表现如何”。

## 7. ClickHouse 分析层

ClickHouse schema 在 [schema.py](../../futureagi/tracer/services/clickhouse/schema.py#L67)。整体分层：

1. Layer 1：PeerDB CDC 落地表，镜像 Postgres。
2. Layer 2：dictionary + denormalized spans 宽表。
3. Layer 3：小时级 rollup。
4. Layer 4：dataset analytics。
5. Layer 5：simulation analytics。
6. Layer 6：usage/eval analytics。

核心思想是：Postgres 适合事务和关系，ClickHouse 适合列表、筛选、聚合和 dashboard。Trace list 不应该直接扫 Postgres JSON，也不应该在 ClickHouse 上一次性读所有大字段。

## 8. Trace List 查询为什么分阶段

[TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27) 是性能设计的好例子。

Phase 1 在 [build](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L107)：

- 只查询轻量字段。
- 只取 root span。
- 用时间过滤和 project 过滤缩小范围。
- `LIMIT page_size + 1` 判断是否还有下一页。
- 不读取 input/output/span_attributes 这类重列。

Phase 2 在 [build_content_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L222)：

- 只对当前页 trace_ids 做 PREWHERE 点查。
- 再读取 input/output/metadata 等重列。

Eval 分数在 [build_eval_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L373)：

- 只查当前页 trace_ids。
- 按 trace_id + eval_config_id 聚合。
- 用 `ifNull` 避免 ClickHouse NULL 三值逻辑导致正常行被排除。

最后 [pivot_eval_results](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L610) 把行转成 `{trace_id: {eval_config_id: score}}`，方便 API 合并到列表结果。

## 9. Error Feed

Error Feed 的数据访问层在 [queries/feed.py](../../futureagi/tracer/queries/feed.py#L314)。它的设计比较清晰：query 层返回 typed dataclass，不直接处理 HTTP。

关键入口：

- [list_clusters](../../futureagi/tracer/queries/feed.py#L314)：聚类列表。
- [get_cluster_detail](../../futureagi/tracer/queries/feed.py#L401)：聚类详情。
- [get_overview](../../futureagi/tracer/queries/feed.py#L1406)：概览 tab。
- [get_traces_tab](../../futureagi/tracer/queries/feed.py#L1555)：代表 trace 列表。
- [get_trends_tab](../../futureagi/tracer/queries/feed.py#L1833)：趋势 tab。

Error Feed 的核心价值是把一堆 trace/eval/scanner 结果收敛成“问题模式”。它会聚合趋势、受影响用户、代表 trace、关键证据和修复建议。

## 10. 和其他模块的关系

- Guardrails：命中结果可以形成 guardrail span 或 metadata。
- Agentic Eval：评测结果进入 `EvalLogger`，再进入 trace list/dashboard。
- Simulate：模拟执行和评测结果进入 simulation analytics。
- Model Hub：prompt/version/label 可通过 ClickHouse dictionary 关联到 trace。

## 11. 新增 trace 属性怎么做

建议步骤：

1. 确认属性是不是已有语义字段的别名。
2. 如果是，优先补到 [AttributeRegistry](../../futureagi/tracer/utils/semantic_conventions.py#L740)。
3. 如果需要查询过滤，确认 ClickHouse 宽表是否要 materialize 或 map 化。
4. 如果需要列表展示，补 query builder 和 API merge 逻辑。
5. 如果需要前端筛选，补前端字段配置。

不要直接在 view 里手写复杂 SQL。复杂查询优先放进 query builder 或 queries 层。

## 12. 阅读顺序

推荐顺序：

1. [Trace](../../futureagi/tracer/models/trace.py#L21)：理解根记录。
2. [ObservationSpan](../../futureagi/tracer/models/observation_span.py#L92)：理解 span 结构。
3. [EvalLogger](../../futureagi/tracer/models/observation_span.py#L292)：理解评测结果如何入库。
4. [semantic_conventions.py](../../futureagi/tracer/utils/semantic_conventions.py#L740)：理解属性归一。
5. [schema.py](../../futureagi/tracer/services/clickhouse/schema.py#L67)：理解分析层分层。
6. [TraceListQueryBuilder](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L27)：学习高性能列表查询。
7. [feed.py](../../futureagi/tracer/queries/feed.py#L314)：学习复杂分析接口如何拆数据访问层。

## 13. 常见排错方向

Trace 列表查不到数据：

- 检查 project_id/project_version_id 过滤。
- 检查时间窗口：`start_time` 是用户语义，`created_at` 用于分区裁剪。
- 检查 root span 条件 `(parent_span_id IS NULL OR parent_span_id = '')`。

Eval 列为空：

- 看 `EvalLogger` 是否有正确 trace_id 和 custom_eval_config_id。
- 看 [build_eval_query](../../futureagi/tracer/services/clickhouse/query_builders/trace_list.py#L373) 是否收到 eval_config_ids。
- 看 output_str 为 NULL 时是否被错误过滤。

ClickHouse 查询慢：

- 检查是否在 Phase 1 读取了 input/output 等重列。
- 检查时间过滤是否能裁剪分区。
- 检查是否绕过了 query builder 直接写大 SQL。

Error Feed 数据不准：

- 检查 cluster 和 trace 的关联是否完整。
- 检查 EvalLogger/scan result 是否被正确聚合。
- 检查代表 trace 是否有 root span、input/output 和 eval score。
