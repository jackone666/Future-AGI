# Agentic Eval 后端源码学习手册

Agentic Eval 是 Future AGI 的评测执行层。它负责把输入数据、评测器、LLM 调用、指标结果、Fi 平台日志和批量运行串起来。它既能离线评测数据集，也能作为 guard/online check 的一部分被调用。

## 1. 模块定位

如果说 Guardrails 负责“请求当下能不能放行”，Agentic Eval 更关注“回答质量如何判断”。它可以用 LLM-as-judge、规则、启发式算法或模型服务给结果打分，并把结果统一成 `EvalResult`。

核心职责：

- 定义 evaluator 的统一接口。
- 校验评测输入字段。
- 支持单条评测、guard 模式和批量评测。
- 支持并发执行并保持结果顺序。
- 将数据集和评测结果写回 Fi 平台。
- 封装多 provider LLM 调用，为 evaluator 或 prompt run 复用。

## 2. 源码入口地图

| 代码 | 作用 |
|---|---|
| [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25) | 所有 evaluator 的抽象基类。 |
| [BaseEvaluator.run](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L132) | 单次运行入口，记录 usage 并返回 `BatchRunResult`。 |
| [BaseEvaluator.guard](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L151) | guard 模式入口，把 `EvalResult.failure` 转成 passed。 |
| [BaseEvaluator.run_batch](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L240) | 批量评测入口，支持并发和 Fi 上传。 |
| [EvalResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37) | evaluator 标准输出结构。 |
| [BatchRunResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L59) | 批量评测返回结构，可转 DataFrame。 |
| [LLM](../../futureagi/agentic_eval/core/llm/llm.py#L171) | 多 provider LLM 封装。 |
| [LLM._prepare_completion_payload](../../futureagi/agentic_eval/core/llm/llm.py#L1211) | 构造 provider completion payload。 |
| [RunPrompt](../../futureagi/agentic_eval/core_evals/run_prompt/litellm_response.py#L92) | 前端/数据库驱动的 prompt 执行器。 |
| [RunPrompt.litellm_response](../../futureagi/agentic_eval/core_evals/run_prompt/litellm_response.py#L3303) | LiteLLM 同步调用主入口。 |

## 3. BaseEvaluator 抽象

[BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25) 规定一个 evaluator 必须提供这些内容：

- `name`：唯一标识，用于日志、配置和 usage。
- `display_name`：展示名称。
- `metric_ids`：会产出的指标 ID。
- `required_args`：运行需要哪些输入字段。
- `examples`：示例。
- `is_failure()`：如何判断失败。
- `_evaluate()`：真正执行评测。
- `to_config()`：可选，把 evaluator 配置表达成可存储结构。

学习时重点看 `_evaluate()` 和 `required_args` 的关系。`required_args` 是 evaluator 的输入契约，调用方应该通过它知道必须传什么；`_evaluate()` 不应该偷偷依赖未声明字段。

## 4. EvalResult 数据结构

标准结果在 [EvalResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37)：

- `name`：评测器 ID。
- `display_name`：展示名称。
- `data`：原始输入或 evaluator 想保留的结构化数据。
- `failure`：是否失败，guard 模式最依赖它。
- `reason`：解释文本。
- `runtime`：运行耗时。
- `model`：使用的 judge/model。
- `metrics`：可以聚合的数值指标。
- `datapoint_field_annotations`：字段级解释。

[BatchRunResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L59) 是批量结果容器。它保留 `eval_results` 和可选的 `eval_request_id`，并提供 `to_df()` 方便离线分析。

## 5. 单次评测链路

单次运行入口是 [run](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L132)：

1. 打日志，记录 evaluator 名称和输入字段。
2. 通过 `FiApiService.log_usage()` 记录使用量。
3. 调用 `_log_evaluation_request()` 创建评测请求日志。
4. 调用 `_evaluate(**kwargs)` 执行实际评测。
5. 包装成 `BatchRunResult` 返回。

注意：`run()` 返回的是 `BatchRunResult`，即使只跑一条也会包装成列表。这让调用方可以用同一种结果处理方式兼容单条和批量。

## 6. Guard 模式

[guard](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L151) 是简化接口：

```text
_evaluate()
  -> EvalResult.failure
  -> GuardResult.passed = not failure
```

这适合把 evaluator 接到在线检查或 guardrail 场景。它只关心是否通过、原因和运行时间，不关心完整的批量日志结构。

## 7. 批量评测链路

[run_batch](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L240) 是批量入口：

1. 记录 usage。
2. 如果 `max_parallel_evals > 1`，走 [_run_batch_generator_async](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L161)。
3. 否则顺序调用 `_run_batch_generator()`。
4. 如果 `upload_to_fi=True`，创建 Fi dataset。
5. 将 eval results 和 config 写回 Fi。
6. 返回 `BatchRunResult`。

并发执行有两个学习重点：

- 它用 `ThreadPoolExecutor`，并通过 `future_to_index` 记录原始索引。
- 结果数组预先按输入长度创建，future 完成后放回原始位置，所以并发不会打乱输出顺序。

## 8. LLM 封装

[LLM](../../futureagi/agentic_eval/core/llm/llm.py#L171) 是更底层的 provider 抽象。它处理：

- OpenAI、Anthropic、Groq、Vertex AI、Bedrock 等 provider。
- token usage 和 cost 更新。
- 同步/异步 completion。
- gateway completion。
- 工具调用。
- 音频转写。
- provider fallback。

复杂 provider 调用一般会落到 [_prepare_completion_payload](../../futureagi/agentic_eval/core/llm/llm.py#L1211) 先构造 payload，再由 provider-specific handler 执行。fallback 逻辑可以看 [_handle_final_fallback](../../futureagi/agentic_eval/core/llm/llm.py#L1576) 和异步版本。

## 9. RunPrompt 执行器

[RunPrompt](../../futureagi/agentic_eval/core_evals/run_prompt/litellm_response.py#L92) 更贴近产品侧的“运行一个 prompt”。它要兼容前端、数据库和 LiteLLM 传来的多种参数形态，所以文件比较长。

重点阅读：

- 初始化参数归一：看 `__init__` 如何保存 model、messages、tools、stream、reasoning 等字段。
- payload 构造：看 `_create_payload`。
- 普通响应：看 `_regular_response`。
- 流式响应：看 `_streaming_response`。
- 对外入口：看 [litellm_response](../../futureagi/agentic_eval/core_evals/run_prompt/litellm_response.py#L3303)。

这个文件很大，学习时不要从头逐行读。先抓入口，再追一条具体模式，例如“非流式 chat completion”。

## 10. 和其他模块的关系

- Simulate 会触发评测，把 call/transcript/agent output 交给 evaluator。
- Tracer 用 `EvalLogger` 保存 eval 结果，并在 trace list/dashboard 中展示。
- Guardrails 可以调用 evaluator 风格的检查，把评测结果变成在线通过/阻断。
- Model Hub/Prompt 相关功能会通过 `RunPrompt` 运行 prompt 并收集结果。

## 11. 新增一个 Evaluator 怎么做

建议步骤：

1. 新建 evaluator 类，继承 [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25)。
2. 定义唯一 `name`，不要和已有 evaluator 冲突。
3. 定义 `display_name` 和 `metric_ids`。
4. 把所有必需输入写进 `required_args`。
5. 在 `_evaluate()` 中返回 [EvalResult](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37)。
6. 用 `failure` 表达是否失败，用 `reason` 表达可读解释。
7. 如果要进入 Fi 平台，补 `to_config()`。
8. 写测试覆盖参数缺失、正常通过、失败、异常和批量顺序。

## 12. 阅读顺序

推荐顺序：

1. [evals_result.py](../../futureagi/agentic_eval/core_evals/fi_utils/evals_result.py#L37)：先理解标准结果。
2. [BaseEvaluator](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L25)：理解 evaluator 生命周期。
3. 找一个具体 evaluator，对照 `_evaluate()` 的返回结构。
4. [run_batch](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L240)：理解批量和 Fi 上传。
5. [LLM](../../futureagi/agentic_eval/core/llm/llm.py#L171)：理解 provider 调用封装。
6. [RunPrompt](../../futureagi/agentic_eval/core_evals/run_prompt/litellm_response.py#L92)：理解产品侧 prompt 执行链路。

## 13. 常见排错方向

评测没有输出：

- 检查 `required_args` 是否和传入字段一致。
- 检查 `_evaluate()` 是否抛异常，批量模式中异常会变成 `None`。
- 检查 LLM provider key 和 payload 是否正确。

批量结果顺序错乱：

- 看 [_run_batch_generator_async](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L161) 是否仍按 index 回填。

结果没有进入平台：

- 检查 `upload_to_fi` 是否为 true。
- 检查 `_log_dataset_to_Fi()` 是否创建 dataset 成功。
- 检查 `_log_eval_results_to_Fi()` 是否有异常日志。

guard 模式判断反了：

- 看 evaluator 返回的 `failure` 语义。
- [guard](../../futureagi/agentic_eval/core_evals/fi_evals/base_evaluator.py#L151) 中 `passed = not eval_result["failure"]`，所以 failure 必须表示“评测失败/不合格”。
