# Simulate 后端源码学习手册

Simulate 模块负责把 agent 放进可控场景中运行：文本聊天、语音通话、persona、dataset rows、场景脚本、评测摘要和优化任务都在这里汇合。它的学习重点是“测试定义如何变成一次执行，再如何展开成多个 call execution”。

## 1. 模块定位

Simulate 解决的是上线前和回归时的测试问题。它不是简单调用一次 prompt，而是批量生成或执行真实场景，让 agent 在多轮上下文、不同 persona、工具调用和语音 provider 中暴露问题。

核心职责：

- 管理测试定义 `RunTest`。
- 创建一次执行 `TestExecution`。
- 将场景展开为多个 `CallExecution`。
- 通过 Temporal 编排大规模执行。
- 处理 provider call、transcript、录音、日志和成本。
- 调用 evaluator 生成评测结果和摘要。

## 2. 源码入口地图

| 代码 | 作用 |
|---|---|
| [RunTest](../../futureagi/simulate/models/run_test.py#L16) | 测试定义：要测哪个 agent/prompt、哪些 scenario、哪些 dataset row。 |
| [TestExecution](../../futureagi/simulate/models/test_execution.py#L24) | 一次实际执行：整体状态、统计、评测摘要状态。 |
| [CallExecution](../../futureagi/simulate/models/test_execution.py#L184) | 单个场景/通话/聊天模拟的执行记录。 |
| [duration_seconds](../../futureagi/simulate/models/test_execution.py#L168) | 聚合所有 call 的持续时间。 |
| [success_rate](../../futureagi/simulate/models/test_execution.py#L177) | 从 completed/total 计算整体成功率。 |
| [TestExecutionWorkflow](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L65) | Temporal 父 workflow，编排一次测试执行。 |
| [workflow.run](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L102) | 父 workflow 主入口。 |
| [call_completed signal](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L336) | 子 workflow 完成时回传进度。 |
| [call_analyzing signal](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L351) | 子 workflow 进入分析状态时回传进度。 |
| [_launch_batch](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L381) | 分批启动子 workflow。 |
| [TestExecutor](../../futureagi/simulate/services/test_executor.py#L131) | 遗留执行器，仍保留大量业务规则。 |
| [execute_test](../../futureagi/simulate/services/test_executor.py#L460) | 遗留执行入口。 |
| [_run_simulate_evaluations](../../futureagi/simulate/services/test_executor.py#L4072) | 执行模拟评测。 |
| [_run_single_simulate_evaluation](../../futureagi/simulate/services/test_executor.py#L4641) | 单条 call 的评测执行。 |

## 3. 数据模型关系

三个模型最关键：

```text
RunTest
  -> TestExecution
    -> CallExecution
```

[RunTest](../../futureagi/simulate/models/run_test.py#L16) 是定义层。它描述：

- 测试名称和描述。
- 目标 agent definition 或 prompt。
- agent version。
- scenarios。
- dataset row ids。
- simulator agent。
- 是否启用工具评测。

[TestExecution](../../futureagi/simulate/models/test_execution.py#L24) 是执行层。它描述：

- 当前状态：pending/running/completed/failed/cancelled/evaluating。
- 开始和完成时间。
- total/completed/failed call 数。
- 本次执行实际使用的 simulator、agent definition、agent version。
- eval explanation summary 和状态。

[CallExecution](../../futureagi/simulate/models/test_execution.py#L184) 是最细粒度的结果层。它保存：

- 所属 test execution 和 scenario。
- voice/text 类型。
- provider call id。
- 状态、开始/结束时间、耗时。
- recording/transcript/logs。
- provider 原始数据。
- monitor 数据。
- 成本拆分。
- 错误原因。

## 4. Temporal 执行链路

新执行路径的核心是 [TestExecutionWorkflow](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L65)。

主流程在 [run](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L102)：

1. 保存 test_execution_id 和 org_id。
2. 如果是 continue-as-new 恢复，先恢复 checkpoint state。
3. 阶段 1 初始化：调用 `setup_test_execution` 校验配置。
4. 创建 call execution records。
5. 阶段 2 启动：按批次启动子 `CallExecutionWorkflow`。
6. 阶段 3 等待：通过 signal 等待 call 进入 analyzing/completed/failed。
7. 阶段 4 收尾：finalize test execution，触发后处理。

这个 workflow 的关键设计点是：父 workflow 不轮询所有子任务，而是让子 workflow 通过 signal 回传状态。

## 5. Signal 和进度统计

子 workflow 完成时调用 [call_completed](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L336)：

- 如果失败，增加 `_failed_calls`。
- 否则增加 `_completed_calls`。
- 写入进度日志。

子 workflow 进入分析阶段时调用 [call_analyzing](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L351)：

- 增加 `_analyzing_calls`。
- 用于判断语音执行阶段是否全部结束。

[_all_analyzing](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L426) 有一个重要细节：失败 call 也要算进去。因为有些 call 在余额检查、准备或发起阶段就失败，不会进入 analyzing。如果不把 failed_calls 算入，父 workflow 会一直等。

## 6. 分批启动和 continue-as-new

[_launch_batch](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L381) 会按小批次启动子 workflow。这样是为了避免大量 call 同时打到 LiveKit agent worker，造成瞬时 503。

大规模测试还要控制 Temporal history 大小。[_checkpoint](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L527) 会保存当前计数状态，然后 `continue_as_new`。这相当于用新的 workflow history 继续跑，但业务状态不丢。

## 7. 遗留 TestExecutor 怎么看

[TestExecutor](../../futureagi/simulate/services/test_executor.py#L131) 被标记为遗留执行器，但它仍然有很高学习价值，因为很多业务细节还在里面：

- [execute_test](../../futureagi/simulate/services/test_executor.py#L460)：老执行入口。
- `_validate_test_ready`：检查测试是否可执行。
- `_execute_scenario_with_calls`：把 scenario 展开成 call。
- `_execute_call`：具体发起 call。
- `_fetch_and_store_transcript`：抓取 transcript。
- `_store_complete_call_data`：存 provider 完整数据。
- [_run_simulate_evaluations](../../futureagi/simulate/services/test_executor.py#L4072)：执行评测。
- [_run_single_simulate_evaluation](../../futureagi/simulate/services/test_executor.py#L4641)：单条评测。

新增执行编排优先走 Temporal，但理解业务规则时可以读 TestExecutor。把它当业务字典，不要把它当新代码范式。

## 8. 评测链路

Simulate 的评测链路大致是：

```text
CallExecution 完成
  -> 读取 transcript / provider data
  -> 构造 evaluator 输入
  -> 调用 Agentic Eval
  -> 保存 EvalLogger / summary
  -> 更新 TestExecution 聚合状态
```

重点代码：

- [_run_simulate_evaluations](../../futureagi/simulate/services/test_executor.py#L4072) 负责批量调度。
- [_run_single_simulate_evaluation](../../futureagi/simulate/services/test_executor.py#L4641) 负责单条 call 的评测。

评测结果后续会被 Tracer 的 `EvalLogger` 或分析查询读取，用于 dashboard、trace list 和错误分析。

## 9. 和其他模块的关系

- 和 Agentic Eval：Simulate 调 evaluator 判断每条 call 是否合格。
- 和 Tracer：call 执行、评测结果和错误可以进入 trace/eval 分析层。
- 和 Guardrails：模拟场景可以覆盖 prompt injection、jailbreak、PII 等防护场景。
- 和 Model Hub：当测试源是 prompt 时，RunTest 会关联 prompt template/version。

## 10. 新增模拟能力怎么做

新增一种执行 provider 或模拟方式，建议：

1. 先确认它应该是 voice 还是 text/chat。
2. 在模型层只补必要字段，不要把 provider 逻辑塞进 model。
3. 新增 provider client/service，封装凭据、发起、状态查询和日志抓取。
4. Temporal 路径下新增 activity，并在 workflow 中编排。
5. 评测输入保持和 Agentic Eval 的 `required_args` 对齐。
6. 给失败状态、取消状态、余额不足和 provider 异常写测试。

## 11. 阅读顺序

推荐顺序：

1. [RunTest](../../futureagi/simulate/models/run_test.py#L16)：理解测试定义。
2. [TestExecution](../../futureagi/simulate/models/test_execution.py#L24)：理解一次运行。
3. [CallExecution](../../futureagi/simulate/models/test_execution.py#L184)：理解单个场景结果。
4. [TestExecutionWorkflow](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L65)：理解新编排方式。
5. [_launch_batch](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L381)：理解大规模执行。
6. [TestExecutor](../../futureagi/simulate/services/test_executor.py#L131)：补业务细节。
7. [_run_simulate_evaluations](../../futureagi/simulate/services/test_executor.py#L4072)：理解评测接入。

## 12. 常见排错方向

测试一直 pending：

- 检查 `setup_test_execution` activity 是否失败。
- 检查 call records 是否创建成功。
- 检查 Temporal worker 是否在正确 task queue 上运行。

测试卡在 running：

- 看子 workflow 是否发送了 `call_completed` signal。
- 看失败 call 是否被计入 [_all_analyzing](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L426)。
- 看 provider call 是否一直没有终态。

评测摘要没有生成：

- 看 call 是否有 transcript 或必要输入。
- 看 [_run_simulate_evaluations](../../futureagi/simulate/services/test_executor.py#L4072) 是否被触发。
- 看 evaluator `required_args` 是否和构造输入一致。

大批量执行不稳定：

- 看 [_launch_batch](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L381) 的分批大小和延迟。
- 看 Temporal history 是否触发 [_checkpoint](../../futureagi/simulate/temporal/workflows/test_execution_workflow.py#L527)。
- 看 provider/LiveKit worker 是否有瞬时 503。
