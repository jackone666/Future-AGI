import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed

from tfc.telemetry import wrap_for_thread

from agentic_eval.core_evals.fi_utils.dataset_helper import (
    generate_eval_display_name,
    generate_unique_dataset_name,
)
from agentic_eval.core_evals.fi_utils.evals_result import (
    BatchRunResult,
    DataPoint,
    EvalResult,
    GuardResult,
)
from agentic_eval.core_evals.fi_utils.fi_dataset import Dataset
from agentic_eval.core_evals.fi_utils.fi_logging_helper import FiLoggingHelper
import structlog

logger = structlog.get_logger(__name__)
from agentic_eval.core_evals.llm_services.fi_api_service import FiApiService


class BaseEvaluator(ABC):

    # 抽象属性。
    @property
    @abstractmethod
    def name(self) -> str:
        """评测器的唯一名称标识。"""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """评测器的展示名称。"""
        pass

    @property
    @abstractmethod
    def metric_ids(self) -> list[str]:
        """该评测器计算的指标。"""
        pass

    @property
    @abstractmethod
    def required_args(self) -> list[str]:
        """评测器所需参数列表。"""
        pass

    @property
    @abstractmethod
    def examples(self):
        """评测器示例列表。"""
        pass

    @abstractmethod
    def is_failure(self, *args) -> bool | None:
        """判断评测是否失败的方法。"""
        pass

    @abstractmethod
    def _evaluate(self, **kwargs) -> EvalResult:
        """执行评测的核心方法。"""
        pass

    def to_config(self) -> dict | None:
        return None

    # 通用方法。
    def _examples_str(self) -> str:
        return "" if self.examples is None else "\n".join(map(str, self.examples))


    def validate_args(self, **kwargs) -> None:
        """
        校验所有必需参数均存在且不为 None。
        """
        for arg in self.required_args:
            if arg not in kwargs:
                raise ValueError(f"Missing required argument: {arg}")
            elif kwargs[arg] is None:
                raise ValueError(f"{arg} cannot be None")

    def _validate_batch_args(self, data: list[DataPoint]) -> bool:
        """
        校验批量数据中的每一项都包含所有必需参数，且参数值不为 None。
        """
        for i, entry in enumerate(data):
            for arg in self.required_args:
                if arg not in entry:
                    raise ValueError(
                        f"Data at index {i} is missing required argument: {arg}"
                    )
                entry_dict = dict(entry)  # 将 TypedDict 转为普通 dict，便于动态访问。
                if entry_dict.get(arg) is None:
                    raise ValueError(
                        f"Data at index {i} has required argument {arg} set to None"
                    )
        return True

    def _log_evaluation_request(self, data) -> str | None:
        """
        将使用情况记录到 Fi，用于分析，并创建评测请求。
        """
        eval_request_id = None
        try:
            eval_request_id = FiLoggingHelper.create_eval_request(
                eval_name=self.name, request_data={"data": data}, request_type="batch"
            )
        except Exception:
            pass
        return eval_request_id


    def _log_evaluation_results(
        self, eval_request_id: str | None, eval_results: list[EvalResult]
    ):
        """
        如果 eval_request_id 可用，则将评测结果记录到 Fi。
        """
        if eval_request_id:
            try:
                FiLoggingHelper.log_eval_results(
                    eval_request_id=eval_request_id,
                    eval_results=eval_results,
                )
            except Exception:
                pass

    def run(self, **kwargs) -> BatchRunResult:
        """
        运行 LLM 评测器，并将结果记录到 Fi。
        """
        logger.info(
            f"base_evaluator_run evaluator={self.name} display_name={self.display_name} input_keys={list(kwargs.keys())}"
        )
        FiApiService.log_usage(eval_name=self.name, run_type="batch")
        eval_request_id = self._log_evaluation_request(kwargs)
        eval_result = self._evaluate(**kwargs)
        # self._log_evaluation_results(
        #     eval_request_id=eval_request_id, eval_results=[eval_result]
        # )

        return BatchRunResult(
            eval_request_id=eval_request_id,
            eval_results=[eval_result],
        )

    def guard(self, **kwargs):
        """
        以 guard 模式运行评测，并返回通过/失败结果。
        """
        eval_result = self._evaluate(**kwargs)
        passed = not eval_result["failure"]
        reason = eval_result["reason"]
        runtime = eval_result["runtime"]
        return GuardResult(passed=passed, reason=reason, runtime=runtime)

    def _run_batch_generator_async(
        self, data: list[DataPoint], max_parallel_evals: int
    ):
        # 用 OTel 上下文传播包装 _evaluate，保证线程安全。
        # 这样 trace 上下文可以从 Temporal activity 流入线程池 worker。
        wrapped_evaluate = wrap_for_thread(self._evaluate)

        with ThreadPoolExecutor(max_workers=max_parallel_evals) as executor:
            # 提交所有任务，并记录它们在原始数据中的索引。
            future_to_index = {
                executor.submit(wrapped_evaluate, **entry): i
                for i, entry in enumerate(data)
            }

            # 按原始顺序存放结果。
            results: list[EvalResult | None] = [None] * len(data)

            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as e:
                    entry = data[index]
                    logger.error(f"Error running batch async {entry}: {e}")
                    traceback.print_exc()
                    results[index] = None

            return results

    def _run_batch_generator(self, data: list[DataPoint]):
        """
        批量运行评测的生成器。
        遍历数据集，并对每一项运行评测器。
        """
        for entry in data:
            try:
                yield self._evaluate(**entry)
            except Exception as e:
                logger.error(f"Error evaluating entry {entry}: {e}")
                traceback.print_exc()
                yield None

    def _log_dataset_to_Fi(self, data: list[DataPoint]) -> Dataset | None:
        """
        将数据集记录到 Fi。
        """
        try:
            dataset = Dataset.create(
                name=generate_unique_dataset_name(),
                rows=data
            )
            return dataset
        except Exception as e:
            logger.error(f"Error logging dataset to Fi: {e}")
            return None

    def _log_eval_results_to_Fi(self, eval_results: list[EvalResult], dataset_id: str):
        """
        将批量评测结果记录到 Fi。
        """
        try:
            eval_config = self.to_config()
            llm_engine = getattr(self, "_model", None)
            FiLoggingHelper.log_eval_results_with_config(
                eval_results_with_config={
                    "eval_results": eval_results,
                    "development_eval_config": {
                        "eval_type_id": self.name,
                        "eval_display_name": generate_eval_display_name(self.display_name),
                        "eval_config": eval_config,
                        "llm_engine": llm_engine
                    }
                },
                dataset_id=dataset_id
            )
        except Exception as e:
            logger.error(f"Error logging eval results to Fi: {e}")
            pass

    def run_batch(
        self, data: list[DataPoint], max_parallel_evals: int = 5, upload_to_fi: bool = True
    ) -> BatchRunResult:
        """
        对一批数据运行评测器。
        """
        # 将使用情况记录到 Fi，用于分析。
        FiApiService.log_usage(eval_name=self.name, run_type="batch")

        # 运行评测。
        if max_parallel_evals > 1:
            eval_results = self._run_batch_generator_async(data, max_parallel_evals)
        else:
            eval_results = list(self._run_batch_generator(data))

        # 创建数据集。
        if upload_to_fi:
            dataset = self._log_dataset_to_Fi(data)
            if dataset:
                self._log_eval_results_to_Fi(eval_results, dataset.id)
        else:
            logger.warning("Upload to Fi is disabled")

        return BatchRunResult(
            eval_results=eval_results,
        )
