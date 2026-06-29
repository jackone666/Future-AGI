import uuid

from django.db import models  # type: ignore[import-not-found]
from django.utils import timezone  # type: ignore[import-not-found]

from simulate.models.agent_optimiser import AgentOptimiser
from simulate.models.agent_version import AgentVersion
from simulate.models.run_test import RunTest
from simulate.semantics import validate_provider_sent_objects
from tfc.utils.base_model import BaseModel, Deprecated

from .agent_definition import AgentDefinition, AgentTypeChoices
from .scenarios import Scenarios
from .simulator_agent import SimulatorAgent


class EvalExplanationSummaryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class TestExecution(BaseModel):
    """
    一次 RunTest 的实际执行记录。

    RunTest 是测试定义，TestExecution 是一次运行实例。这里保存整体状态、
    call 统计、实际使用的 agent/simulator 版本，以及评测摘要状态。
    详细到单个场景/通话/对话的结果由 CallExecution 保存。
    """

    class ExecutionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        CANCELLING = "cancelling", "Cancelling"
        EVALUATING = "evaluating", "Evaluating"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    run_test = models.ForeignKey(
        RunTest,
        on_delete=models.CASCADE,
        related_name="executions",
        help_text="The run test being executed",
    )

    status = models.CharField(
        max_length=20,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.PENDING,
        help_text="Current status of the test execution",
    )

    started_at = models.DateTimeField(
        default=timezone.now, help_text="When the test execution started"
    )

    completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the test execution completed"
    )

    total_scenarios = models.IntegerField(
        default=0, help_text="Total number of scenarios in this execution"
    )

    scenario_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of scenario IDs that were executed in this run",
    )

    total_calls = models.IntegerField(
        default=0, help_text="Total number of calls to be made"
    )

    completed_calls = models.IntegerField(
        default=0, help_text="Number of successfully completed calls"
    )

    failed_calls = models.IntegerField(default=0, help_text="Number of failed calls")

    execution_metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional metadata about the execution"
    )

    picked_up_by_executor = models.BooleanField(
        default=False,
        help_text="Whether the test execution was picked up by the executor",
    )

    # 存储本次执行实际使用的 simulator 和 agent definition。
    simulator_agent = models.ForeignKey(
        SimulatorAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_executions_simulation",
        help_text="Simulator agent used for this test execution",
    )

    agent_definition = models.ForeignKey(
        AgentDefinition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_executions_agent",
        help_text="Agent definition used for this test execution",
    )

    agent_version = models.ForeignKey(
        AgentVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_executions_agent_version",
        help_text="Agent version used for this test execution",
    )

    eval_explanation_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Evaluation explanation summary for each eval config",
    )

    eval_explanation_summary_last_updated = models.DateTimeField(
        null=True,
        blank=True,
    )

    eval_explanation_summary_status = models.CharField(
        max_length=20,
        choices=EvalExplanationSummaryStatus.choices,
        default=EvalExplanationSummaryStatus.PENDING,
        help_text="Status of the evaluation explanation summary",
    )

    agent_optimiser = models.ForeignKey(
        AgentOptimiser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_executions",
        help_text="Agent optimiser used for this test execution",
    )

    error_reason = models.CharField(blank=True, null=True)

    class Meta:
        db_table = "simulate_test_execution"
        verbose_name = "Test Execution"
        verbose_name_plural = "Test Executions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "picked_up_by_executor"], name="idx_status_executor"
            ),
            models.Index(fields=["run_test_id"], name="idx_testexecution_run_test_id"),
        ]

    def __str__(self):
        return f"{self.run_test.name} - {self.status}"

    @property
    def duration_seconds(self):
        """汇总所有 call execution 的持续时间，返回总秒数。"""
        total_duration = 0
        for call in self.calls.all():
            if call.duration_seconds is not None:
                total_duration += call.duration_seconds
        return int(total_duration) if total_duration > 0 else None

    @property
    def success_rate(self):
        """按百分比计算成功率。"""
        if self.total_calls > 0:
            return (self.completed_calls / self.total_calls) * 100
        return 0


class CallExecution(BaseModel):
    """
    单个场景、通话或聊天模拟的执行记录。

    它是 Simulate 最细粒度的执行结果：provider call id、录音、
    transcript、成本、provider 原始数据、监控日志、评测结果和错误原因
    都围绕这个模型聚合。
    """

    class CallStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        REGISTERED = "queued", "Queued"
        ONGOING = "ongoing", "Ongoing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        ANALYZING = "analyzing", "Analyzing"
        CANCELLED = "cancelled", "Cancelled"

    # 复用 agent 类型选项（voice/text），避免多个模型重复维护同一组值。
    SimulationCallType = AgentTypeChoices

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    test_execution = models.ForeignKey(
        TestExecution,
        on_delete=models.CASCADE,
        related_name="calls",
        help_text="The test execution this call belongs to",
    )

    simulation_call_type = models.CharField(
        max_length=20,
        choices=SimulationCallType.choices,
        default=SimulationCallType.VOICE,
        help_text="Type of simulation call",
    )

    scenario = models.ForeignKey(
        Scenarios,
        on_delete=models.CASCADE,
        related_name="call_executions",
        help_text="The scenario this call belongs to",
    )

    phone_number = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Phone number called (null for TEXT/chat simulations)",
    )

    service_provider_call_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Service provider call ID for tracking",
    )

    status = models.CharField(
        max_length=20,
        choices=CallStatus.choices,
        default=CallStatus.PENDING,
        help_text="Current status of the call",
    )

    started_at = models.DateTimeField(
        null=True, blank=True, help_text="When the call started"
    )

    completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the call completed"
    )

    duration_seconds = models.IntegerField(
        null=True, blank=True, help_text="Duration of the call in seconds"
    )

    recording_url = models.URLField(
        max_length=500, null=True, blank=True, help_text="URL to the call recording"
    )

    cost_cents = models.IntegerField(
        null=True, blank=True, help_text="Cost of the call in cents"
    )

    call_metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional metadata about the call"
    )

    error_message = models.TextField(
        null=True, blank=True, help_text="Error message if the call failed"
    )

    # 存储完整 call 数据的附加字段。
    provider_call_data = models.JSONField(
        null=True,
        blank=True,
        validators=[validate_provider_sent_objects],
        help_text="Complete call data from the provider. Format: dict[provider_name, data] where provider_name must be from SupportedProviders",
    )

    monitor_call_data = models.JSONField(
        null=True, blank=True, help_text="Monitor call data from API"
    )

    logs_ingested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When call logs were last ingested from the provider",
    )

    logs_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Lightweight summary of ingested call logs (e.g., counts by level)",
    )

    customer_logs_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Summary of customer call logs (e.g., counts by level)",
    )

    stereo_recording_url = models.URLField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Stereo recording URL from Vapi",
    )

    customer_log_url = models.URLField(
        max_length=10000,
        null=True,
        blank=True,
        help_text="Customer call log URL from the service provider",
    )

    call_summary = models.TextField(
        null=True, blank=True, help_text="Call summary from the service"
    )

    ended_reason = models.CharField(
        max_length=10000, null=True, blank=True, help_text="Reason why the call ended"
    )

    # 成本拆分字段：用于把一次 call 的 STT/LLM/TTS/存储/provider 成本分开分析。
    stt_cost_cents = models.IntegerField(
        null=True, blank=True, help_text="STT cost in cents"
    )

    llm_cost_cents = models.IntegerField(
        null=True, blank=True, help_text="LLM cost in cents"
    )

    tts_cost_cents = models.IntegerField(
        null=True, blank=True, help_text="TTS cost in cents"
    )

    storage_cost_cents = models.FloatField(
        null=True, blank=True, help_text="S3 recording storage cost in cents"
    )

    vapi_cost_cents = Deprecated(
        models.IntegerField(
            null=True, blank=True, help_text="Vapi platform cost in cents"
        )
    )

    # 性能指标。
    overall_score = models.FloatField(
        null=True, blank=True, help_text="Overall call performance score"
    )

    response_time_ms = models.IntegerField(
        null=True, blank=True, help_text="Average response time in milliseconds"
    )

    # provider 返回的附加 call 详情。
    assistant_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Assistant ID used for the call (system side)",
    )

    customer_number = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Customer phone number (E.164 format)",
    )

    call_type = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Type of call (e.g., outboundPhoneCall)",
    )

    ended_at = models.DateTimeField(
        null=True, blank=True, help_text="When the call ended"
    )

    # 分析和评测字段。
    analysis_data = models.JSONField(
        null=True, blank=True, help_text="Call analysis data from the service provider"
    )

    evaluation_data = models.JSONField(
        null=True,
        blank=True,
        help_text="Call evaluation data from the service provider",
    )

    # 消息和 transcript 元数据。
    message_count = models.IntegerField(
        null=True, blank=True, help_text="Number of messages in the call"
    )

    transcript_available = models.BooleanField(
        default=False, help_text="Whether transcript is available"
    )

    recording_available = models.BooleanField(
        default=False, help_text="Whether recording is available"
    )

    row_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Row ID from the dataset if this call is from a dataset scenario",
    )

    eval_outputs = models.JSONField(
        null=True, blank=True, help_text="Evaluation output"
    )

    tool_outputs = models.JSONField(
        null=True,
        blank=True,
        help_text="Tool evaluation output - separate from standard evaluations",
    )

    agent_version = models.ForeignKey(
        AgentVersion,
        on_delete=models.CASCADE,
        related_name="call_executions",
        null=True,
    )

    customer_call_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Customer call ID if available",
    )
    customer_cost_cents = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total customer-reported cost in cents",
    )
    customer_cost_breakdown = models.JSONField(
        null=True,
        blank=True,
        help_text="Detailed cost breakdown from customer call data",
    )
    customer_latency_metrics = models.JSONField(
        null=True,
        blank=True,
        help_text="Latency metrics from customer call data",
    )
    # 对话指标字段。
    avg_agent_latency_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average agent latency in milliseconds (time taken by agent to respond after user's pause)",
    )

    user_interruption_count = models.IntegerField(
        null=True, blank=True, help_text="Number of times user interrupted the AI"
    )

    user_interruption_rate = models.FloatField(
        null=True,
        blank=True,
        help_text="Rate of user interruptions (interruptions per minute)",
    )

    user_wpm = models.FloatField(
        null=True, blank=True, help_text="User's words per minute"
    )

    bot_wpm = models.FloatField(
        null=True, blank=True, help_text="Bot's words per minute"
    )

    talk_ratio = models.FloatField(
        null=True,
        blank=True,
        help_text="Ratio of bot speaking time to user speaking time",
    )

    ai_interruption_count = models.IntegerField(
        null=True, blank=True, help_text="Number of times AI interrupted the user"
    )

    ai_interruption_rate = models.FloatField(
        null=True,
        blank=True,
        help_text="Rate of AI interruptions (interruptions per minute)",
    )

    avg_stop_time_after_interruption_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average stop time after user interruption in milliseconds",
    )

    conversation_metrics_data = models.JSONField(
        null=True, blank=True, help_text="Detailed conversation metrics data"
    )

    def clean(self):
        """
        Validate model fields including custom validators for JSONFields.
        This ensures Pydantic validation runs on provider_call_data.
        """
        super().clean()
        # Django 会自动调用字段 validator，包括 validate_provider_call_data。

    # reset_to_default 会重置的字段；bulk_update 路径会复用这份字段清单。
    RESET_FIELDS = [
        "monitor_call_data",
        "provider_call_data",
        "service_provider_call_id",
        "customer_call_id",
        "status",
        "started_at",
        "completed_at",
        "ended_at",
        "duration_seconds",
        "recording_url",
        "stereo_recording_url",
        "cost_cents",
        "stt_cost_cents",
        "llm_cost_cents",
        "tts_cost_cents",
        "vapi_cost_cents",
        "call_summary",
        "ended_reason",
        "overall_score",
        "response_time_ms",
        "assistant_id",
        "customer_number",
        "call_type",
        "analysis_data",
        "evaluation_data",
        "message_count",
        "transcript_available",
        "recording_available",
        "eval_outputs",
        "tool_outputs",
        "avg_agent_latency_ms",
        "user_interruption_count",
        "user_interruption_rate",
        "user_wpm",
        "bot_wpm",
        "talk_ratio",
        "ai_interruption_count",
        "ai_interruption_rate",
        "avg_stop_time_after_interruption_ms",
        "conversation_metrics_data",
    ]

    def reset_to_default(self, save: bool = True):
        """
        Reset call execution fields to their default values for rerun.

        Args:
            save: If True (default), saves the model after resetting.
                  Set to False for bulk operations where you'll call bulk_update.
        """
        self.monitor_call_data = None
        # 清空 provider-neutral 元数据和 payload，让重跑时重新抓取数据。
        self.provider_call_data = None
        self.service_provider_call_id = None
        self.customer_call_id = None
        self.status = CallExecution.CallStatus.PENDING
        self.started_at = None
        self.completed_at = None
        self.ended_at = None
        self.duration_seconds = None
        self.recording_url = None
        self.stereo_recording_url = None
        self.cost_cents = None
        self.stt_cost_cents = None
        self.llm_cost_cents = None
        self.tts_cost_cents = None
        self.storage_cost_cents = None
        self.vapi_cost_cents = None
        self.call_summary = None
        self.ended_reason = None
        self.overall_score = None
        self.response_time_ms = None
        self.assistant_id = None
        self.customer_number = None
        self.call_type = None
        self.analysis_data = None
        self.evaluation_data = None
        self.message_count = None
        self.transcript_available = False
        self.recording_available = False
        self.eval_outputs = {}
        self.tool_outputs = {}
        self.avg_agent_latency_ms = None
        self.user_interruption_count = None
        self.user_interruption_rate = None
        self.user_wpm = None
        self.bot_wpm = None
        self.talk_ratio = None
        self.ai_interruption_count = None
        self.ai_interruption_rate = None
        self.avg_stop_time_after_interruption_ms = None
        self.conversation_metrics_data = None

        if not isinstance(self.call_metadata, dict):
            self.call_metadata = {}
        self.call_metadata.pop("eval_started", None)
        self.call_metadata.pop("eval_completed", None)
        self.call_metadata.pop("processing_skipped", None)
        self.call_metadata.pop("processing_skip_reason", None)

        if save:
            self.save()

    class Meta:
        db_table = "simulate_call_execution"
        verbose_name = "Call Execution"
        verbose_name_plural = "Call Executions"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["status"], name="idx_callexecution_status"),
            models.Index(
                fields=["test_execution", "status"],
                name="idx_callexec_testexec_status",
            ),
        ]

    def __str__(self):
        return f"{self.phone_number} - {self.status}"

    @property
    def is_successful(self):
        """判断 call 是否成功。"""
        return self.status == self.CallStatus.COMPLETED

    @property
    def is_failed(self):
        """判断 call 是否失败。"""
        return self.status in [self.CallStatus.FAILED, self.CallStatus.CANCELLED]


class CallTranscript(BaseModel):
    """
    Model to store call transcript data
    """

    class SpeakerRole(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"
        TOOL_CALLS = "tool_calls", "Tool Calls"
        TOOL_CALL_RESULT = "tool_call_result", "Tool Call Result"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    call_execution = models.ForeignKey(
        CallExecution,
        on_delete=models.CASCADE,
        related_name="transcripts",
        help_text="The call execution this transcript belongs to",
    )

    speaker_role = models.CharField(
        max_length=20,
        choices=SpeakerRole.choices,
        default=SpeakerRole.UNKNOWN,
        help_text="Role of the speaker (user or assistant)",
    )

    content = models.TextField(help_text="Transcript content")

    start_time_ms = models.BigIntegerField(
        default=0, help_text="Start time of this transcript segment in milliseconds"
    )

    end_time_ms = models.BigIntegerField(
        default=0, help_text="End time of this transcript segment in milliseconds"
    )

    confidence_score = models.FloatField(
        default=1.0, help_text="Confidence score for this transcript segment"
    )

    class Meta:
        db_table = "simulate_call_transcript"
        verbose_name = "Call Transcript"
        verbose_name_plural = "Call Transcripts"
        ordering = ["start_time_ms"]

    def __str__(self):
        return f"{self.speaker_role} - {self.content[:50]}..."


class CallExecutionSnapshot(BaseModel):
    """
    Model to store historical snapshots of call executions before reruns
    """

    class RerunType(models.TextChoices):
        EVAL_ONLY = "eval_only", "Evaluation Only"
        CALL_AND_EVAL = "call_and_eval", "Call and Evaluation"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    call_execution = models.ForeignKey(
        CallExecution,
        on_delete=models.CASCADE,
        related_name="snapshots",
        help_text="The call execution this snapshot belongs to",
    )

    snapshot_timestamp = models.DateTimeField(
        auto_now_add=True, help_text="When this snapshot was created"
    )

    rerun_type = models.CharField(
        max_length=20,
        choices=RerunType.choices,
        help_text="Type of rerun that triggered this snapshot",
    )

    # 创建 snapshot 时的 call execution 数据。
    service_provider_call_id = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    recording_url = models.URLField(max_length=500, null=True, blank=True)
    stereo_recording_url = models.URLField(max_length=500, null=True, blank=True)

    # 成本数据。
    cost_cents = models.IntegerField(null=True, blank=True)
    stt_cost_cents = Deprecated(models.IntegerField(null=True, blank=True))
    llm_cost_cents = Deprecated(models.IntegerField(null=True, blank=True))
    tts_cost_cents = Deprecated(models.IntegerField(null=True, blank=True))
    vapi_cost_cents = Deprecated(models.IntegerField(null=True, blank=True))

    # call 详情。
    call_summary = models.TextField(null=True, blank=True)
    ended_reason = models.CharField(max_length=10000, null=True, blank=True)
    overall_score = models.FloatField(null=True, blank=True)
    response_time_ms = models.IntegerField(null=True, blank=True)
    assistant_id = models.CharField(max_length=255, null=True, blank=True)
    customer_number = models.CharField(max_length=20, null=True, blank=True)
    call_type = models.CharField(max_length=50, null=True, blank=True)
    message_count = models.IntegerField(null=True, blank=True)
    transcript_available = models.BooleanField(default=False)
    recording_available = models.BooleanField(default=False)

    # 复杂结构使用 JSON 字段存储。
    analysis_data = models.JSONField(null=True, blank=True)
    evaluation_data = models.JSONField(null=True, blank=True)
    eval_outputs = models.JSONField(null=True, blank=True)
    tool_outputs = models.JSONField(null=True, blank=True)
    provider_call_data = models.JSONField(
        null=True,
        blank=True,
        validators=[validate_provider_sent_objects],
    )
    monitor_call_data = models.JSONField(null=True, blank=True)

    # 对话指标。
    avg_agent_latency_ms = models.IntegerField(null=True, blank=True)
    user_interruption_count = models.IntegerField(null=True, blank=True)
    user_interruption_rate = models.FloatField(null=True, blank=True)
    user_wpm = models.FloatField(null=True, blank=True)
    bot_wpm = models.FloatField(null=True, blank=True)
    talk_ratio = models.FloatField(null=True, blank=True)
    ai_interruption_count = models.IntegerField(null=True, blank=True)
    ai_interruption_rate = models.FloatField(null=True, blank=True)
    avg_stop_time_after_interruption_ms = models.IntegerField(null=True, blank=True)
    conversation_metrics_data = models.JSONField(null=True, blank=True)

    # transcript 快照；作为历史数据以 JSON 保存。
    transcripts = models.JSONField(
        default=list,
        blank=True,
        help_text="Snapshot of transcripts at the time of rerun",
    )

    def clean(self):
        super().clean()

    class Meta:
        db_table = "simulate_call_execution_snapshot"
        verbose_name = "Call Execution Snapshot"
        verbose_name_plural = "Call Execution Snapshots"
        ordering = ["-snapshot_timestamp"]
        indexes = [
            models.Index(
                fields=["call_execution", "-snapshot_timestamp"],
                name="idx_snapshot_call_time",
            ),
            models.Index(fields=["rerun_type"], name="idx_snapshot_rerun_type"),
        ]

    def __str__(self):
        return f"Snapshot of {self.call_execution.id} at {self.snapshot_timestamp}"
