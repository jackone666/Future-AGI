import uuid

from django.db import models

from tfc.utils.base_model import BaseModel
from tracer.models.project import Project
from tracer.models.project_version import ProjectVersion
from tracer.models.trace_session import TraceSession


class TraceErrorAnalysisStatus(models.TextChoices):
    """trace 错误分析处理状态。"""

    PENDING = ("pending",)  # 尚未处理
    PROCESSING = ("processing",)  # 正在分析
    COMPLETED = ("completed",)  # 分析成功
    SKIPPED = ("skipped",)  # 已跳过分析
    FAILED = ("failed",)


class Trace(BaseModel):
    """一次完整 agent/LLM 请求或会话片段的根记录。

    Trace 保存顶层输入、输出、错误、标签和 session/project 归属；
    具体的 LLM/tool/guardrail/evaluator 等步骤保存在 ObservationSpan 中。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="traces",
        blank=False,
        null=False,
    )
    project_version = models.ForeignKey(
        ProjectVersion,
        on_delete=models.CASCADE,
        related_name="traces",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=2000, blank=True, null=True)
    metadata = models.JSONField(null=True, blank=True)
    input = models.JSONField(null=True, blank=True)
    output = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    session = models.ForeignKey(
        TraceSession,
        on_delete=models.CASCADE,
        related_name="traces",
        blank=True,
        null=True,
    )
    external_id = models.CharField(max_length=255, null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    error_analysis_status = models.CharField(
        max_length=20,
        default=TraceErrorAnalysisStatus.PENDING,
        choices=TraceErrorAnalysisStatus.choices,
    )

    class Meta:
        db_table = "tracer_trace"
        ordering = ["-created_at"]

        indexes = [
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["project_version"]),
            models.Index(fields=["session"]),
            models.Index(fields=["external_id"]),
        ]
