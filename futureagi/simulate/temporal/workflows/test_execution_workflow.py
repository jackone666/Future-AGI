"""
TestExecutionWorkflow - 测试执行的父级编排器。

管理一次测试执行的完整生命周期：
1. 初始化与校验
2. 创建 CallExecution 记录
3. 启动子级 CallExecutionWorkflow
4. 通过 signal 跟踪进度
5. 汇总状态与计数并结束

设计要点：
- 可启动 1000+ 个子 workflow
- 基于 signal 跟踪进度（子 workflow 完成后发 signal）
- 大规模测试通过 continue-as-new 控制事件历史
- 支持优雅取消
"""

import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.workflow import ParentClosePolicy

from simulate.temporal.constants import (
    CALL_EXECUTION_WORKFLOW_ID_PREFIX,
    CONTINUE_AS_NEW_THRESHOLD,
    LAUNCH_BATCH_SIZE,
    LAUNCH_SUB_BATCH_DELAY_SECONDS,
    LAUNCH_SUB_BATCH_SIZE,
    QUEUE_L,
    QUEUE_S,
)
from simulate.temporal.retry_policies import DB_RETRY_POLICY
from simulate.temporal.signals import SIGNAL_CALL_ANALYZING, SIGNAL_CALL_COMPLETED
from simulate.temporal.types.activities import (
    CancelPendingCallsInput,
    CancelPendingCallsOutput,
    CreateCallRecordsInput,
    CreateCallRecordsOutput,
    FinalizeInput,
    GetUnlaunchedCallsInput,
    GetUnlaunchedCallsOutput,
    ReportErrorInput,
    SetupTestInput,
    SetupTestOutput,
)
from simulate.temporal.types.call_execution import CallExecutionInput
from simulate.temporal.types.test_execution import (
    CallAnalyzingSignal,
    CallCompletedSignal,
    TestExecutionInput,
    TestExecutionOutput,
    TestExecutionState,
    TestExecutionStatus,
)

# 通过 sandbox passthrough 导入 Django model，以访问状态枚举。
with workflow.unsafe.imports_passed_through():
    from simulate.models.test_execution import TestExecution as TestExecutionModel


@workflow.defn
class TestExecutionWorkflow:
    """
    测试执行的父级编排器。

    负责启动并跟踪 CallExecutionWorkflow 子 workflow。
    使用基于 signal 的协作方式跟踪进度。

    阶段：
    1. INITIALIZING：初始化、校验配置、创建 call 记录
    2. LAUNCHING：批量启动子 workflow
    3. RUNNING：等待子 workflow 完成（通过 signal）
    4. FINALIZING：更新状态并触发后处理
    """

    def __init__(self):
        self._status = "PENDING"
        self._test_execution_id: Optional[str] = None
        self._org_id: Optional[str] = None
        self._workspace_id: Optional[str] = None

        # 进度跟踪。
        self._total_calls = 0
        self._launched_calls = 0
        self._completed_calls = 0
        self._failed_calls = 0
        self._analyzing_calls = 0  # 已进入 ANALYZING 状态的 call 数量。

        # 子 workflow 跟踪。
        self._pending_call_ids: list[str] = []

        # 取消状态。
        self._cancelled = False

        # 用于 continue-as-new 的事件计数。
        self._event_count = 0

    @workflow.run
    async def run(self, input: TestExecutionInput) -> TestExecutionOutput:
        """主 workflow 执行入口。"""
        self._test_execution_id = input.test_execution_id
        self._org_id = input.org_id

        # 如果从 checkpoint 继续，则恢复状态。
        if input.state:
            self._restore_state(input.state)
            workflow.logger.info(
                f"Restored from checkpoint: launched={self._launched_calls}, "
                f"completed={self._completed_calls}, failed={self._failed_calls}"
            )

        try:
            # ========================================
            # 阶段 1：初始化。continue-as-new 恢复时会跳过，避免重复创建 call。
            # ========================================
            if not input.state:
                self._status = "INITIALIZING"

                # 初始化并校验。
                setup_result = await workflow.execute_activity(
                    "setup_test_execution",
                    SetupTestInput(
                        test_execution_id=input.test_execution_id,
                        run_test_id=input.run_test_id,
                        scenario_ids=input.scenario_ids,
                        simulator_id=input.simulator_id,
                    ),
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=DB_RETRY_POLICY,
                    task_queue=QUEUE_L,
                    result_type=SetupTestOutput,
                )

                if not setup_result.success:
                    return await self._fail(
                        input, f"Setup failed: {setup_result.error}"
                    )

                # 保存 setup 结果中的 workspace_id（来自 run_test.workspace）。
                self._workspace_id = setup_result.workspace_id

                # 创建 call 记录。
                create_result = await workflow.execute_activity(
                    "create_call_execution_records",
                    CreateCallRecordsInput(
                        test_execution_id=input.test_execution_id,
                        scenarios=setup_result.scenarios,
                        simulator_agent=setup_result.simulator_agent,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=DB_RETRY_POLICY,
                    task_queue=QUEUE_L,
                    result_type=CreateCallRecordsOutput,
                )

                if create_result.error:
                    return await self._fail(
                        input, f"Failed to create calls: {create_result.error}"
                    )

                self._total_calls = create_result.total_created
                self._pending_call_ids = create_result.call_ids

                workflow.logger.info(f"Created {self._total_calls} call records")

            # ========================================
            # 阶段 2：批量启动子 workflow。
            # ========================================
            self._status = "LAUNCHING"

            # 如果是恢复执行，则从数据库读取尚未启动的 call。
            if input.state and not self._pending_call_ids:
                unlaunched = await workflow.execute_activity(
                    "get_unlaunched_call_ids",
                    GetUnlaunchedCallsInput(test_execution_id=input.test_execution_id),
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=DB_RETRY_POLICY,
                    task_queue=QUEUE_L,
                    result_type=GetUnlaunchedCallsOutput,
                )
                self._pending_call_ids = unlaunched.call_ids

            # 批量启动子 workflow。
            while self._pending_call_ids:
                # 检查是否需要 continue-as-new。
                if self._event_count >= CONTINUE_AS_NEW_THRESHOLD:
                    return await self._checkpoint(input)

                # 获取下一批。
                batch = self._pending_call_ids[:LAUNCH_BATCH_SIZE]
                self._pending_call_ids = self._pending_call_ids[LAUNCH_BATCH_SIZE:]

                # 启动当前批次。
                await self._launch_batch(input, batch)

                self._launched_calls += len(batch)
                self._event_count += len(batch)

            # ========================================
            # 阶段 3：运行中，等待所有 call 进入 ANALYZING。
            # ========================================
            self._status = "RUNNING"

            # 等待所有 call 进入 ANALYZING 状态（呼叫完成，正在处理结果）。
            while not self._all_analyzing():
                # 检查是否需要 continue-as-new。
                if self._event_count >= CONTINUE_AS_NEW_THRESHOLD:
                    return await self._checkpoint(input)

                # 定期更新数据库中的进度；这是非关键步骤，失败不应导致 workflow 失败。
                try:
                    await workflow.execute_activity(
                        "update_test_execution_counts",
                        args=[
                            input.test_execution_id,
                            self._completed_calls,
                            self._failed_calls,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=DB_RETRY_POLICY,
                        task_queue=QUEUE_S,
                    )
                except Exception as e:
                    workflow.logger.warning(f"Failed to update progress counts: {e}")

                # 等待 signal 或超时；使用 workflow.sleep 保持 Temporal 确定性。
                await workflow.sleep(10)
                self._event_count += 1

            # ========================================
            # 阶段 4：评测中；所有 call 已进入分析阶段，继续等待 eval 完成。
            # ========================================
            # 注意：最后一个 call 进入 ANALYZING 时，DB 状态会由
            # update_call_status activity 更新为 EVALUATING。
            self._status = TestExecutionModel.ExecutionStatus.EVALUATING

            workflow.logger.info(
                f"All calls analyzing, transitioning to EVALUATING: "
                f"analyzing={self._analyzing_calls}, total={self._total_calls}"
            )

            # 等待所有 call 完全结束，也就是 eval 已完成。
            while not self._is_complete():
                # 检查是否需要 continue-as-new。
                if self._event_count >= CONTINUE_AS_NEW_THRESHOLD:
                    return await self._checkpoint(input)

                # 定期把进度写回数据库。
                try:
                    await workflow.execute_activity(
                        "update_test_execution_counts",
                        args=[
                            input.test_execution_id,
                            self._completed_calls,
                            self._failed_calls,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=DB_RETRY_POLICY,
                        task_queue=QUEUE_S,
                    )
                except Exception as e:
                    workflow.logger.warning(f"Failed to update progress counts: {e}")

                # 等待 signal 或超时。
                await workflow.sleep(10)
                self._event_count += 1

            # ========================================
            # 阶段 5：最终收尾。
            # ========================================
            self._status = "FINALIZING"

            final_status = TestExecutionModel.ExecutionStatus.COMPLETED
            if self._failed_calls > 0 and self._completed_calls == 0:
                final_status = TestExecutionModel.ExecutionStatus.FAILED

            await workflow.execute_activity(
                "finalize_test_execution",
                FinalizeInput(
                    test_execution_id=input.test_execution_id,
                    status=final_status,
                    completed_calls=self._completed_calls,
                    failed_calls=self._failed_calls,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY_POLICY,
                task_queue=QUEUE_L,
            )

            self._status = final_status

            return TestExecutionOutput(
                status=final_status,
                total_calls=self._total_calls,
                completed_calls=self._completed_calls,
                failed_calls=self._failed_calls,
            )

        except asyncio.CancelledError:
            # 处理 Temporal 的外部取消（例如通过 handle.cancel() 触发）。
            workflow.logger.info(
                f"TestExecutionWorkflow cancelled via handle.cancel(): {input.test_execution_id}"
            )
            return await self._handle_cancellation(input)

        except Exception as e:
            workflow.logger.warning(f"TestExecutionWorkflow failed: {str(e)}")
            # 通过 activity 上报 workflow 错误；这里不等待完成，避免错误上报本身阻塞失败收敛。
            workflow.start_activity(
                "report_workflow_error",
                ReportErrorInput(
                    workflow_name="TestExecutionWorkflow",
                    workflow_id=workflow.info().workflow_id,
                    error_message=str(e),
                    error_type=type(e).__name__,
                    context={
                        "test_execution_id": input.test_execution_id,
                        "run_test_id": input.run_test_id,
                    },
                ),
                start_to_close_timeout=timedelta(seconds=10),
                task_queue=QUEUE_S,
            )
            return await self._fail(input, str(e))

    # ========================================
    # Signal 处理器：子 workflow 用 signal 回传状态，父 workflow 不轮询数据库。
    # ========================================

    @workflow.signal
    async def call_completed(self, signal: CallCompletedSignal) -> None:
        """子 CallExecutionWorkflow 完成时发送的 signal。"""
        self._event_count += 1

        if signal.failed:
            self._failed_calls += 1
        else:
            self._completed_calls += 1

        workflow.logger.info(
            f"Call completed: {signal.call_id}, status={signal.status}, failed={signal.failed}, "
            f"progress={self._completed_calls + self._failed_calls}/{self._total_calls}"
        )

    @workflow.signal
    async def call_analyzing(self, signal: CallAnalyzingSignal) -> None:
        """子 CallExecutionWorkflow 进入 ANALYZING 状态时发送的 signal。"""
        self._event_count += 1
        self._analyzing_calls += 1

        workflow.logger.info(
            f"Call analyzing: {signal.call_id}, "
            f"analyzing_progress={self._analyzing_calls}/{self._total_calls}"
        )

    # ========================================
    # Query：外部可以读取 workflow 内存状态，不改变 workflow 历史。
    # ========================================

    @workflow.query
    def get_status(self) -> TestExecutionStatus:
        """查询当前 workflow 状态。"""
        return TestExecutionStatus(
            status=self._status,
            total_calls=self._total_calls,
            completed_calls=self._completed_calls,
            failed_calls=self._failed_calls,
            launched_calls=self._launched_calls,
            analyzing_calls=self._analyzing_calls,
        )

    # ========================================
    # 辅助方法。
    # ========================================

    async def _launch_batch(
        self, input: TestExecutionInput, call_ids: list[str]
    ) -> None:
        """批量启动 CallExecutionWorkflow 子 workflow。

        按 LAUNCH_SUB_BATCH_SIZE 分小批启动，并在批次之间短暂等待。
        这样可以避免大量 call 同时启动时把 LiveKit agent worker 打出
        thundering-herd/503。
        """
        try:
            from ee.voice.temporal.workflows.call_execution_workflow import (
                CallExecutionWorkflow,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Voice call execution workflow is unavailable without Enterprise Edition."
            ) from exc

        for i, call_id in enumerate(call_ids):
            # 错峰启动：批次之间暂停，让 agent worker 有时间接收 dispatch。
            if i > 0 and i % LAUNCH_SUB_BATCH_SIZE == 0:
                await workflow.sleep(LAUNCH_SUB_BATCH_DELAY_SECONDS)

            workflow_id = f"{CALL_EXECUTION_WORKFLOW_ID_PREFIX}-{call_id}"

            await workflow.start_child_workflow(
                CallExecutionWorkflow.run,
                CallExecutionInput(
                    call_id=call_id,
                    org_id=input.org_id,
                    workspace_id=self._workspace_id or "",
                    test_workflow_id=workflow.info().workflow_id,
                    test_execution_id=input.test_execution_id,
                ),
                id=workflow_id,
                task_queue=QUEUE_L,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                # ABANDON 允许父 workflow continue-as-new 后，子 workflow 继续运行。
                parent_close_policy=ParentClosePolicy.ABANDON,
            )

    def _is_complete(self) -> bool:
        """检查所有 call 是否已结束。"""
        return (self._completed_calls + self._failed_calls) >= self._total_calls

    def _all_analyzing(self) -> bool:
        """检查所有 call 是否已经完成语音阶段并进入分析/完成/失败状态。"""
        # 对父 workflow 来说，analyzing/completed/failed 都表示“语音执行阶段结束”。
        # 有些 call 在余额检查、准备或发起阶段就失败，不会发送 call_analyzing signal；
        # 因此必须把 failed_calls 也算进去，否则父 workflow 会永久等待。
        return (self._analyzing_calls + self._failed_calls) >= self._total_calls

    async def _fail(self, input: TestExecutionInput, error: str) -> TestExecutionOutput:
        """将 workflow 标记为失败，并同步更新数据库状态。"""
        self._status = TestExecutionModel.ExecutionStatus.FAILED

        # 将失败状态写回数据库。
        try:
            await workflow.execute_activity(
                "finalize_test_execution",
                FinalizeInput(
                    test_execution_id=input.test_execution_id,
                    status=TestExecutionModel.ExecutionStatus.FAILED,
                    completed_calls=self._completed_calls,
                    failed_calls=self._failed_calls,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY_POLICY,
                task_queue=QUEUE_L,
            )
        except Exception as e:
            workflow.logger.warning(
                f"Failed to update TestExecution status to FAILED: {str(e)}"
            )

        return TestExecutionOutput(
            status=TestExecutionModel.ExecutionStatus.FAILED,
            total_calls=self._total_calls,
            completed_calls=self._completed_calls,
            failed_calls=self._failed_calls,
            error=error,
        )

    async def _handle_cancellation(
        self, input: TestExecutionInput
    ) -> TestExecutionOutput:
        """处理 workflow 取消（来自 handle.cancel()）。

        在 Python Temporal SDK 中，一旦捕获 CancelledError，workflow 可以正常
        运行清理 activity，不需要 shielding scope。CancellationScope 是 Go SDK
        概念，Python SDK 中不可用。
        """
        self._status = TestExecutionModel.ExecutionStatus.CANCELLED
        self._cancelled = True

        # 取消所有 pending/ongoing 子 workflow，并释放执行槽位。
        try:
            await workflow.execute_activity(
                "cancel_pending_calls",
                CancelPendingCallsInput(
                    test_execution_id=input.test_execution_id,
                    reason="Cancelled by user",
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY_POLICY,
                task_queue=QUEUE_L,
                result_type=CancelPendingCallsOutput,
            )
        except Exception as e:
            workflow.logger.warning(f"Failed to cancel pending calls: {str(e)}")

        # 将取消状态写回数据库。
        try:
            await workflow.execute_activity(
                "finalize_test_execution",
                FinalizeInput(
                    test_execution_id=input.test_execution_id,
                    status=TestExecutionModel.ExecutionStatus.CANCELLED,
                    completed_calls=self._completed_calls,
                    failed_calls=self._failed_calls,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY_POLICY,
                task_queue=QUEUE_L,
            )
        except Exception as e:
            workflow.logger.warning(
                f"Failed to update TestExecution status to CANCELLED: {str(e)}"
            )

        return TestExecutionOutput(
            status=TestExecutionModel.ExecutionStatus.CANCELLED,
            total_calls=self._total_calls,
            completed_calls=self._completed_calls,
            failed_calls=self._failed_calls,
        )

    def _restore_state(self, state: TestExecutionState) -> None:
        """从 continue-as-new checkpoint 恢复内存状态。"""
        self._status = state.status
        self._total_calls = state.total_calls
        self._completed_calls = state.completed_calls
        self._failed_calls = state.failed_calls
        self._launched_calls = state.launched_calls
        self._analyzing_calls = state.analyzing_calls

    async def _checkpoint(self, input: TestExecutionInput) -> TestExecutionOutput:
        """保存 checkpoint 并触发 continue-as-new。"""
        workflow.logger.info(
            f"Checkpointing: events={self._event_count}, "
            f"completed={self._completed_calls}, launched={self._launched_calls}"
        )

        state = TestExecutionState(
            status=self._status,
            total_calls=self._total_calls,
            completed_calls=self._completed_calls,
            failed_calls=self._failed_calls,
            launched_calls=self._launched_calls,
            analyzing_calls=self._analyzing_calls,
        )

        # 带着已保存状态继续为新的 workflow history。
        workflow.continue_as_new(
            TestExecutionInput(
                test_execution_id=input.test_execution_id,
                run_test_id=input.run_test_id,
                org_id=input.org_id,
                scenario_ids=input.scenario_ids,
                simulator_id=input.simulator_id,
                state=state,
            )
        )

        # 理论上不会执行到这里；保留返回值是为了满足类型检查器。
        return TestExecutionOutput(status="CHECKPOINT")
