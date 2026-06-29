from dataclasses import dataclass, field
from typing import TypedDict

import pandas as pd
from pydantic import BaseModel


class OpenAiPromptMessage(TypedDict):
    role: str
    content: str

class DataPoint(TypedDict, total=False):
    """单条评测输入数据。不同 evaluator 会通过 required_args 声明需要哪些字段。"""

    response: str

class EvalResultMetric(TypedDict):
    """
    单个评测指标结果，例如 score、confidence 或自定义 rubric 分数。
    """

    id: str
    value: float


class DatapointFieldAnnotation(TypedDict):
    """
    写回 Fi 平台的数据字段标注，用于解释某个输入/输出字段为何影响评测结果。
    """

    field_name: str
    text: str
    annotation_type: str
    annotation_note: str


class EvalResult(TypedDict):
    """
    evaluator 的标准输出结构。

    读源码时重点看 failure/reason/metrics：
    failure 决定 guard 模式是否通过，reason 给 UI/日志展示解释，
    metrics 则承载可以聚合或画图的数值。
    """

    name: str
    display_name: str
    data: dict
    failure: bool | None
    reason: str
    runtime: int
    model: str | None
    metadata: str | None
    metrics: list[EvalResultMetric]
    datapoint_field_annotations: list[DatapointFieldAnnotation] | None


@dataclass
class BatchRunResult:
    """
    批量评测返回值，保留原始评测结果，并可转换为 DataFrame 便于离线分析。
    """

    eval_results: list[EvalResult | None]
    eval_request_id: str | None = field(default=None)

    def to_df(self):
        """
        将批量评测结果转换为 Pandas DataFrame。
        固定字段和动态 metrics 会被摊平成列，方便 notebook 或报表使用。
        """
        pd.set_option("display.max_colwidth", 500)

        df_data = []
        for item in self.eval_results:
            if item is None:
                # 对失败或异常的单条评测保留空行，避免批量结果错位。
                entry = {
                    "display_name": None,
                    "failed": None,
                    "grade_reason": None,
                    "runtime": None,
                    "model": None,
                    # 后续如果需要展示更多固定字段，可以在这里补空值占位。
                }
            else:
                # 先展开 evaluator 返回的原始 data 字段。
                entry = dict(item["data"].items())

                # 再追加所有 evaluator 都有的固定字段。
                entry.update(
                    {
                        "display_name": item["display_name"],
                        "failed": item.get("failure"),
                        "grade_reason": item["reason"],
                        "runtime": item["runtime"],
                        "model": item.get("model"),
                    }
                )

                # 最后把 metrics 动态展开成独立列。
                for metric in item["metrics"]:
                    entry[metric["id"]] = metric["value"]

            df_data.append(entry)

        df = pd.DataFrame(df_data)
        return df


class EvalPerformanceReport(TypedDict):
    """
    评测器自身的效果报告，用于衡量 evaluator 判断是否准确。
    """

    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    runtime: int
    dataset_size: int


class GuardResult(BaseModel):
    passed: bool
    reason: str
    runtime: int
