import re

import structlog
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from tfc.utils.base_viewset import BaseModelViewSetMixinWithUserOrg
from tfc.utils.general_methods import GeneralMethods

logger = structlog.get_logger(__name__)

# 静态参考数据。
PII_ENTITY_TYPES = [
    {"id": "SSN", "label": "Social Security Number", "category": "identity"},
    {"id": "CREDIT_CARD", "label": "Credit Card Number", "category": "financial"},
    {"id": "EMAIL", "label": "Email Address", "category": "contact"},
    {"id": "PHONE", "label": "Phone Number", "category": "contact"},
    {"id": "ADDRESS", "label": "Physical Address", "category": "contact"},
    {"id": "NAME", "label": "Person Name", "category": "identity"},
    {"id": "DOB", "label": "Date of Birth", "category": "identity"},
    {"id": "PASSPORT", "label": "Passport Number", "category": "identity"},
    {"id": "DRIVER_LICENSE", "label": "Driver's License", "category": "identity"},
    {"id": "IP_ADDRESS", "label": "IP Address", "category": "technical"},
    {"id": "BANK_ACCOUNT", "label": "Bank Account Number", "category": "financial"},
    {"id": "MEDICAL_RECORD", "label": "Medical Record Number", "category": "health"},
    {"id": "AWS_KEY", "label": "AWS Access Key", "category": "technical"},
    {"id": "API_KEY", "label": "API Key / Secret", "category": "technical"},
]

TOPIC_CATEGORIES = [
    {
        "id": "violence",
        "label": "Violence & Harm",
        "subcategories": [
            "weapons",
            "self_harm",
            "threats",
            "graphic_violence",
        ],
    },
    {
        "id": "sexual",
        "label": "Sexual Content",
        "subcategories": [
            "explicit",
            "suggestive",
            "minors",
        ],
    },
    {
        "id": "hate",
        "label": "Hate Speech & Discrimination",
        "subcategories": [
            "racism",
            "sexism",
            "religious_hate",
            "disability_hate",
        ],
    },
    {
        "id": "illegal",
        "label": "Illegal Activities",
        "subcategories": [
            "drugs",
            "fraud",
            "hacking",
            "terrorism",
        ],
    },
    {
        "id": "misinformation",
        "label": "Misinformation",
        "subcategories": [
            "health_misinfo",
            "political_misinfo",
            "conspiracy",
        ],
    },
    {
        "id": "privacy",
        "label": "Privacy Violations",
        "subcategories": [
            "doxxing",
            "surveillance",
            "stalking",
        ],
    },
    {
        "id": "profanity",
        "label": "Profanity & Offensive Language",
        "subcategories": [
            "strong_profanity",
            "slurs",
            "insults",
        ],
    },
    {"id": "custom", "label": "Custom Topics", "subcategories": []},
]


class AgentccGuardrailConfigViewSet(BaseModelViewSetMixinWithUserOrg, GenericViewSet):
    """Guardrail 配置所需的参考数据和工具接口。"""

    permission_classes = [IsAuthenticated]
    _gm = GeneralMethods()

    @action(detail=False, methods=["get"], url_path="pii-entities")
    def pii_entities(self, request):
        """列出所有可用的 PII 实体类型。"""
        return self._gm.success_response(PII_ENTITY_TYPES)

    @action(detail=False, methods=["get"])
    def topics(self, request):
        """列出主题限制分类。"""
        return self._gm.success_response(TOPIC_CATEGORIES)

    @action(detail=False, methods=["post"], url_path="validate-cel")
    def validate_cel(self, request):
        """校验 CEL 表达式语法。"""
        expression = request.data.get("expression", "")
        if not expression:
            return self._gm.bad_request("expression is required")

        valid, error = _validate_cel_syntax(expression)
        return self._gm.success_response(
            {
                "expression": expression,
                "valid": valid,
                "error": error,
            }
        )


def _validate_cel_syntax(expression):
    """
    基础 CEL 表达式语法校验。
    检查括号平衡、基础运算符用法和常见模式。
    返回 (valid: bool, error: str or None)。
    """
    if not isinstance(expression, str) or not expression.strip():
        return False, "Expression must be a non-empty string"

    expr = expression.strip()

    # 检查括号是否平衡。
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth < 0:
            return False, "Unbalanced parentheses: unexpected ')'"
    if depth != 0:
        return False, "Unbalanced parentheses: missing ')'"

    # 检查空括号；这通常是误写。
    if "()" in expr and not re.search(r"\w+\(\)", expr):
        return False, "Empty parentheses without function call"

    # 检查悬空或异常的运算符。
    if re.search(r"[&|]{3,}", expr):
        return False, "Invalid operator sequence"

    # 基础结构检查：至少包含比较、函数调用或标识符之一。
    has_comparison = bool(
        re.search(r"[=!<>]=?|in\b|contains|matches|startsWith|endsWith", expr)
    )
    has_function = bool(re.search(r"\w+\(", expr))
    has_identifier = bool(re.search(r"[a-zA-Z_]\w*", expr))

    if not (has_comparison or has_function or has_identifier):
        return (
            False,
            "Expression must contain at least one comparison, function call, or identifier",
        )

    return True, None
