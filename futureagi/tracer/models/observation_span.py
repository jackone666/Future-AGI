import uuid

from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import Organization
from model_hub.models.choices import StatusType
from model_hub.models.prompt_label import PromptLabel
from model_hub.models.run_prompt import PromptVersion
from tfc.utils.base_model import BaseModel
from tracer.models.custom_eval_config import CustomEvalConfig
from tracer.models.project import Project
from tracer.models.project_version import ProjectVersion
from tracer.models.trace import Trace
from tracer.models.trace_session import TraceSession


def validate_span_status(value):
    valid_choices = [choice[0] for choice in ObservationSpan.SPAN_STATUS]
    if value not in valid_choices:
        raise ValidationError(
            f"Invalid span status. Valid choices are: {', '.join(valid_choices)}"
        )


class UserIdType(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Phone"
    UUID = "uuid", "UUID"
    CUSTOM = "custom", "Custom"


class ObservationType(models.TextChoices):
    """与 ``ObservationSpan.OBSERVATION_SPAN_TYPES`` 保持一致的类型化枚举。

    与 ``ObservationSpan.observation_type`` 做相等判断时应使用它，
    避免直接写裸字符串。这里的值必须与下方 ``OBSERVATION_SPAN_TYPES``
    元组保持同步。
    """

    TOOL = "tool", "Tool"
    CHAIN = "chain", "Chain"
    LLM = "llm", "LLM"
    RETRIEVER = "retriever", "Retriever"
    EMBEDDING = "embedding", "Embedding"
    AGENT = "agent", "Agent"
    RERANKER = "reranker", "Reranker"
    UNKNOWN = "unknown", "Unknown"
    GUARDRAIL = "guardrail", "Guardrail"
    EVALUATOR = "evaluator", "Evaluator"
    CONVERSATION = "conversation", "Conversation"


class EndUser(BaseModel):
    """Trace 维度的最终用户画像。

    EndUser 用 project + organization + user_id + user_id_type 做唯一约束，
    让 Error Feed、趋势分析和用户影响范围统计可以从 trace 反查到用户。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
    )
    workspace = models.ForeignKey(
        "accounts.Workspace",
        on_delete=models.CASCADE,
        related_name="end_users",
        null=True,
        blank=True,
    )
    user_id = models.CharField(max_length=255, null=False, blank=False)
    user_id_type = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        choices=UserIdType.choices,
    )
    user_id_hash = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
    )

    class Meta:
        unique_together = ("project", "organization", "user_id", "user_id_type")


class ObservationSpan(BaseModel):
    """Trace 内部的可观测片段。

    一个 Trace 通常包含多个 ObservationSpan：LLM 调用、工具调用、agent
    节点、retriever、guardrail、evaluator 等都统一落在这里。Tracer
    的大部分分析查询都围绕这个模型及其 ClickHouse 镜像展开。
    """

    OBSERVATION_SPAN_TYPES = (
        ("tool", "Tool"),
        ("chain", "Chain"),
        ("llm", "LLM"),
        ("retriever", "Retriever"),
        ("embedding", "Embedding"),
        ("agent", "Agent"),
        ("reranker", "Reranker"),
        ("unknown", "Unknown"),
        ("guardrail", "Guardrail"),
        ("evaluator", "Evaluator"),
        ("conversation", "Conversation"),
    )

    OBSERVATION_SPAN_LOGGER_STATUS = (
        ("COMPLETED", "completed"),
        ("ERROR", "error"),
    )

    SPAN_STATUS = (
        ("UNSET", "unset"),
        ("OK", "ok"),
        ("ERROR", "error"),
    )

    id = models.CharField(primary_key=True, max_length=255, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="observation_spans",
        null=False,
        blank=False,
    )
    project_version = models.ForeignKey(
        ProjectVersion,
        on_delete=models.CASCADE,
        related_name="observation_spans",
        null=True,
        blank=True,
    )
    trace = models.ForeignKey(
        Trace,
        on_delete=models.CASCADE,
        related_name="observation_spans",
        null=False,
        blank=False,
    )
    parent_span_id = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=2000, null=False, blank=False)
    observation_type = models.CharField(
        max_length=20, choices=OBSERVATION_SPAN_TYPES, null=False, blank=False
    )
    operation_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Operation type within span kind (e.g., chat, image_generation, speech_to_text)",
    )
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)

    input = models.JSONField(null=True, blank=True)
    output = models.JSONField(null=True, blank=True)

    model = models.CharField(max_length=255, null=True, blank=True)
    model_parameters = models.JSONField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)

    org_id = models.UUIDField(blank=True, null=True)
    org_user_id = models.UUIDField(null=True, blank=True)

    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    response_time = models.FloatField(null=True, blank=True)

    eval_id = models.CharField(max_length=255, null=True, blank=True)
    cost = models.FloatField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=SPAN_STATUS,
        null=True,
        blank=True,
        validators=[validate_span_status],
    )
    status_message = models.TextField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True, null=True)
    metadata = models.JSONField(null=True, blank=True)
    span_events = models.JSONField(default=list, blank=True, null=True)

    provider = models.CharField(max_length=255, null=True, blank=True)

    input_images = models.JSONField(default=list, blank=True, null=True)
    eval_input = models.JSONField(default=list, blank=True, null=True)

    eval_attributes = models.JSONField(default=dict, blank=True, null=True)
    custom_eval_config = models.ForeignKey(
        CustomEvalConfig,
        on_delete=models.CASCADE,
        related_name="observation_spans",
        blank=True,
        null=True,
    )
    # TODO(tech-debt): span 上的 eval_status 是一个设计缺陷。
    # 它是反规范化快照，新增/移除 eval 后容易过期。
    # 状态应从 EvalLogger 行（每个 eval 对每个 span 一行）推导，而不是依赖这里的单个标记。
    # 参考：span.py 中的 run_evals_on_spans()，eval.py 中的 eval_observation_span_runner()。
    eval_status = models.CharField(
        max_length=50,
        choices=[(status.value, status.value) for status in StatusType],
        null=True,
        blank=True,
        default=StatusType.INACTIVE.value,
    )

    end_user = models.ForeignKey(
        EndUser,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
    )

    prompt_version = models.ForeignKey(
        PromptVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
    )

    prompt_label = models.ForeignKey(
        PromptLabel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
    )

    # GenAI Schema 基础：灵活存储属性。
    span_attributes = models.JSONField(
        default=dict,
        blank=True,
        help_text="Raw OTEL span attributes in their original form. "
        "Stored for ClickHouse migration and future-proofing.",
    )
    resource_attributes = models.JSONField(
        default=dict,
        blank=True,
        help_text="Raw OTEL resource attributes (service.name, project info, etc.)",
    )
    semconv_source = models.CharField(
        max_length=50,
        default="traceai",
        help_text="Semantic convention source: traceai, otel_genai, openinference, openllmetry",
    )

    def __str__(self):
        return self.name

    class Meta:
        db_table = "tracer_observation_span"
        ordering = ["-start_time"]

        indexes = [
            models.Index(fields=["trace", "created_at"]),
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["project_version"]),
            models.Index(fields=["parent_span_id"]),
            models.Index(fields=["observation_type"]),
            models.Index(fields=["custom_eval_config"]),
            GinIndex(fields=["metadata"]),
            models.Index(fields=["start_time"]),
            models.Index(fields=["trace", "start_time"]),
        ]


class EvalTargetType(models.TextChoices):
    """EvalLogger 行所评测的目标粒度。

    target_type 用来区分 span 级、trace 级和 session 级结果：
    span 级结果是当前常见形态；trace 级结果锚定到 trace 的 root span，
    外键列形态和 span 行一致，靠 target_type 区分；session 级结果不设置
    span/trace 外键，而是设置 ``trace_session``。不同 target_type 的外键规则
    见 ``EvalLogger.Meta.constraints``。
    """

    SPAN = "span", "Span"
    TRACE = "trace", "Trace"
    SESSION = "session", "Session"


class EvalLogger(BaseModel):
    """评测结果日志。

    EvalLogger 可以挂在 span、trace 或 session 上；target_type 决定 FK
    组合形态。它是 tracer、dataset、simulation、SDK 和 playground
    评测结果进入分析层的统一入口。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # target_type='session' 时 trace 为空；其他 target_type 通常会设置 trace。
    # eval_logger_target_type_fks 约束要求：
    #   span/trace target -> 设置 observation_span + trace，trace_session 为空
    #   session target    -> observation_span + trace 为空，设置 trace_session
    trace = models.ForeignKey(
        Trace,
        on_delete=models.CASCADE,
        related_name="eval_logs",
        null=True,
        blank=True,
    )
    observation_span = models.ForeignKey(
        ObservationSpan,
        on_delete=models.CASCADE,
        related_name="eval_logs",
        null=True,
        blank=True,
    )
    trace_session = models.ForeignKey(
        TraceSession,
        on_delete=models.CASCADE,
        related_name="eval_logs",
        null=True,
        blank=True,
    )
    target_type = models.CharField(
        max_length=16,
        choices=EvalTargetType.choices,
        default=EvalTargetType.SPAN,
        db_index=True,
    )
    eval_type_id = models.CharField(max_length=255, null=True, blank=True)
    output_metadata = models.JSONField(null=True, blank=True)
    results_tags = models.JSONField(default=list, blank=True)
    results_explanation = models.JSONField(default=dict, blank=True)
    eval_tags = models.JSONField(default=list, blank=True)
    eval_explanation = models.TextField(null=True, blank=True)
    output_bool = models.BooleanField(null=True, blank=True)
    output_float = models.FloatField(null=True, blank=True)
    output_str = models.TextField(null=True, blank=True)
    output_str_list = models.JSONField(default=list, blank=True)
    eval_id = models.CharField(max_length=255, null=True, blank=True)
    eval_task_id = models.CharField(max_length=255, null=True, blank=True)
    custom_eval_config = models.ForeignKey(
        CustomEvalConfig,
        on_delete=models.CASCADE,
        related_name="eval_loggers",
        blank=True,
        null=True,
    )
    error = models.BooleanField(default=False)
    error_message = models.TextField(null=True, blank=True)
    # 评测被跳过时写入，例如映射的 span 属性不存在。
    # 它和 error 区分开，读取侧可以显示 "Skipped"，并从失败率指标中排除。
    skipped_reason = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Eval Log {self.id}"

    def clean(self):
        """在 Python 层校验不同 target_type 对应的外键形态。

        这和数据库中的 eval_logger_target_type_fks CHECK 约束保持一致。
        单行 .save() 可以更早抛出清晰的 ValidationError；bulk_create、
        raw SQL 和 ClickHouse CDC 镜像等绕过 Model.save() 的路径仍以数据库
        CHECK 约束作为最终防线。
        """
        super().clean()
        if self.target_type == EvalTargetType.SESSION:
            if self.observation_span_id or self.trace_id:
                raise ValidationError(
                    "Session-target EvalLogger rows must not set "
                    "observation_span or trace."
                )
            if not self.trace_session_id:
                raise ValidationError(
                    "Session-target EvalLogger rows must set trace_session."
                )
        else:
            if self.trace_session_id:
                raise ValidationError(
                    "Span/trace-target EvalLogger rows must not set "
                    "trace_session."
                )
            if not (self.observation_span_id and self.trace_id):
                raise ValidationError(
                    "Span/trace-target EvalLogger rows must set both "
                    "observation_span and trace."
                )

    def save(self, *args, **kwargs):
        # full_clean() 会运行字段 validator 和 clean()。
        # .save() 的单行写入会经过这里；bulk_create/raw insert 仍依赖 DB CHECK，
        # 因为 Django 设计上不会对 bulk_create 调 clean()。
        self.full_clean()
        super().save(*args, **kwargs)

    class Meta:
        db_table = "tracer_eval_logger"
        ordering = ["-created_at"]

        indexes = [
            models.Index(fields=["trace", "created_at"]),
            models.Index(fields=["observation_span"]),
            models.Index(fields=["trace_session"]),
            models.Index(fields=["custom_eval_config"]),
            models.Index(fields=["output_bool"]),
            models.Index(fields=["output_float"]),
            models.Index(fields=["error"]),
            # trace + session evaluator 的去重查询，以及 span-only 读取路径，
            # 都会用这三个字段缩小范围。迁移中会并发创建该索引，避免生产表锁。
            models.Index(
                fields=["eval_task_id", "target_type", "custom_eval_config"],
                name="eval_logger_task_target_idx",
            ),
        ]
        constraints = [
            # 互斥规则：span 和 trace target 共用 span+trace 外键形态
            # （trace 锚定到 root span，通过 target_type 区分）；session target
            # 则要求 span/trace 为空，并设置 trace_session。
            # “session eval 不出现在 span/trace 页面”这个前端可见规则由读取侧的
            # ``target_type IN ('span','trace')`` 过滤保证；这里的约束只负责保持
            # 数据库形态一致。
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(target_type__in=["span", "trace"])
                        & models.Q(observation_span__isnull=False)
                        & models.Q(trace__isnull=False)
                        & models.Q(trace_session__isnull=True)
                    )
                    | (
                        models.Q(target_type="session")
                        & models.Q(observation_span__isnull=True)
                        & models.Q(trace__isnull=True)
                        & models.Q(trace_session__isnull=False)
                    )
                ),
                name="eval_logger_target_type_fks",
            ),
        ]
