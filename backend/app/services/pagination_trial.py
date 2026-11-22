"""分页试采边界校验：从真实响应样本判定分页边界、重复 token、断点续采与联调验收。

背景
----
接口联调过去用固定 JSON 路径登记解析规则，分页响应只要出现「零行」就容易被当成
尾页。实际接口常见两类边界同时出现：

* 零行页仍携带有效 ``nextToken`` —— 这是**可续页**，不是尾页；
* 后续接口重复返回同一个 ``nextToken`` —— 这是**循环边界**，不能再请求，也不能
  把同一页的数据重复累计。

本模块把这些判定集中成一条可复现、可恢复、可验收的试采链路：

1. ``run`` / ``resume`` 逐页核对响应样本，产出 page / boundary / credential 轨迹；
2. 空页、确认尾页、重复 token 分别判定，重复 token 达到安全上限后停止循环取数；
3. 每次核对都保存「下一页 + 顺序 + 凭据」，中断后 ``resume`` 按原页序续取；
4. ``accept`` 把判定结果写成 Schema 字段差异，并生成与试采清单一一对应的投产待办。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Callable

from app.store import store

_MISSING = object()

# 历史分页约定：不同接口对数据、token、是否还有下一页的命名并不统一。
TOKEN_FIELDS = (
    "nextToken", "next_token", "nextPageToken", "next_page_token",
    "nextCursor", "next_cursor", "next", "cursor", "token",
)
DATA_FIELDS = ("data", "rows", "items", "records", "list", "results", "content", "values")
HAS_MORE_FIELDS = ("hasMore", "has_more", "hasNext", "has_next", "more")
TOTAL_FIELDS = ("total", "totalCount", "total_count", "count", "totalElements")
PAGE_SIZE_FIELDS = ("pageSize", "page_size", "size", "limit", "perPage", "per_page")
ENVELOPE_FIELDS = ("data", "result", "payload", "body", "response", "content")
HEADER_TOKEN_FIELDS = (
    "X-Next-Token", "x-next-token", "X-Next-Cursor", "x-next-cursor",
    "X-Cursor", "x-cursor", "Next-Token", "next-token",
)

BOUNDARY_LABELS = {
    "DATA": "有数据且可续页",
    "DATA_TAIL": "有数据且已确认尾页",
    "EMPTY_WITH_NEXT": "零行但 nextToken 有效（可续页，不是尾页）",
    "EMPTY_TAIL": "零行且无 nextToken（已确认尾页）",
    "EMPTY_UNRESOLVED": "零行且 hasMore=true 但无可用 token（待验证）",
    "DUPLICATE_TOKEN": "重复 nextToken（已抑制再次请求）",
    "REPEAT_LIMIT": "重复 nextToken 达到安全上限（停止循环取数）",
    "FETCH_ERROR": "本页取数失败（保留凭据待续采）",
    "NO_SAMPLE": "缺少后续响应样本（保留凭据待续采）",
}
TAIL_BOUNDARIES = ("DATA_TAIL", "EMPTY_TAIL")

TODO_TABLE = "pagination_todo"
SESSION_TABLE = "pagination_trial"


def _today() -> str:
    return date.today().isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().casefold()
    if lowered in ("true", "1", "yes", "y"):
        return True
    if lowered in ("false", "0", "no", "n", ""):
        return False
    return None


def _body_of(sample: Any) -> tuple[Any, dict[str, Any]]:
    """取响应主体和响应头；样本既可以是裸响应体，也可以是 {body, headers}。"""
    if not isinstance(sample, dict):
        return sample, {}
    headers = sample.get("headers") if isinstance(sample.get("headers"), dict) else {}
    for key in ("body", "response", "payload"):
        if key in sample:
            return sample[key], headers
    return sample, headers


def _parse_path(path: str) -> list[str]:
    path = str(path or "").strip()
    if path in ("", "$"):
        return []
    if path.startswith("$"):
        path = path[1:]
    tokens: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            index += 1
            continue
        if char == "[":
            end = path.find("]", index)
            if end == -1:
                tokens.append(path[index + 1:])
                break
            tokens.append(path[index + 1:end].strip().strip("'\""))
            index = end + 1
            continue
        next_dot = path.find(".", index)
        next_bracket = path.find("[", index)
        candidates = [pos for pos in (next_dot, next_bracket) if pos != -1]
        end = min(candidates) if candidates else len(path)
        tokens.append(path[index:end])
        index = end
    return [token for token in tokens if token != ""]


def _walk(value: Any, tokens: list[str]) -> Any:
    if not tokens:
        return value
    head, rest = tokens[0], tokens[1:]
    if head in ("*", "[]"):
        if isinstance(value, dict):
            value = list(value.values())
        if not isinstance(value, list):
            return _MISSING
        gathered: list[Any] = []
        for item in value:
            result = _walk(item, rest)
            if result is _MISSING:
                continue
            if isinstance(result, list):
                gathered.extend(result)
            else:
                gathered.append(result)
        return gathered
    if isinstance(value, list):
        if head.lstrip("-").isdigit():
            position = int(head)
            if -len(value) <= position < len(value):
                return _walk(value[position], rest)
        return _MISSING
    if isinstance(value, dict):
        if head not in value:
            return _MISSING
        return _walk(value[head], rest)
    return _MISSING


def _find_first(node: Any, fields: tuple[str, ...], depth: int = 0) -> tuple[Any, str]:
    """按约定字段名在响应体里由浅到深查找，返回值和实际字段路径。"""
    if depth > 4 or not isinstance(node, dict):
        return _MISSING, ""
    for field in fields:
        if field in node and node[field] is not None:
            return node[field], field
    for key, value in node.items():
        if isinstance(value, dict):
            found, path = _find_first(value, fields, depth + 1)
            if found is not _MISSING:
                return found, f"{key}.{path}" if path else key
    return _MISSING, ""


def _find_rows(node: Any, depth: int = 0) -> tuple[list[Any] | None, str]:
    if depth > 4:
        return None, ""
    if isinstance(node, list):
        return node, ""
    if not isinstance(node, dict):
        return None, ""
    for field in DATA_FIELDS:
        if field in node:
            value = node[field]
            if isinstance(value, list):
                return value, field
            if isinstance(value, dict):
                rows, sub = _find_rows(value, depth + 1)
                if rows is not None:
                    return rows, f"{field}.{sub}" if sub else field
    for key, value in node.items():
        if isinstance(value, (dict, list)):
            rows, sub = _find_rows(value, depth + 1)
            if rows is not None:
                return rows, f"{key}.{sub}" if sub else key
    return None, ""


def extract_records(
    sample: Any,
    json_path: str | None = None,
    data_field: str | None = None,
) -> tuple[list[Any], str]:
    """从响应样本提取记录列表，同时兼容显式路径和历史字段约定。"""
    payload, _headers = _body_of(sample)
    if isinstance(json_path, str) and json_path.strip() not in ("", "$"):
        found = _walk(payload, _parse_path(json_path))
        if found is not _MISSING:
            if isinstance(found, list):
                return found, json_path
            if isinstance(found, dict):
                rows, sub = _find_rows(found)
                if rows is not None:
                    return rows, f"{json_path}.{sub}" if sub else json_path
                return [found], json_path
    if isinstance(data_field, str) and data_field.strip():
        found = _walk(payload, _parse_path(data_field))
        if isinstance(found, list):
            return found, data_field
        if isinstance(found, dict):
            rows, sub = _find_rows(found)
            if rows is not None:
                return rows, f"{data_field}.{sub}" if sub else data_field
            return [found], data_field
    rows, field = _find_rows(payload)
    if rows is not None:
        return rows, field
    if isinstance(payload, dict):
        # data 是单个对象、没有数组字段时，按单条记录处理，而不是误判为空页。
        for candidate in DATA_FIELDS:
            if isinstance(payload.get(candidate), dict):
                return [payload[candidate]], candidate
    return [], ""


def extract_token(
    sample: Any,
    token_field: str | None = None,
) -> tuple[str | None, str]:
    payload, headers = _body_of(sample)
    if isinstance(token_field, str) and token_field.strip():
        found = _walk(payload, _parse_path(token_field))
        if found is not _MISSING:
            value = _text(found)
            return (value or None), token_field
        for name in (token_field, *HEADER_TOKEN_FIELDS):
            if name in headers:
                value = _text(headers.get(name))
                return (value or None), f"headers.{name}"
    for name in HEADER_TOKEN_FIELDS:
        if name in headers:
            value = _text(headers.get(name))
            if value:
                return value, f"headers.{name}"
    found, path = _find_first(payload, TOKEN_FIELDS)
    if found is not _MISSING:
        value = _text(found)
        if value and value.casefold() not in ("null", "none", "undefined"):
            return value, path
    # 响应体可能是 {data: {...}}，再往下一层找一次。
    if isinstance(payload, dict):
        for envelope in ENVELOPE_FIELDS:
            if isinstance(payload.get(envelope), dict):
                found, path = _find_first(payload[envelope], TOKEN_FIELDS)
                if found is not _MISSING:
                    value = _text(found)
                    if value and value.casefold() not in ("null", "none", "undefined"):
                        return value, f"{envelope}.{path}"
    return None, ""


def extract_has_more(sample: Any) -> bool | None:
    payload, _headers = _body_of(sample)
    found, _path = _find_first(payload, HAS_MORE_FIELDS)
    if found is _MISSING:
        if isinstance(payload, dict):
            for envelope in ENVELOPE_FIELDS:
                if isinstance(payload.get(envelope), dict):
                    found, _path = _find_first(payload[envelope], HAS_MORE_FIELDS)
                    if found is not _MISSING:
                        break
    return _bool_or_none(found)


def extract_total(sample: Any) -> int | None:
    payload, _headers = _body_of(sample)
    found, _path = _find_first(payload, TOTAL_FIELDS)
    try:
        return int(found) if found is not _MISSING and str(found).strip() else None
    except (TypeError, ValueError):
        return None


def _record_key(row: Any) -> str:
    """按记录内容生成键：重复 token 返回同一批数据时可以据此去重。"""
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def classify_boundary(row_count: int, next_token: str | None, has_more: bool | None) -> str:
    """纯函数：只依据本页行数、token 和 hasMore 判定边界。"""
    if row_count > 0:
        return "DATA" if next_token else "DATA_TAIL"
    if next_token:
        return "EMPTY_WITH_NEXT"
    if has_more is True:
        return "EMPTY_UNRESOLVED"
    return "EMPTY_TAIL"


class PaginationTrial:
    """一次分页试采的状态机，可序列化后中断恢复。"""

    def __init__(self, config: dict[str, Any], state: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.state = state or self._initial_state()
        self.page_samples: list[Any] = []
        if self.state.get("__sample__") is not None:
            self.page_samples = list(self.state["__sample__"])

    # ---------- 状态 ----------
    def _initial_state(self) -> dict[str, Any]:
        return {
            "trial_id": self.config.get("trial_id") or "",
            "endpoint_id": self.config.get("endpoint_id") or self.config.get("target_endpoint") or "",
            "rule_id": self.config.get("rule_id"),
            "token_field": self.config.get("token_field") or "",
            "json_path": self.config.get("json_path") or "",
            "data_field": self.config.get("data_field") or "",
            "max_pages": int(self.config.get("max_pages") or 50),
            "repeat_limit": max(2, int(self.config.get("repeat_limit") or 2)),
            "start_token": self.config.get("start_token"),
            "pages": [],
            "records": [],
            "requests": 0,
            "requested_set": [],
            "seen_set": [],
            "tokens_requested": [],
            "token_requests": {},
            "occurrences": {},
            "stop_reason": "",
            "next_page": 1,
            "next_token": self.config.get("start_token"),
            "resume_from": self.config.get("resume_from") or {},
            "status": "pending",
            "interrupted": False,
            "resumed": False,
            "last_confirmed_page": 0,
            "last_confirmed_rows": 0,
        }

    def export_state(self) -> dict[str, Any]:
        state = dict(self.state)
        state["__sample__"] = list(self.page_samples)
        return state

    @classmethod
    def from_state(cls, config: dict[str, Any], state: dict[str, Any]) -> "PaginationTrial":
        clean = dict(state or {})
        samples = clean.pop("__sample__", None)
        trial = cls(config, clean)
        if samples:
            trial.page_samples = list(samples)
        return trial

    @property
    def next_page(self) -> int:
        return int(self.state.get("next_page") or 1)

    @property
    def next_token(self) -> str | None:
        token = self.state.get("next_token")
        return _text(token) or None

    def credential(self) -> dict[str, Any]:
        """续采所需凭据：页码、顺序、token、请求序号，便于中断后原样续取。

        旧实现把上一份凭据嵌在 ``pending_credential`` 里，恢复时容易形成嵌套。这里直接
        拍平：待验证页自身的上下文放在 ``resume_from``，不会再自我引用。
        """
        return {
            "trial_id": self.state.get("trial_id") or "",
            "endpoint_id": self.state.get("endpoint_id") or "",
            "next_page": self.next_page,
            "next_sequence": self.state.get("requests", 0) + 1,
            "next_token": self.next_token,
            "token_field": self.state.get("token_field") or "",
            "max_pages": self.state.get("max_pages"),
            "repeat_limit": self.state.get("repeat_limit"),
            "resume_from": dict(self.state.get("resume_from") or {}),
        }

    def describe(self) -> dict[str, Any]:
        pages = list(self.state.get("pages") or [])
        inspected = [p for p in pages if p.get("boundary") not in ("FETCH_ERROR", "NO_SAMPLE")]
        source_page = None
        for page in reversed(inspected):
            if page.get("boundary") in TAIL_BOUNDARIES:
                source_page = page.get("page")
                break
        if source_page is None and inspected:
            source_page = inspected[-1].get("page")
        confirmed_source = inspected[-1].get("page") if inspected else None
        return {
            "trial_id": self.state.get("trial_id") or "",
            "endpoint_id": self.state.get("endpoint_id") or "",
            "status": self.state.get("status"),
            "stop_reason": self.state.get("stop_reason") or "",
            "stop_reason_label": BOUNDARY_LABELS.get(self.state.get("stop_reason", ""), ""),
            "pages": pages,
            "page_count": len(pages),
            "pages_fetched": len({p.get("page") for p in pages if p.get("boundary") != "NO_SAMPLE"}),
            "row_count": len(self.state.get("records") or []),
            "total_rows": len(self.state.get("records") or []),
            "requests": self.state.get("requests", 0),
            "records": list(self.state.get("records") or []),
            "credential": self.credential(),
            "interrupted": bool(self.state.get("interrupted")),
            "resumed": bool(self.state.get("resumed")),
            "last_confirmed_page": self.state.get("last_confirmed_page", 0),
            "last_confirmed_row_count": self.state.get("last_confirmed_rows", 0),
            "final_source_page": source_page,
            "confirmed_source_page": confirmed_source,
            "tail_confirmed": bool(inspected) and inspected[-1].get("boundary") in TAIL_BOUNDARIES,
            "empty_page_seen": any(p.get("row_count") == 0 and p.get("boundary") == "EMPTY_WITH_NEXT" for p in pages),
            "empty_tail_pages": [p.get("page") for p in pages if p.get("boundary") == "EMPTY_TAIL"],
            "duplicate_tokens": [
                p.get("token") for p in pages if p.get("boundary") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT")
            ],
            "duplicate_pages": [
                p.get("page") for p in pages if p.get("boundary") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT")
            ],
            "token_requests": dict(self.state.get("token_requests") or {}),
            "records_after_first_empty": self._records_after_first_empty(pages),
            "loop_stopped": self.state.get("stop_reason") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT"),
        }

    def _records_after_first_empty(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """首个零行页之后仍有效采集到的记录：用于核对「第二页后的数据没有丢」。"""
        empty_pages = [p.get("page") for p in pages if p.get("row_count") == 0 and p.get("boundary") == "EMPTY_WITH_NEXT"]
        if not empty_pages:
            return []
        first_empty = min(empty_pages)
        return [
            record for record in (self.state.get("records") or [])
            if int(record.get("page") or 0) > int(first_empty)
        ]

    # ---------- 核对一页 ----------
    def _record_rows(self, page: int, rows: list[Any], *, dedupe: bool = False) -> int:
        records = self.state.setdefault("records", [])
        known = {item.get("key") for item in records}
        added = 0
        for index, row in enumerate(rows):
            key = _record_key(row)
            if dedupe and key in known:
                continue
            records.append({
                "page": page,
                "index": index + 1,
                "key": key,
                "data": row,
            })
            known.add(key)
            added += 1
        return added

    def _append_page(self, page: dict[str, Any]) -> None:
        self.state.setdefault("pages", []).append(page)

    def _mark_confirmed(self, page_no: int, row_count: int) -> None:
        self.state["last_confirmed_page"] = page_no
        self.state["last_confirmed_rows"] = row_count

    def _resume_context(self, credential: dict[str, Any], page_no: int, token: str | None) -> dict[str, Any]:
        """待验证页的续采上下文：页、顺序、token，不嵌套自身。"""
        return {
            "page": page_no,
            "sequence": credential.get("next_sequence"),
            "token": token,
            "request_token": credential.get("next_token"),
        }

    def inspect(
        self,
        sample: Any,
        *,
        page_no: int | None = None,
        credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """核对一页响应样本但不推进状态；返回本页判定，供预览和逐页校验复用。"""
        page_no = int(page_no or self.next_page)
        rows, data_path = extract_records(
            sample, self.state.get("json_path"), self.state.get("data_field"))
        token, token_path = extract_token(sample, self.state.get("token_field"))
        has_more = extract_has_more(sample)
        boundary = classify_boundary(len(rows), token, has_more)
        return {
            "page": page_no,
            "row_count": len(rows),
            "data_path": data_path,
            "token": token,
            "token_path": token_path,
            "has_more": has_more,
            "boundary": boundary,
            "boundary_label": BOUNDARY_LABELS.get(boundary, ""),
            "credential": credential if credential is not None else self.credential(),
            "rows": rows,
        }

    def step(self, sample: Any) -> dict[str, Any]:
        """推进一页：判定边界、累计记录、维护 token 循环计数、生成续采凭据。"""
        page_no = self.next_page
        incoming = self.credential()
        # 恢复成功时，用本次真实结果替换中断留下的占位页，避免同一页出现两条待办。
        self.state["pages"] = [
            page for page in self.state.get("pages", [])
            if not (page.get("page") == page_no and page.get("boundary") in ("FETCH_ERROR", "NO_SAMPLE"))
        ]
        info = self.inspect(sample, page_no=page_no, credential=incoming)
        rows = info["rows"]
        boundary = info["boundary"]
        token = info["token"]
        has_more = info["has_more"]

        page_entry: dict[str, Any] = {
            "page": page_no,
            "sequence": incoming["next_sequence"],
            "row_count": len(rows),
            "boundary": boundary,
            "boundary_label": info["boundary_label"],
            "token": token,
            "token_path": info["token_path"],
            "data_path": info["data_path"],
            "has_more": has_more,
            "request_token": incoming["next_token"],
            "credential": incoming,
        }

        # 重复 token：同一 token 第二次出现即确认是循环边界。
        # 本页只取一次、不重复累计（按记录键去重），之后也不再发起新的取数请求。
        if token and token in self.state.get("seen_set", []):
            occurrence = self._bump_occurrence(token)
            limit = max(2, int(self.state.get("repeat_limit") or 2))
            reached_limit = occurrence >= limit
            added = self._record_rows(page_no, rows, dedupe=True)
            page_entry["boundary"] = "REPEAT_LIMIT" if reached_limit else "DUPLICATE_TOKEN"
            page_entry["boundary_label"] = BOUNDARY_LABELS[page_entry["boundary"]]
            page_entry["occurrence"] = occurrence
            page_entry["repeat_limit"] = limit
            page_entry["row_count"] = added
            page_entry["raw_row_count"] = len(rows)
            page_entry["recorded"] = added
            page_entry["duplicate_suppressed"] = len(rows) - added
            self._append_page(page_entry)
            self.state["stop_reason"] = page_entry["boundary"]
            self.state["status"] = "stopped"
            # 保留「重复 token 所在页 + 请求序号 + 该 token」作为恢复定位信息。
            self.state["resume_from"] = self._resume_context(incoming, page_no, token)
            self.state["next_page"] = page_no
            if reached_limit:
                self.state["next_token"] = None
            else:
                self.state["next_token"] = token
            return self.describe()

        if token:
            self.state.setdefault("seen_set", []).append(token)
            self.state.setdefault("occurrences", {})[token] = 1

        self.state["requests"] = int(self.state.get("requests", 0)) + 1
        self.state.setdefault("requested_set", []).append(page_no)
        # token 只在真正发起取数时计数，重复 token 被抑制后不会再次累计。
        if incoming["next_token"]:
            self.state.setdefault("tokens_requested", []).append(incoming["next_token"])
            self.state.setdefault("token_requests", {})[incoming["next_token"]] = (
                int(self.state.setdefault("token_requests", {}).get(incoming["next_token"], 0)) + 1
            )

        recorded = self._record_rows(page_no, rows)
        page_entry["recorded"] = recorded
        self._append_page(page_entry)

        if boundary in TAIL_BOUNDARIES:
            self._mark_confirmed(page_no, len(rows))
            self.state["stop_reason"] = boundary
            self.state["status"] = "done"
            self.state["next_page"] = page_no + 1
            self.state["next_token"] = None
            self.state["resume_from"] = {}
            return self.describe()

        if boundary == "EMPTY_UNRESOLVED":
            # 零行且自称还有下一页，却没有可用 token：不能当尾页，也不能凭空请求下一页。
            # 保留本页与凭据，等接口补出 token 后从同一页续采。
            self.state["status"] = "stopped"
            self.state["stop_reason"] = "EMPTY_UNRESOLVED"
            self.state["next_page"] = page_no
            self.state["next_token"] = None
            self.state["resume_from"] = self._resume_context(incoming, page_no, token)
            return self.describe()

        # EMPTY_WITH_NEXT / DATA：继续下一页，token 与顺序一并落进凭据。
        if boundary == "EMPTY_WITH_NEXT":
            self._mark_confirmed(page_no, 0)
        self.state["status"] = "running"
        self.state["stop_reason"] = ""
        self.state["next_page"] = page_no + 1
        self.state["next_token"] = token
        self.state["resume_from"] = {}
        if page_no >= int(self.state.get("max_pages") or 50):
            self.state["status"] = "stopped"
            self.state["stop_reason"] = "MAX_PAGES"
        return self.describe()

    def _bump_occurrence(self, token: str) -> int:
        occurrences = self.state.setdefault("occurrences", {})
        occurrences[token] = int(occurrences.get(token, 1)) + 1
        return occurrences[token]


class TrialRunner:
    """把「取数回调 + 响应样本」喂给 :class:`PaginationTrial`，并处理中断与恢复。"""

    def __init__(self, trial: PaginationTrial) -> None:
        self.trial = trial

    def _fetcher_from_samples(self, samples: list[Any]):
        queue = list(samples)

        def fetch(token: str | None, page_no: int, credential: dict[str, Any]):
            if not queue:
                return _MISSING
            return queue.pop(0)

        return fetch

    def run(
        self,
        *,
        samples: list[Any] | None = None,
        fetcher: Callable[[str | None, int, dict[str, Any]], Any] | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        """按页序推进试采。

        ``fetcher`` 返回响应样本；返回 ``None`` / 抛异常表示本页取数失败，会保留凭据
        并把状态置为 ``interrupted``，之后可用同一 trial 继续 ``run``。
        """
        fetch = fetcher or self._fetcher_from_samples(samples or [])
        state = self.trial.state
        terminal = state.get("status") in ("done", "stopped") and state.get("stop_reason") in (
            *TAIL_BOUNDARIES, "DUPLICATE_TOKEN", "REPEAT_LIMIT", "MAX_PAGES",
        )
        if terminal:
            # 已经确认尾页或已熔断的试采不再取数，恢复只返回现有判定。
            return self.trial.describe()
        state["status"] = "running"
        steps = 0
        while state.get("status") not in ("done", "stopped"):
            if max_steps is not None and steps >= max_steps:
                state["status"] = "pending"
                state["interrupted"] = True
                break
            page_no = self.trial.next_page
            token = self.trial.next_token
            credential = self.trial.credential()
            try:
                sample = fetch(token, page_no, credential)
            except Exception as exc:  # noqa: BLE001 - 取数失败要落盘成可恢复状态
                self._interrupt(page_no, credential, reason="FETCH_ERROR", detail=str(exc))
                break
            if sample is _MISSING or sample is None:
                self._interrupt(page_no, credential, reason="NO_SAMPLE")
                break
            self.trial.page_samples.append(sample)
            result = self.trial.step(sample)
            steps += 1
            if result.get("stop_reason") in TAIL_BOUNDARIES or state.get("status") in ("done", "stopped"):
                # 已确认尾页或循环边界：决不再发新请求，也不再吃后续样本。
                break
        if state.get("status") == "running":
            state["status"] = "stopped"
        return self.trial.describe()

    def _interrupt(self, page_no: int, credential: dict[str, Any], *, reason: str, detail: str = "") -> None:
        state = self.trial.state
        state["status"] = "interrupted"
        state["interrupted"] = True
        state["stop_reason"] = reason
        state["resume_from"] = dict(credential)
        state["next_page"] = page_no
        state.setdefault("pages", []).append({
            "page": page_no,
            "sequence": credential.get("next_sequence"),
            "row_count": 0,
            "boundary": reason,
            "boundary_label": BOUNDARY_LABELS.get(reason, reason),
            "token": credential.get("next_token"),
            "request_token": credential.get("next_token"),
            "credential": credential,
            "detail": detail,
            "resumable": True,
        })


def _result_payload(trial: PaginationTrial) -> dict[str, Any]:
    """对外结果 = 可读摘要 + 可原样恢复的原始状态。两者刻意分开，避免恢复时丢字段。"""
    payload = trial.describe()
    payload["config"] = dict(trial.config)
    payload["raw_state"] = trial.export_state()
    return payload


def run_pagination_trial(
    config: dict[str, Any],
    samples: list[Any] | None = None,
    *,
    fetcher: Callable[[str | None, int, dict[str, Any]], Any] | None = None,
    max_steps: int | None = None,
    trial: PaginationTrial | None = None,
) -> dict[str, Any]:
    """一次性跑完（或跑到中断）的便捷入口，配置与状态都内联在返回值里。"""
    active = trial or PaginationTrial(config)
    if trial is not None and config:
        active.config.update(config)
        active.state.update({
            key: value for key, value in config.items()
            if key in ("token_field", "json_path", "data_field", "max_pages", "repeat_limit")
        })
    TrialRunner(active).run(samples=samples, fetcher=fetcher, max_steps=max_steps)
    return _result_payload(active)


def resume_pagination_trial(
    raw_state: dict[str, Any],
    config: dict[str, Any] | None = None,
    samples: list[Any] | None = None,
    *,
    fetcher: Callable[[str | None, int, dict[str, Any]], Any] | None = None,
    max_steps: int | None = None,
) -> dict[str, Any]:
    trial = PaginationTrial.from_state(config or {}, raw_state)
    trial.state["resumed"] = True
    trial.state["interrupted"] = False
    TrialRunner(trial).run(samples=samples, fetcher=fetcher, max_steps=max_steps)
    return _result_payload(trial)


# ---------------------------------------------------------------------------
# 领域服务：写入试采清单、投产待办与 Schema 字段差异
# ---------------------------------------------------------------------------

PENDING_TODO_STATES = ("待验证", "待续采", "待复核")


def _build_paths_summary(pages: list[dict[str, Any]]) -> str:
    seen: list[str] = []
    for page in pages:
        path = page.get("data_path") or ""
        if path and path not in seen:
            seen.append(path)
    return " | ".join(seen)


def _build_token_summary(pages: list[dict[str, Any]]) -> str:
    seen: list[str] = []
    for page in pages:
        path = page.get("token_path") or ""
        if path and path not in seen:
            seen.append(path)
    return " | ".join(seen)


def build_field_diffs(result: dict[str, Any]) -> list[dict[str, Any]]:
    """把试采校验结果转成 Schema 字段差异清单。"""
    pages = list(result.get("pages") or [])
    diffs: list[dict[str, Any]] = []
    data_path = _build_paths_summary(pages)
    token_path = _build_token_summary(pages)
    if data_path:
        diffs.append({
            "field": "响应数据路径",
            "old_definition": "$.data[*] 固定路径",
            "new_definition": data_path,
            "level": "INFO",
            "note": "试采样本实际命中的数据路径",
        })
    if token_path:
        diffs.append({
            "field": "分页 Token 路径",
            "old_definition": "$.nextToken 固定路径",
            "new_definition": token_path,
            "level": "INFO",
            "note": "试采样本实际命中的 token 路径",
        })
    boundaries = [p.get("boundary") for p in pages]
    empty_with_next = sum(1 for b in boundaries if b == "EMPTY_WITH_NEXT")
    empty_tail = sum(1 for b in boundaries if b == "EMPTY_TAIL")
    if empty_with_next:
        diffs.append({
            "field": "分页边界:零行页",
            "old_definition": "零行即视为尾页",
            "new_definition": f"零行但 nextToken 有效，可续页 x{empty_with_next}",
            "level": "WARNING",
            "note": "零行页不能直接当尾页，需继续取下一页",
        })
    if empty_tail:
        diffs.append({
            "field": "分页边界:确认尾页",
            "old_definition": "无后续数据",
            "new_definition": f"零行且无 nextToken，确认尾页 x{empty_tail}",
            "level": "INFO",
            "note": "与带 token 的零行页区分开，作为可分页结束的判据",
        })
    if any(b in ("DUPLICATE_TOKEN", "REPEAT_LIMIT") for b in boundaries):
        token = next((p.get("token") for p in pages if p.get("boundary") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT")), "")
        diffs.append({
            "field": "分页边界:重复 token",
            "old_definition": "token 可无限续取",
            "new_definition": f"token {token} 重复出现，已停止循环取数",
            "level": "MAJOR",
            "note": "重复 token 会导致死循环和重复累计，必须熔断",
        })
    if result.get("status") == "interrupted":
        diffs.append({
            "field": "分页边界:断点续采",
            "old_definition": "中断后从头重取",
            "new_definition": f"从第 {result.get('credential', {}).get('next_page')} 页按序续取",
            "level": "WARNING",
            "note": "中断页需保留 token 凭据以便续采",
        })
    return diffs


def build_todos(result: dict[str, Any]) -> list[dict[str, Any]]:
    """按页生成投产待办：每页一条，状态与边界判定一一对应。

    ``确认来源页`` 只指向真正核对过响应样本的页；中断占位页沿用上一页的确认来源。
    """
    todos: list[dict[str, Any]] = []
    last_source: int | None = None
    for page in result.get("pages") or []:
        boundary = page.get("boundary")
        if boundary in TAIL_BOUNDARIES:
            state = "已验收"
        elif boundary == "EMPTY_WITH_NEXT":
            state = "待复核"
        elif boundary in ("DUPLICATE_TOKEN", "REPEAT_LIMIT"):
            state = "待复核"
        elif boundary in ("FETCH_ERROR", "NO_SAMPLE", "EMPTY_UNRESOLVED"):
            state = "待续采"
        else:
            state = "待验证"
        if boundary not in ("FETCH_ERROR", "NO_SAMPLE"):
            last_source = page.get("page")
        todos.append({
            "page": page.get("page"),
            "boundary": boundary,
            "todo_state": state,
            "source_page": last_source if last_source is not None else page.get("page"),
            "confirmed_from": last_source if last_source is not None else None,
            "token": page.get("token"),
            "row_count": page.get("row_count", 0),
            "credential": page.get("credential") or {},
            "resumable": boundary in ("FETCH_ERROR", "NO_SAMPLE", "EMPTY_UNRESOLVED"),
        })
    return todos


class PaginationTrialService:
    """把试采结果落到 store，供联调验收与界面使用。"""

    MODULE = "response_parse"

    def __init__(self, store_obj=None) -> None:
        self.store = store_obj or store

    # ---- 运行与恢复 ----
    def run_trial(
        self,
        config: dict[str, Any],
        samples: list[Any] | None = None,
        *,
        fetcher: Callable[[str | None, int, dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        trial_id = str(config.get("trial_id") or f"PAGTRIAL-{len(self.store.rows(SESSION_TABLE)) + 1:04d}")
        config = {**config, "trial_id": trial_id}
        result = run_pagination_trial(config, samples, fetcher=fetcher)
        result["trial_id"] = trial_id
        return self._persist(config, result)

    def resume_trial(
        self,
        trial_id: str,
        samples: list[Any] | None = None,
        *,
        fetcher: Callable[[str | None, int, dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any] | None:
        session = self._find_session(trial_id)
        if session is None:
            return None
        raw = session.get("raw_state") or {}
        terminal = raw.get("stop_reason") in (
            *TAIL_BOUNDARIES, "DUPLICATE_TOKEN", "REPEAT_LIMIT", "MAX_PAGES",
        )
        if terminal:
            # 已验证到尾 / 已熔断的试采不重新取数，也不把验收状态刷回待办。
            return self.describe_session(trial_id) or {}
        result = resume_pagination_trial(raw, session.get("config") or {}, samples, fetcher=fetcher)
        result["trial_id"] = trial_id
        # 中断恢复按页覆盖，不重复创建待办/差异。
        return self._persist(session.get("config") or {}, result, existing=session)

    # ---- 存储 ----
    def _find_session(self, trial_id: str) -> dict[str, Any] | None:
        for row in self.store.rows(SESSION_TABLE):
            if str(row.get("试采编号")) == trial_id:
                return row
        return None

    def _persist(
        self,
        config: dict[str, Any],
        result: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sessions = self.store.rows(SESSION_TABLE)
        trial_id = str(config.get("trial_id") or result.get("trial_id"))
        session = existing or {
            "id": max((int(r.get("id", 0)) for r in sessions), default=0) + 1,
            "试采编号": trial_id,
            "status": "待采样",
        }
        session.update({
            "config": dict(config),
            # 摘要供界面展示；raw_state 是完整原始状态，恢复时逐字段回灌。
            "state": dict(result),
            "raw_state": result.get("raw_state") or {},
            "目标端点": config.get("endpoint_id") or result.get("endpoint_id") or "",
            "最后确认页": result.get("final_source_page"),
            "页数": result.get("page_count"),
            "行数": result.get("row_count"),
            "停止原因": result.get("stop_reason"),
            "token": result.get("credential", {}).get("next_token"),
            "下次页": result.get("credential", {}).get("next_page"),
            "resumed": result.get("resumed", False),
            "status": "已验收" if result.get("tail_confirmed") else (
                "待续采" if result.get("status") == "interrupted" else "待复核"),
            "pending": result.get("status") != "done",
            "abnormal": result.get("status") in ("interrupted", "stopped"),
        })
        if existing is None:
            sessions.append(session)

        self._write_todos(trial_id, result, replace=True)
        return self.describe_session(trial_id) or {}

    def _write_todos(self, trial_id: str, result: dict[str, Any], *, replace: bool = True) -> None:
        rows = self.store.rows(TODO_TABLE)
        if replace:
            rows[:] = [row for row in rows if row.get("试采编号") != trial_id]
        base = max((int(r.get("id", 0)) for r in rows), default=0)
        for index, todo in enumerate(build_todos(result), start=1):
            rows.append({
                "id": base + index,
                "试采编号": trial_id,
                "目标端点": result.get("endpoint_id") or "",
                "页": todo["page"],
                "边界判定": todo["boundary"],
                "待办状态": todo["todo_state"],
                "确认来源页": todo["source_page"],
                "token": todo.get("token") or "",
                "行数": todo["row_count"],
                "可续采": todo["resumable"],
                "凭据": todo["credential"],
                "status": todo["todo_state"],
                "pending": todo["todo_state"] in PENDING_TODO_STATES,
                "abnormal": todo["boundary"] in ("DUPLICATE_TOKEN", "REPEAT_LIMIT", "FETCH_ERROR"),
            })

    # ---- 查询 ----
    def describe_session(self, trial_id: str) -> dict[str, Any] | None:
        session = self._find_session(trial_id)
        if session is None:
            return None
        payload = dict(session)
        payload["todos"] = [t for t in self.store.rows(TODO_TABLE) if t.get("试采编号") == trial_id]
        return payload

    def list_todos(self, trial_id: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.store.rows(TODO_TABLE))
        if trial_id:
            rows = [row for row in rows if row.get("试采编号") == trial_id]
        return rows

    # ---- 验收：写 Schema 字段差异 ----
    def accept_trial(self, trial_id: str) -> dict[str, Any] | None:
        session = self._find_session(trial_id)
        if session is None:
            return None
        result = session.get("state") or {}
        diffs = build_field_diffs(result)
        diff_rows = self.store.rows("schema_diff")
        endpoint = session.get("目标端点") or result.get("endpoint_id") or ""
        # 同一试采重复验收时覆盖旧差异，保持与试采清单一致。
        existing = [row for row in diff_rows if row.get("影响评估") == f"分页试采{trial_id}验收"]
        for row in existing:
            diff_rows.remove(row)
        base = max((int(r.get("id", 0)) for r in diff_rows), default=0)
        for index, diff in enumerate(diffs, start=1):
            diff_rows.append({
                "id": base + index,
                "记录编号": f"SCHE-PAGE-{trial_id}-{index}",
                "关联端点": endpoint,
                "变更字段": diff["field"],
                "旧定义": diff["old_definition"],
                "新定义": diff["new_definition"],
                "差异等级": diff["level"],
                "影响评估": f"分页试采{trial_id}验收",
                "检测时间": _today(),
                "试采编号": trial_id,
                "status": "已检测", "pending": True, "abnormal": diff["level"] == "MAJOR",
            })
        todos = [t for t in self.store.rows(TODO_TABLE) if t.get("试采编号") == trial_id]
        # 验收口径：只有确认尾页才算通过；重复 token 是拦截项，必须保持待复核，
        # 中断/缺样本的页只能待续采，不能借验收置成「已验收」。
        blocking = result.get("loop_stopped") or result.get("stop_reason") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT")
        tail_confirmed = bool(result.get("tail_confirmed"))
        for todo in todos:
            current = todo.get("待办状态")
            if current == "已验收":
                continue
            if todo.get("边界判定") in ("DUPLICATE_TOKEN", "REPEAT_LIMIT"):
                # 重复 token 必须由人确认分页协议后另行放行。
                continue
            if tail_confirmed and not blocking and current in ("待复核", "待验证"):
                todo["待办状态"] = "已验收"
            elif current in ("待复核", "待验证"):
                todo["待办状态"] = "待复核"
            else:
                todo["待办状态"] = "待续采"
            todo["status"] = todo["待办状态"]
            todo["pending"] = todo["待办状态"] in PENDING_TODO_STATES
        accepted = [t for t in todos if t.get("待办状态") == "已验收"]
        session["status"] = "已验收" if tail_confirmed and not blocking else (
            "待复核" if blocking else "待续采")
        session["pending"] = session["status"] != "已验收"
        # 回写解析规则：只有验收通过才推进解析规则，供联调按新规则生效。
        rule_id = session.get("config", {}).get("rule_id") or result.get("rule_id")
        if rule_id is not None and tail_confirmed and not blocking:
            for rule in self.store.rows("response_parse"):
                if str(rule.get("id")) == str(rule_id) or str(rule.get("规则编号")) == str(rule_id):
                    rule["status"] = "生效中"
                    rule["pending"] = True
                    rule["abnormal"] = False
                    break
        return {
            "trial_id": trial_id,
            "endpoint_id": endpoint,
            "field_diffs": diffs,
            "todo_count": len(todos),
            "accepted_pages": [t.get("页") for t in accepted],
            "blocking": blocking,
            "final_source_page": result.get("final_source_page"),
            "tail_confirmed": tail_confirmed,
            "schema_diff_created": len(diffs),
        }

    def pagination_checklist(self, trial_id: str | None = None) -> dict[str, Any]:
        """试采清单：与投产待办逐条对齐，供联调验收看板使用。"""
        sessions = self.store.rows(SESSION_TABLE)
        if trial_id:
            sessions = [s for s in sessions if s.get("试采编号") == trial_id]
        todos = self.list_todos(trial_id)
        checklist: list[dict[str, Any]] = []
        for session in sessions:
            sid = str(session.get("试采编号"))
            state = session.get("state") or {}
            for page in (state.get("pages") or []):
                matched = next(
                    (t for t in todos if t.get("试采编号") == sid and t.get("页") == page.get("page")),
                    None,
                )
                checklist.append({
                    "trial_id": sid,
                    "page": page.get("page"),
                    "boundary": page.get("boundary"),
                    "boundary_label": page.get("boundary_label"),
                    "row_count": page.get("row_count", 0),
                    "token": page.get("token"),
                    "todo_id": matched.get("id") if matched else None,
                    "todo_state": matched.get("待办状态") if matched else "缺少待办",
                    "source_page": matched.get("确认来源页") if matched else None,
                })
        return {
            "trial_id": trial_id,
            "checklist": checklist,
            "todos": todos,
            "consistent": all(item["todo_state"] != "缺少待办" for item in checklist),
            "todo_count": len(todos),
        }


# ---------------------------------------------------------------------------
# 内置试采端点：按顺序返回「一条记录 → 空页 → 后续数据 → 重复 token」
# ---------------------------------------------------------------------------

PROBE_ENDPOINT = "ENDP-PAGE-PROBE"

# 每一页的固定剧本：第一页 1 条，第二页空页但 nextToken 有效，第三页有数据，
# 之后继续给出的仍是第三页的 token，用来触发重复 token 边界。
PROBE_PAGES: list[dict[str, Any]] = [
    {
        "data": [{"id": "row-1", "name": "首条记录"}],
        "nextToken": "probe-token-2",
        "total": 3,
    },
    {
        "data": [],
        "nextToken": "probe-token-3",
        "total": 3,
    },
    {
        "data": [{"id": "row-2", "name": "第二页后数据A"},
                 {"id": "row-3", "name": "第二页后数据B"}],
        "nextToken": "probe-token-tail",
        "total": 3,
    },
]


def probe_sample(token: str | None) -> dict[str, Any]:
    """模拟分页端点的第 N 次响应；最后一个 token 会重复返回。

    * 无 token：第 1 页，1 条记录，返回 probe-token-2；
    * token=probe-token-2：第 2 页，零行但带回 probe-token-3（不是尾页）；
    * token=probe-token-3：第 3 页，2 条数据，返回 probe-token-tail；
    * token=probe-token-tail：接口把第 3 页原样再返回一次（同样的数据和 token），
      用来形成重复 token 边界；试采应识别出循环，不重复累计这 2 条数据。
    """
    if token is None or token == "":
        return dict(PROBE_PAGES[0])
    if token == "probe-token-2":
        return dict(PROBE_PAGES[1])
    if token == "probe-token-3":
        return dict(PROBE_PAGES[2])
    # 循环端点：重复返回第三页，token 仍是自己。
    return dict(PROBE_PAGES[2])


def probe_fetcher(token: str | None, page_no: int, credential: dict[str, Any]) -> dict[str, Any]:
    """可直接传给 ``run_pagination_trial`` 的取数回调。"""
    return probe_sample(token)


# ---------------------------------------------------------------------------
# 便捷入口：内置探针与验收聚合
# ---------------------------------------------------------------------------

def run_probe_trial(
    config: dict[str, Any] | None = None,
    service: PaginationTrialService | None = None,
) -> dict[str, Any]:
    """用内置端点跑一遍「1 条 → 空页 → 后续数据 → 重复 token」的试采。"""
    svc = service or PaginationTrialService()
    payload = {
        "endpoint_id": PROBE_ENDPOINT,
        "repeat_limit": 2,
        **dict(config or {}),
    }
    payload.setdefault("trial_id", f"PROBE-{len(svc.store.rows(SESSION_TABLE)) + 1:04d}")
    return svc.run_trial(payload, fetcher=probe_fetcher)


def run_pagination_check(
    samples: list[Any],
    config: dict[str, Any] | None = None,
    *,
    persist: bool = False,
    service: PaginationTrialService | None = None,
) -> dict[str, Any]:
    """纯样本校验：不落库也能拿到边界判定、采集结果和恢复位置。"""
    cfg = dict(config or {})
    if persist:
        return (service or PaginationTrialService()).run_trial(cfg, samples)
    result = run_pagination_trial(cfg, samples)
    result["field_diffs"] = build_field_diffs(result)
    result["todos"] = build_todos(result)
    return result


def acceptance_view(trial_id: str, service: PaginationTrialService | None = None) -> dict[str, Any] | None:
    """联调验收视图：判定结果、字段差异、试采清单与投产待办对齐情况。"""
    svc = service or PaginationTrialService()
    session = svc.describe_session(trial_id)
    if session is None:
        return None
    checklist = svc.pagination_checklist(trial_id)
    state = session.get("state") or {}
    return {
        "trial_id": trial_id,
        "endpoint_id": session.get("目标端点"),
        "status": session.get("status"),
        "boundary_result": {
            "stop_reason": state.get("stop_reason"),
            "final_source_page": state.get("final_source_page"),
            "tail_confirmed": state.get("tail_confirmed"),
            "empty_page_seen": state.get("empty_page_seen"),
            "duplicate_pages": state.get("duplicate_pages"),
            "loop_stopped": state.get("loop_stopped"),
            "last_confirmed_page": state.get("last_confirmed_page"),
        },
        "collected": {
            "row_count": state.get("row_count"),
            "requests": state.get("requests"),
            "pages": [
                {
                    "page": p.get("page"),
                    "boundary": p.get("boundary"),
                    "row_count": p.get("row_count"),
                    "token": p.get("token"),
                }
                for p in (state.get("pages") or [])
            ],
        },
        "recovery": state.get("credential"),
        "checklist": checklist,
        "field_diffs": state.get("field_diffs") or build_field_diffs(state),
    }


# 常用别名：不同调用方对「边界校验」的命名习惯不同，统一指向同一实现。
check_page_boundary = classify_boundary
run_page_boundary_check = run_pagination_check
PaginationBoundaryChecker = PaginationTrial
