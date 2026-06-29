"""
ClickHouse 的 Trace List 查询构造器。

用两阶段 ClickHouse 查询策略替代 ``tracer.views.trace`` 中的
``list_traces()`` 方法：

Phase 1 -- 从反规范化 ``spans`` 表中分页读取 trace ID 和 root span 数据
（``WHERE parent_span_id IS NULL``）。

Phase 2 -- 针对这些 trace ID 从 ``tracer_eval_logger FINAL`` 读取 eval 分数，
并按 ``(trace_id, custom_eval_config_id)`` 分组。

两个结果集最终在 Python 中合并。
"""

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from tracer.services.clickhouse.query_builders.base import BaseQueryBuilder
from tracer.services.clickhouse.query_builders.filters import ClickHouseFilterBuilder

# TODO: 为 start_time 建索引后，将这里切换为 "start_time"。
TIME_FILTER_COLUMN = "created_at"  # 可选值："created_at" 或 "start_time"


class TraceListQueryBuilder(BaseQueryBuilder):
    """为分页 trace list 视图构造查询。

    Args:
        project_id: Project UUID 字符串。
        page_number: 从 0 开始的页码。
        page_size: 每页 trace 数。
        filters: 前端过滤器列表。
        sort_params: 前端排序规范列表。
        eval_config_ids: ``CustomEvalConfig`` UUID 字符串列表，用于读取 eval 分数。
    """

    TABLE = "spans"
    EVAL_TABLE = "tracer_eval_logger"

    # 前端发送的排序列名到 root span 上实际 ClickHouse 列名的映射。
    SORT_FIELD_MAP: Dict[str, str] = {
        "created_at": "start_time",
        "start_time": "start_time",
        "latency": "latency_ms",
        "latency_ms": "latency_ms",
        "cost": "cost",
        "total_tokens": "total_tokens",
        "name": "trace_name",
        "trace_name": "trace_name",
        "status": "status",
    }

    # 可配置列选择中支持的所有轻量列。
    AVAILABLE_COLUMNS: List[str] = [
        "trace_id",
        "trace_name",
        "name",
        "observation_type",
        "status",
        "start_time",
        "end_time",
        "latency_ms",
        "cost",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "model",
        "provider",
        "trace_session_id",
        "project_id",
    ]

    def __init__(
        self,
        project_id: Optional[str] = None,
        project_ids: Optional[List[str]] = None,
        page_number: int = 0,
        page_size: int = 50,
        filters: Optional[List[Dict]] = None,
        sort_params: Optional[List[Dict]] = None,
        eval_config_ids: Optional[List[str]] = None,
        project_version_id: Optional[str] = None,
        search: Optional[str] = None,
        columns: Optional[List[str]] = None,
        annotation_label_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(project_id=project_id, project_ids=project_ids, **kwargs)
        self.page_number = page_number
        self.page_size = page_size
        self.filters = filters or []
        self.sort_params = sort_params or []
        self.eval_config_ids = eval_config_ids or []
        self.project_version_id = project_version_id
        self.search = search.strip() if search else None
        self.columns = columns
        self.annotation_label_ids = annotation_label_ids or []
        self.start_date: Optional[datetime] = None
        self.end_date: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Phase 1：分页 trace list。
    # ------------------------------------------------------------------

    def build(self) -> Tuple[str, Dict[str, Any]]:
        """构造 Phase-1 查询，用于分页读取 root-span trace 数据。

        Returns:
            ``(query_string, params)`` 元组。查询结果每个 trace 一行，
            包含 root-span 元数据。
        """
        self.start_date, self.end_date = self.parse_time_range(self.filters)
        self.params["start_date"] = self.start_date
        self.params["end_date"] = self.end_date

        # 转换属性 / 指标过滤器。
        fb = ClickHouseFilterBuilder(
            table=self.TABLE,
            annotation_label_ids=self.annotation_label_ids,
        )
        extra_where, extra_params = fb.translate(self.filters)
        self.params.update(extra_params)

        # 排序。
        order_clause = fb.translate_sort(
            self.sort_params, field_map=self.SORT_FIELD_MAP
        )
        if not order_clause:
            order_clause = "ORDER BY start_time DESC"

        # 分页。
        offset = self.page_number * self.page_size
        self.params["limit"] = self.page_size + 1  # +1 用于判断 has_more。
        self.params["offset"] = offset

        # 构造可选过滤片段。
        filter_fragment = f"AND {extra_where}" if extra_where else ""

        # 可选 project_version_id 过滤器，用于 prototype tab。
        pv_fragment = ""
        if self.project_version_id:
            pv_fragment = "AND project_version_id = %(project_version_id)s"
            self.params["project_version_id"] = self.project_version_id

        # trace_name 搜索过滤器。
        search_fragment = ""
        if self.search:
            search_fragment = "AND trace_name ILIKE %(search)s"
            self.params["search"] = f"%{self.search}%"

        # 可配置列：只 SELECT 请求的列。
        # trace_id 始终包含。
        if self.columns:
            valid = [c for c in self.columns if c in self.AVAILABLE_COLUMNS]
            if "trace_id" not in valid:
                valid.insert(0, "trace_id")
            # 为向后兼容，将 'name' 别名为 'span_name'。
            select_cols = []
            for c in valid:
                if c == "name":
                    select_cols.append("name AS span_name")
                else:
                    select_cols.append(c)
            select_clause = ",\n            ".join(select_cols)
        else:
            select_clause = """trace_id,
            trace_name,
            name AS span_name,
            observation_type,
            status,
            start_time,
            end_time,
            latency_ms,
            cost,
            total_tokens,
            prompt_tokens,
            completion_tokens,
            model,
            provider,
            trace_session_id,
            project_id"""

        # Phase 1 只取轻量列，不取 input/output/span_attr/metadata。
        # 重列由 build_content_query() 针对已返回的 trace_ids 再读取，
        # 以避免大表上 OOM。
        #
        # `created_at` 是分区/排序键（`PARTITION BY toYYYYMM(created_at)`，
        # `ORDER BY (project_id, toDate(created_at), trace_id, id)`）。
        # 只给 `created_at` 添加下界，可以让 ClickHouse 裁剪旧分区。
        # 如果没有这个条件，仅靠现有 `start_time` 过滤会触发项目全表扫描，
        # 因为 `start_time` 没有索引。`start_time` 仍作为语义边界，
        # 确保用户可见时间戳精确符合筛选条件。
        #
        # 不给 `created_at` 加上界：生产数据表明约 0.5% 的行会晚到 7 天以上
        # （SDK 缓冲、回填、手动上传）。上界会静默丢掉这些行。
        # 下界保留 1 天 buffer 用于容忍时钟偏差。这已经获得全部分区裁剪收益；
        # 测试中上界没有额外收益，因为没有行的 `created_at` 在未来。
        #
        # 在一个 350 万 span 的项目上，7 天 page-1 查询从 663ms/350 万行
        # 降到 256ms/30.6 万行，约 2.5 倍提速，I/O 减少 91%。
        query = f"""
        SELECT
            {select_clause}
        FROM {self.TABLE}
        {self.project_where()}
          AND (parent_span_id IS NULL OR parent_span_id = '')
          AND created_at >= %(start_date)s - INTERVAL 1 DAY
          AND {TIME_FILTER_COLUMN} >= %(start_date)s
          AND {TIME_FILTER_COLUMN} < %(end_date)s
          {pv_fragment}
          {search_fragment}
          {filter_fragment}
        {order_clause}
        LIMIT 1 BY trace_id
        LIMIT %(limit)s
        OFFSET %(offset)s
        """
        return query, self.params

    def build_content_query(self, trace_ids: List[str]) -> Tuple[str, Dict[str, Any]]:
        """为当前页 trace 拉取重列：input、output、attributes。

        这里用 trace_id 做 PREWHERE 点查，只读取 Phase 1 已经分页出来的 trace。
        这样避免为了列表页在整张宽表上扫描 input/output 等大字段。
        """
        if not trace_ids:
            return "", {}

        params: Dict[str, Any] = {
            **self.params,
            "content_trace_ids": tuple(trace_ids),
        }

        query = f"""
        SELECT
            trace_id,
            input,
            output,
            span_attr_str,
            span_attr_num,
            metadata_map,
            trace_tags
        FROM {self.TABLE}
        PREWHERE trace_id IN %(content_trace_ids)s
        WHERE {self.project_filter_sql()}
          AND _peerdb_is_deleted = 0
          AND (parent_span_id IS NULL OR parent_span_id = '')
        LIMIT 1 BY trace_id
        """
        return query, params

    def build_span_attributes_query(
        self, trace_ids: List[str]
    ) -> Tuple[str, Dict[str, Any]]:
        """读取当前页每个 trace 下所有 span 的原始属性。

        返回结果会在 API 层按 trace 聚合，用于列表页展示跨 span 的属性筛选信息。
        原始大字段仍保持懒加载，避免列表查询过重。
        """
        if not trace_ids:
            return "", {}

        params = {**self.params, "attr_trace_ids": tuple(trace_ids)}
        query = f"""
        SELECT
            trace_id,
            span_attributes_raw
        FROM {self.TABLE}
        PREWHERE trace_id IN %(attr_trace_ids)s
        WHERE {self.project_filter_sql()}
          AND _peerdb_is_deleted = 0
          AND span_attributes_raw != '{{}}'
          AND span_attributes_raw != ''
        """
        return query, params

    def build_count_query(self) -> Tuple[str, Dict[str, Any]]:
        """构造分页总数查询。

        Returns:
            返回 ``(query_string, params)``，查询结果是一行总数。
        """
        fb = ClickHouseFilterBuilder(
            table=self.TABLE,
            annotation_label_ids=self.annotation_label_ids,
        )
        extra_where, extra_params = fb.translate(self.filters)
        # 合并参数，并复用 build() 中解析出的 start/end 时间。
        params = dict(self.params)
        params.update(extra_params)

        filter_fragment = f"AND {extra_where}" if extra_where else ""

        # 可选 project_version_id 过滤器。
        pv_fragment = ""
        if self.project_version_id:
            pv_fragment = "AND project_version_id = %(project_version_id)s"
            params["project_version_id"] = self.project_version_id

        # 搜索过滤器，和 build() 保持一致。
        search_fragment = ""
        if self.search:
            search_fragment = "AND trace_name ILIKE %(search)s"
            params["search"] = f"%{self.search}%"

        # 参考 build() 的说明：created_at 只加下界用于裁剪旧分区。
        # 在一个 350 万 span 项目上，7 天 count 从 716ms/350 万行降到
        # 255ms/30.6 万行，同时不丢失用户 start_time 窗口内的合法数据。

        query = f"""
        SELECT uniq(trace_id) AS total
        FROM {self.TABLE}
        {self.project_where()}
          AND (parent_span_id IS NULL OR parent_span_id = '')
          AND created_at >= %(start_date)s - INTERVAL 1 DAY
          AND {TIME_FILTER_COLUMN} >= %(start_date)s
          AND {TIME_FILTER_COLUMN} < %(end_date)s
          {pv_fragment}
          {search_fragment}
          {filter_fragment}
        """
        return query, params

    # ------------------------------------------------------------------
    # 每个 trace 的 span 计数；仅当请求列包含 span_count 时才需要。
    # ------------------------------------------------------------------

    def build_span_count_query(
        self, trace_ids: List[str]
    ) -> Tuple[str, Dict[str, Any]]:
        """统计当前页每个 trace 的 span 总数和错误 span 数。"""
        if not trace_ids:
            return "", {}

        params: Dict[str, Any] = {
            **self.params,
            "sc_trace_ids": tuple(trace_ids),
        }
        query = f"""
        SELECT
            trace_id,
            count() AS span_count,
            countIf(status = 'ERROR') AS error_count
        FROM {self.TABLE}
        WHERE {self.project_filter_sql()}
          AND trace_id IN %(sc_trace_ids)s
          AND _peerdb_is_deleted = 0
        GROUP BY trace_id
        """
        return query, params

    @staticmethod
    def pivot_span_count_results(
        data: List[Dict],
    ) -> Dict[str, Dict[str, int]]:
        """将 span 计数结果转换为 ``{trace_id: {span_count, error_count}}``。"""
        result: Dict[str, Dict[str, int]] = {}
        for row in data:
            tid = str(row.get("trace_id", ""))
            if tid:
                result[tid] = {
                    "span_count": row.get("span_count", 0),
                    "error_count": row.get("error_count", 0),
                }
        return result

    # ------------------------------------------------------------------
    # Phase 2：读取当前页 trace 的 eval 分数。
    # ------------------------------------------------------------------

    def build_eval_query(
        self,
        trace_ids: List[str],
    ) -> Tuple[str, Dict[str, Any]]:
        """构造 Phase 2 eval 分数查询。

        查询 ``tracer_eval_logger FINAL``，按 ``(trace_id, custom_eval_config_id)``
        聚合，最终每个 trace + eval config 得到一行分数。

        Args:
            trace_ids: Phase 1 返回的 trace ID 列表。

        Returns:
            返回 ``(query_string, params)``。如果 trace_ids 或 eval_config_ids
            为空，则返回空查询。
        """
        if not trace_ids or not self.eval_config_ids:
            return "", {}

        params: Dict[str, Any] = {
            "trace_ids": tuple(trace_ids),
            "eval_config_ids": tuple(self.eval_config_ids),
        }

        # 错误行也纳入查询，但聚合分数只统计成功行（error = 0）。
        # success_count/error_count 让 pivot 阶段能区分：
        # “没有运行 eval”、“运行成功但失败”、“所有 eval 行都报错”。
        # str_lists 保存非错误行的 output_str_list，用于 CHOICES eval 的选项占比。
        #
        # output_str 是 Nullable(String)，多数 evaluator 会留空。
        # ClickHouse 三值逻辑中 NULL != 'ERROR' 结果是 NULL 而不是 TRUE；
        # 如果直接写 output_str != 'ERROR'，会把 output_str 为 NULL 的正常行排除，
        # 导致 success_count 变 0、avg_score/pass_rate 变 NaN、trace list 上 eval 列为空。
        # 因此这里用 ifNull(...) 保持 NULL-safe 比较。
        query = f"""
        SELECT
            trace_id,
            toString(custom_eval_config_id) AS eval_config_id,
            -- ifNotFinite(..., NULL)：全 NULL 分组上的 avgIf 会返回 NaN，
            -- json.dumps(allow_nan=False) 会拒绝 NaN；NULL 可以序列化成 null。
            ifNotFinite(avgIf(
                output_float,
                error = 0 AND ifNull(output_str, '') != 'ERROR'
            ), NULL) AS avg_score,
            ifNotFinite(avgIf(
                CASE WHEN output_bool = 1 THEN 100.0 ELSE 0.0 END,
                error = 0 AND ifNull(output_str, '') != 'ERROR'
            ), NULL) AS pass_rate,
            countIf(
                error = 0 AND ifNull(output_str, '') != 'ERROR'
            ) AS success_count,
            countIf(
                error = 1 OR ifNull(output_str, '') = 'ERROR'
            ) AS error_count,
            count() AS eval_count,
            groupArrayIf(
                output_str_list,
                error = 0 AND ifNull(output_str, '') != 'ERROR'
            ) AS str_lists
        FROM {self.EVAL_TABLE} FINAL
        WHERE _peerdb_is_deleted = 0
          AND (deleted = 0 OR deleted IS NULL)
          AND trace_id IN %(trace_ids)s
          AND custom_eval_config_id IN %(eval_config_ids)s
        GROUP BY trace_id, custom_eval_config_id
        """
        return query, params

    # ------------------------------------------------------------------
    # Phase 3：读取当前页 trace 的 annotation。
    # ------------------------------------------------------------------

    ANNOTATION_TABLE = "model_hub_score"

    def build_annotation_query(
        self,
        trace_ids: List[str],
        annotation_label_ids: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """为当前页 trace 构造 annotation 查询。"""
        if not trace_ids or not annotation_label_ids:
            return "", {}

        params: Dict[str, Any] = {
            "trace_ids": tuple(trace_ids),
            "label_ids": tuple(annotation_label_ids),
        }

        query = f"""
        SELECT
            if(
                isNull(s.trace_id)
                OR s.trace_id = toUUID('00000000-0000-0000-0000-000000000000'),
                sp.trace_id,
                toString(s.trace_id)
            ) AS trace_id,
            toString(s.label_id) AS label_id,
            anyLast(s.value) AS value,
            toString(anyLast(s.annotator_id)) AS annotator_id
        FROM {self.ANNOTATION_TABLE} AS s FINAL
        LEFT JOIN {self.TABLE} AS sp
          ON sp.id = s.observation_span_id
         AND sp._peerdb_is_deleted = 0
        WHERE s._peerdb_is_deleted = 0
          AND s.deleted = false
          AND if(
                isNull(s.trace_id)
                OR s.trace_id = toUUID('00000000-0000-0000-0000-000000000000'),
                sp.trace_id,
                toString(s.trace_id)
              ) IN %(trace_ids)s
          AND s.label_id IN %(label_ids)s
        GROUP BY trace_id, label_id
        """
        return query, params

    def build_user_id_query(
        self, trace_ids: List[str]
    ) -> Tuple[str, Dict[str, Any]]:
        """从 ClickHouse 为当前页 trace 读取 user_id 字符串。

        通过 enduser_dict 在一次查询中把 end_user_id UUID 解析为 user_id。
        每个 trace 返回一个 user_id，使用 any() 从该 trace 的所有 span 中
        选择第一个非空值。
        """
        if not trace_ids:
            return "", {}

        params: Dict[str, Any] = {
            **self.params,
            "user_trace_ids": tuple(trace_ids),
        }

        query = f"""
        SELECT trace_id, user_id
        FROM (
            SELECT
                trace_id,
                dictGetOrDefault('enduser_dict', 'user_id', any(end_user_id), '') AS user_id
            FROM {self.TABLE}
            PREWHERE trace_id IN %(user_trace_ids)s
            WHERE {self.project_filter_sql()}
              AND _peerdb_is_deleted = 0
              AND end_user_id IS NOT NULL
              AND end_user_id != toUUID('00000000-0000-0000-0000-000000000000')
            GROUP BY trace_id
        )
        WHERE user_id != ''
        """
        return query, params

    def resolve_user_ids(
        self, trace_ids: List[str], analytics
    ) -> Dict[str, str]:
        """解析当前页 trace 的 user_id 字符串。

        使用 ClickHouse enduser_dict 单查询完成：
        - 通过 dictionary lookup 查询 user_id 字符串，通常约 50-100ms。
        - 不需要再回查 PostgreSQL。

        Args:
            trace_ids: 需要解析用户的 trace ID 字符串列表。
            analytics: 用于执行 ClickHouse 查询的 Analytics service 实例。

        Returns:
            trace_id 到 user_id 字符串的映射。
        """
        if not trace_ids:
            return {}

        user_query, user_params = self.build_user_id_query(trace_ids)
        if not user_query:
            return {}

        result = analytics.execute_ch_query(
            user_query, user_params, timeout_ms=10000
        )

        # 构造 trace_id -> user_id 映射；过滤条件已经在查询中应用。
        user_id_map = {
            str(row.get("trace_id", "")): row.get("user_id")
            for row in result.data
            if row.get("user_id")
        }

        return user_id_map

    @staticmethod
    def pivot_annotation_results(
        annotation_rows: List[Dict],
        label_types: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """将 annotation 查询结果转换为以 trace_id 为 key 的结构。

        Returns:
            ``{trace_id: {label_id: annotation_value}}``.
        """
        import json

        label_types = label_types or {}
        result: Dict[str, Dict[str, Any]] = {}
        for row in annotation_rows:
            trace_id = str(row.get("trace_id", ""))
            label_id = str(row.get("label_id", ""))
            label_type = label_types.get(label_id, "").lower()

            raw_val = row.get("value", "{}")
            if isinstance(raw_val, str):
                try:
                    val = json.loads(raw_val)
                except (json.JSONDecodeError, TypeError):
                    val = {}
            else:
                val = raw_val if isinstance(raw_val, dict) else {}

            if label_type in ("numeric", "star"):
                value_key = "value" if label_type == "numeric" else "rating"
                value = val.get(value_key) if isinstance(val, dict) else val
            elif label_type == "thumbs_up_down":
                thumb_val = val.get("value") if isinstance(val, dict) else val
                value = thumb_val in (True, "up", 1, "true")
            elif label_type == "categorical":
                value = val.get("selected", []) if isinstance(val, dict) else val
            elif label_type == "text":
                value = val.get("text", val) if isinstance(val, dict) else val
            else:
                value = val

            result.setdefault(trace_id, {})[label_id] = value

        return result

    # ------------------------------------------------------------------
    # 结果合并。
    # ------------------------------------------------------------------

    @staticmethod
    def pivot_eval_results(
        eval_rows: List[Tuple],
        eval_columns: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """将 eval 查询结果透视为以 trace_id 为 key 的嵌套 dict。

        Args:
            eval_rows: Phase 2 eval 查询返回的行。
            eval_columns: eval_rows 对应的列名。

        Returns:
            ``{trace_id: {eval_config_id: score_dict}}`` 结构。
        """
        result: Dict[str, Dict[str, Any]] = {}
        col_idx = {name: i for i, name in enumerate(eval_columns)}

        def _get(row, key, idx, default=None):
            if isinstance(row, dict):
                return row.get(key, default)
            return (
                row[col_idx.get(key, idx)]
                if len(row) > col_idx.get(key, idx)
                else default
            )

        import json as _json

        for row in eval_rows:
            trace_id = str(_get(row, "trace_id", 0, ""))
            config_id = str(_get(row, "eval_config_id", 1, ""))
            avg_score = _get(row, "avg_score", 2)
            pass_rate = _get(row, "pass_rate", 3)
            success_count = _get(row, "success_count", 4, 0) or 0
            error_count = _get(row, "error_count", 5, 0) or 0
            str_lists = _get(row, "str_lists", 7, []) or []

            # 所有行都报错时，显式返回 error 标记。
            # UI 可以据此展示错误状态，并和“没有运行 eval”区分开。
            if success_count == 0 and error_count > 0:
                result.setdefault(trace_id, {})[config_id] = {"error": True}
                continue

            # CHOICES eval：对当前 (trace, config) 的非错误 eval 行统计每个选项占比。
            # 调用方会展开成 ``{config_id}**{choice}`` 列。
            #
            # ClickHouse 把 ``output_str_list`` 存成 ``String DEFAULT '[]'``。
            # 非 CHOICES eval（Pass/Fail、score）会返回字符串 ``'[]'``，
            # 它在 Python 中为 truthy，容易绕过 ``if not sl`` 检查。
            # 因此只有真正包含选项值的条目才当作 CHOICES 数据；空列表要继续走
            # ``avg_score``/``pass_rate`` 分支。
            parsed = []
            for sl in str_lists:
                if not sl:
                    continue
                if isinstance(sl, list):
                    if sl:
                        parsed.append([str(x) for x in sl])
                elif isinstance(sl, str) and sl.startswith("["):
                    try:
                        p = _json.loads(sl)
                        if isinstance(p, list) and p:
                            parsed.append([str(x) for x in p])
                    except _json.JSONDecodeError:
                        continue
            if parsed:
                total = len(parsed)
                counts: Dict[str, int] = {}
                for lst in parsed:
                    for choice in set(lst):
                        counts[choice] = counts.get(choice, 0) + 1
                per_choice = {
                    k: round(100.0 * v / total, 2) for k, v in counts.items()
                }
                result.setdefault(trace_id, {})[config_id] = {
                    "per_choice": per_choice,
                }
                continue

            # ClickHouse 的 ``avgIf`` 在没有行满足条件或匹配值全为 NULL 时会返回 NaN。
            # Python 中 ``bool(float('nan'))`` 为 True，所以普通 ``if avg_score``
            # 会把 NaN 泄漏进 JSON 响应，并触发 DRF 严格编码器报错。
            # 这里显式过滤非有限数值。
            def _finite(v):
                return (
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(v)
                )

            score_data = {
                "avg_score": (
                    round(avg_score * 100, 2) if _finite(avg_score) else None
                ),
                "pass_rate": (
                    round(pass_rate, 2) if _finite(pass_rate) else None
                ),
                "count": _get(row, "eval_count", 6, 0) or 0,
            }
            result.setdefault(trace_id, {})[config_id] = score_data

        return result
