"""
Guardrail 同步服务：将 guardrail 策略合并到 org config，
并把合并结果推送到 gateway。
"""

import structlog
from django.db import transaction

from agentcc.models import AgentccOrgConfig
from agentcc.models.guardrail_policy import AgentccGuardrailPolicy

logger = structlog.get_logger(__name__)


def sync_guardrail_policies(org, user=None):
    """
    收集当前 org 下所有激活的 guardrail 策略，将其中的 checks 合并为
    单一 guardrail config，然后推送到 gateway。

    Returns:
        gateway 推送成功返回 True；失败或跳过返回 False。
    """
    # 只收集 SCOPE_GLOBAL 的激活策略，并按优先级排序。
    policies = list(
        AgentccGuardrailPolicy.no_workspace_objects.filter(
            organization=org,
            is_active=True,
            deleted=False,
            scope=AgentccGuardrailPolicy.SCOPE_GLOBAL,
        ).order_by("priority", "name")
    )

    # 合并 checks；同名 check 以后出现的策略为准。
    merged_checks = {}
    for policy in policies:
        for check in policy.checks:
            name = check.get("name")
            if name:
                merged_checks[name] = {
                    **check,
                    "_policy": str(policy.id),
                    "_policy_name": policy.name,
                    "_mode": policy.mode,
                    "_scope": policy.scope,
                }

    merged_guardrails = {
        "enabled": len(merged_checks) > 0,
        "checks": list(merged_checks.values()),
    }

    with transaction.atomic():
        # 加锁读取当前激活的 org config。
        active_config = (
            AgentccOrgConfig.no_workspace_objects.select_for_update()
            .filter(
                organization=org,
                is_active=True,
                deleted=False,
            )
            .first()
        )

        if not active_config:
            logger.info(
                "guardrail_sync_skipped",
                reason="no active org config",
                org_id=str(org.id),
            )
            return False

        # 在当前激活配置上原地更新 guardrails，而不是创建新版本。
        # 这样可以避免版本无限累积；只有用户在 org config UI 显式保存时才创建新版本。
        active_config.guardrails = merged_guardrails
        active_config.save(update_fields=["guardrails", "updated_at"])

    # 推送到 gateway。
    from agentcc.services.config_push import push_org_config

    synced = push_org_config(str(org.id), active_config)
    if synced:
        logger.info(
            "guardrail_sync_success",
            org_id=str(org.id),
            version=active_config.version,
            policy_count=len(policies),
        )
    return synced
