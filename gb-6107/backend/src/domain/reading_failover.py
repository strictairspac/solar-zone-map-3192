"""主库/备库读数故障切换与页级续跑的纯 Python 领域逻辑。

这里不依赖 FastAPI 或数据库，方便验收用假读数库直接驱动。页码对外尽量同时
兼容 0 基下标和 1 基页号；服务层与界面统一使用 1 基页号。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil
from typing import Any, Callable, Iterable

from src.utils.serial import content_digest

from .columnar import Page, Zone, build_pages
from .predicate import Predicate

PRIMARY = "primary"
STANDBY = "standby"
# 常见别名统一到 standby，避免验收方按 replica/backup 命名时拿不到同一状态。
BACKUP = STANDBY
REPLICA = STANDBY

SOURCE_CODES = {PRIMARY, STANDBY}
SOURCE_LABELS = {
    PRIMARY: "主库",
    STANDBY: "备库",
    "backup": "备库",
    "replica": "备库",
    "snapshot": "接单快照",
    "mixed": "主库、备库",
}


class ReadingError(RuntimeError):
    """读数来源相关错误的基类。"""


class SourceDisconnected(ReadingError):
    def __init__(self, source: str = PRIMARY, message: str | None = None):
        self.source = normalize_source(source)
        super().__init__(message or f"{SOURCE_LABELS.get(self.source, source)}连接中断")


class PrimaryDisconnected(SourceDisconnected):
    def __init__(self, message: str | None = None):
        super().__init__(PRIMARY, message)


class PageNotArrived(ReadingError):
    """必读页尚未到达备库/主库；这不是缺测（null reading）。"""

    def __init__(self, pages: Iterable[int] | int | None = None, source: str | None = None,
                 message: str | None = None):
        self.pages = normalize_page_numbers(pages)
        self.page_indexes = [page - 1 for page in self.pages]
        self.source = normalize_source(source) if source else None
        where = SOURCE_LABELS.get(self.source, self.source or "读数库")
        detail = f"第 {('、'.join(map(str, self.pages)))} 页" if self.pages else "必读页"
        super().__init__(message or f"{where}{detail}尚未到达，抽检单留队等待")


class RequiredPagesMissing(PageNotArrived):
    pass


class ReadingInterrupted(ReadingError):
    """读到一半被中断；cursor 是已经安全提交的页数（下一次从该 0 基页继续）。"""

    def __init__(self, cursor: int = 0, message: str | None = None):
        self.cursor = max(0, int(cursor))
        super().__init__(message or f"读数在第 {self.cursor + 1} 页附近中断，可从检查点续跑")


class SnapshotMismatch(ReadingError):
    """备库的板号、瓦数或 Zone Map 与接单时钉住的读数不一致。"""


# 常见验收命名别名。
PrimaryDatabaseDown = PrimaryDisconnected
PrimaryUnavailable = PrimaryDisconnected
PageMissing = PageNotArrived
MissingPage = PageNotArrived
PagesNotReady = RequiredPagesMissing
RequiredPageMissing = RequiredPagesMissing
ReadInterrupted = ReadingInterrupted
ReadingPaused = ReadingInterrupted
CheckpointInterrupt = ReadingInterrupted


def normalize_source(value: str | None) -> str:
    if isinstance(value, dict):
        value = value.get("side") or value.get("name") or value.get("type") or value.get("role")
    if value is None:
        return PRIMARY
    text = str(value).strip().lower()
    aliases = {
        "primary": PRIMARY, "main": PRIMARY, "master": PRIMARY, "主库": PRIMARY,
        "主": PRIMARY, "primary_db": PRIMARY,
        "standby": STANDBY, "backup": STANDBY, "replica": STANDBY,
        "secondary": STANDBY, "备库": STANDBY, "备用库": STANDBY, "副本库": STANDBY,
        "从库": STANDBY,
    }
    return aliases.get(text, text)


def normalize_page_numbers(values: Iterable[int] | int | None) -> list[int]:
    if values is None:
        return []
    if isinstance(values, int):
        raw = [values]
    elif isinstance(values, str):
        raw = [part for part in values.replace("，", ",").split(",") if part.strip()]
    else:
        raw = list(values)
    pages: list[int] = []
    for value in raw:
        if isinstance(value, str):
            text = value.strip().lower().removeprefix("page:").removeprefix("p").removesuffix("页")
            if not text:
                continue
            value = int(text)
        page = int(value)
        # 领域外部配置一般使用 1 基页号；0 只可能是误传的 0 基下标。
        if page == 0:
            page = 1
        if page < 0:
            page = -page
        if page not in pages:
            pages.append(page)
    return sorted(pages)


def indexes_to_page_numbers(indexes: Iterable[int]) -> list[int]:
    return sorted({int(index) + 1 for index in indexes})


def source_label(code: str | None) -> str:
    if not code:
        return "未知读数来源"
    if code == "mixed":
        return SOURCE_LABELS["mixed"]
    return SOURCE_LABELS.get(code, str(code))


@dataclass(frozen=True)
class FetchedPage:
    index: int
    source: str
    serials: tuple[str, ...]
    zone: Zone
    power: tuple[int | None, ...] | None = None
    watts_available: bool = True

    @property
    def page_number(self) -> int:
        return self.index + 1

    def to_columnar_page(self) -> Page:
        power = self.power if self.power is not None else tuple(None for _ in self.serials)
        return Page(self.serials, power, self.zone)


@dataclass
class DictReadingSource:
    """测试/教学用读数库适配器。

    records 是接单快照中的 ``(serial, power_cw)``；生产适配器可把 fetch_page
    换成 MySQL/PostgreSQL/对象存储读取，状态机保持不变。
    """

    name: str = PRIMARY
    records: list[tuple[str, int | None]] = field(default_factory=list)
    page_size: int = 4
    online: bool = True
    disconnected: bool | None = None
    down: bool | None = None
    missing_indexes: set[int] = field(default_factory=set)
    missing_pages: Iterable[int] | None = None
    zone_only_indexes: set[int] = field(default_factory=set)
    interrupt_before_index: int | None = None
    interrupt_after_reads: int | None = None
    disconnect_after_reads: int | None = None
    page_overrides: dict[int, FetchedPage] = field(default_factory=dict)
    attempts: int = 0
    reads: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self.disconnected or self.down:
            self.online = False
        normalized_records = []
        for row in self.records:
            if isinstance(row, dict):
                normalized_records.append((row["serial"], row.get("power_cw")))
            else:
                normalized_records.append((row[0], row[1]))
        self.records = normalized_records
        self.name = normalize_source(self.name)
        self.missing_indexes = {int(i) for i in self.missing_indexes}
        if self.missing_pages is not None:
            self.missing_indexes.update(page - 1 for page in normalize_page_numbers(self.missing_pages))
        self.missing_pages = None

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any], name: str = PRIMARY, **options) -> "DictReadingSource":
        records = [(row["serial"], row["power_cw"]) for row in snapshot["records"]]
        return cls(name=name, records=records, page_size=int(snapshot["parameter"]), **options)

    @property
    def pages(self) -> list[Page]:
        return build_pages(self.records, self.page_size)

    def disconnect(self):
        self.online = False
        self.events.append({"type": "disconnect"})

    def connect(self):
        self.online = True
        self.events.append({"type": "connect"})

    def fetch_page(self, index: int, pinned: Page | None = None,
                   predicate: Predicate | None = None) -> FetchedPage:
        index = int(index)
        self.attempts += 1
        if not self.online:
            raise SourceDisconnected(self.name)
        if self.interrupt_before_index is not None and index >= int(self.interrupt_before_index):
            raise ReadingInterrupted(index)
        if self.interrupt_after_reads is not None and self.reads >= int(self.interrupt_after_reads):
            raise ReadingInterrupted(index)
        if index in self.missing_indexes:
            raise PageNotArrived(index + 1, source=self.name)

        if index in self.page_overrides:
            fetched = self.page_overrides[index]
        else:
            page = self.pages[index]
            zone_only = index in self.zone_only_indexes
            fetched = FetchedPage(
                index=index,
                source=self.name,
                serials=page.serials,
                zone=page.power_zone,
                power=None if zone_only else page.power,
                watts_available=not zone_only,
            )
        self.reads += 1
        self.events.append({"type": "read", "page": index + 1})
        if self.disconnect_after_reads is not None and self.reads >= int(self.disconnect_after_reads):
            self.online = False
        return fetched


def _predicate_config(predicate: Predicate | dict | None) -> dict[str, Any] | None:
    if predicate is None:
        return None
    if isinstance(predicate, Predicate):
        if predicate.mode == "null":
            return {"mode": "null"}
        return {"mode": "range", "low": predicate.low, "high": predicate.high}
    return dict(predicate)


def build_snapshot(payload: dict[str, Any] | None = None, parameter: int | None = None,
                   records: list[tuple[str, int | None]] | list[dict] | None = None,
                   predicate: Predicate | dict | None = None) -> dict[str, Any]:
    """生成接单时钉住的板号、瓦数、谓词和逐页 Zone Map/位图摘要。"""
    # 延迟导入，避免 domain 包初始化时的循环依赖。
    from .scenario import normalize

    if payload is not None and isinstance(payload, dict) and "test_records" not in payload \
            and isinstance(payload.get("payload"), dict):
        # 允许直接传入 cases 表行/mapping。
        payload = payload["payload"]

    if records is None:
        if payload is None:
            raise ValueError("需要 payload 或 records")
        rows, parsed_predicate = normalize(payload)
        parameter = int(parameter or payload.get("parameter") or 4)
        predicate = parsed_predicate
    else:
        rows = []
        for row in records:
            if isinstance(row, dict):
                rows.append((row["serial"], row.get("power_cw")))
            else:
                rows.append((row[0], row[1]))
        if predicate is None and payload is not None:
            _, predicate = normalize(payload)
        if isinstance(predicate, dict):
            predicate = Predicate.parse(predicate)
        parameter = int(parameter or 4)

    parameter = max(1, min(256, int(parameter)))
    pages = build_pages(rows, parameter)
    predicate_dict = _predicate_config(predicate)
    manifest_pages = []
    for index, page in enumerate(pages):
        zone = page.power_zone
        manifest_pages.append({
            "index": index,
            "page_number": index + 1,
            "count": zone.count,
            "minimum": zone.minimum,
            "maximum": zone.maximum,
            "valid_mask": zone.valid_mask,
            "nulls": zone.nulls,
            "serial_digest": content_digest(list(page.serials)),
            "power_digest": content_digest(list(page.power)),
            "zone_digest": content_digest(asdict(zone)),
            "page_digest": content_digest({
                "serials": list(page.serials), "power": list(page.power),
                "zone": asdict(zone),
            }),
        })
    snapshot = {
        "schema": "flash-reading-snapshot/v1",
        "parameter": parameter,
        "page_size": parameter,
        "record_count": len(rows),
        "page_count": len(pages),
        "records": [{"serial": serial, "power_cw": power} for serial, power in rows],
        "predicate": predicate_dict,
        "records_digest": content_digest(rows),
        "manifest": {
            "page_size": parameter,
            "page_count": len(pages),
            "records_digest": content_digest(rows),
            "pages": manifest_pages,
        },
    }
    snapshot["digest"] = content_digest({
        "records_digest": snapshot["records_digest"],
        "parameter": parameter,
        "predicate": predicate_dict,
    })
    return snapshot


def snapshot_pages(snapshot: dict[str, Any]) -> list[Page]:
    records = [(row["serial"], row["power_cw"]) for row in snapshot["records"]]
    return build_pages(records, int(snapshot["parameter"]))


def initial_progress() -> dict[str, Any]:
    return {
        "cursor": 0,
        "matches": [],
        "rows": [],
        "page_sources": {},
        "read_watts": 0,
        "read_pages": 0,
        "primary_unavailable": False,
        "failover_count": 0,
        "processed_pages": [],
        "attempts": 0,
    }


def restore_progress(value: dict[str, Any] | None) -> dict[str, Any]:
    progress = initial_progress()
    if isinstance(value, dict):
        progress.update({key: value[key] for key in progress if key in value})
        # 兼容直接存 cursor/checkpoint 的调用方。
        if "cursor" in value:
            progress["cursor"] = int(value["cursor"])
        elif "checkpoint" in value:
            progress["cursor"] = int(value["checkpoint"])
        if "page_sources" in value:
            progress["page_sources"] = {str(k): v for k, v in value["page_sources"].items()}
    progress["cursor"] = max(0, int(progress.get("cursor") or 0))
    return progress


def verify_page(fetched: FetchedPage, pinned: Page, *, require_watts: bool):
    if tuple(fetched.serials) != tuple(pinned.serials):
        raise SnapshotMismatch(f"第 {fetched.page_number} 页板号与接单快照不一致")
    actual_zone = fetched.zone
    expected_zone = pinned.power_zone
    if (actual_zone.count != expected_zone.count or
            actual_zone.valid_mask != expected_zone.valid_mask or
            actual_zone.minimum != expected_zone.minimum or
            actual_zone.maximum != expected_zone.maximum):
        raise SnapshotMismatch(f"第 {fetched.page_number} 页 Zone Map/位图与接单依据不一致")
    if require_watts:
        if not fetched.watts_available or fetched.power is None:
            raise PageNotArrived(fetched.page_number, source=fetched.source,
                                 message=f"{source_label(fetched.source)}第 {fetched.page_number} 页瓦数尚未到齐")
        if tuple(fetched.power) != tuple(pinned.power):
            raise SnapshotMismatch(f"第 {fetched.page_number} 页瓦数与接单快照不一致")


def _evidence_row(index: int, page: Page, action: str, offsets: list[int],
                  source_code: str, source_note: str = "") -> dict[str, Any]:
    zone = page.power_zone
    serials = [page.serials[offset] for offset in offsets]
    source_text = source_label(source_code)
    if source_note:
        source_text = f"{source_text}（{source_note}）"
    return {
        "页段": index + 1,
        "最小厘瓦": zone.minimum,
        "最大厘瓦": zone.maximum,
        "空值行": zone.nulls,
        "有效位图": format(zone.valid_mask, f"0{zone.count}b"),
        "动作": action,
        "命中数": len(offsets),
        "板件": "、".join(serials),
        "读数来源": source_text,
        "来源": source_code,
    }


def _source_order(primary: DictReadingSource | None, standby: DictReadingSource | None,
                  progress: dict[str, Any]) -> list[DictReadingSource]:
    if primary is None and standby is None:
        raise ReadingError("至少需要一个读数来源")
    if progress.get("primary_unavailable") and standby is not None:
        return [standby]
    ordered = [source for source in (primary, standby) if source is not None]
    return ordered


def advance_one(snapshot: dict[str, Any], progress: dict[str, Any] | None = None,
                primary: DictReadingSource | None = None,
                standby: DictReadingSource | None = None,
                sources: dict[str, DictReadingSource] | None = None) -> dict[str, Any]:
    """推进恰好一页并返回新的 progress；调用方负责逐页持久化。"""
    if sources:
        primary = primary or sources.get(PRIMARY)
        standby = standby or sources.get(STANDBY) or sources.get("backup") or sources.get(REPLICA)
    state = restore_progress(progress)
    state["attempts"] = int(state.get("attempts", 0)) + 1
    pages = snapshot_pages(snapshot)
    cursor = int(state["cursor"])
    if cursor >= len(pages):
        state["status"] = "done"
        return state

    predicate = Predicate.parse(snapshot.get("predicate") or {"mode": "range", "low": 0, "high": 500000})
    page = pages[cursor]
    zone = page.power_zone
    may_match = predicate.page_may_match(zone)

    if not may_match:
        # 跳页只使用接单时钉住的 Zone Map/位图，不要求主/备库传来原始瓦数页。
        offsets: list[int] = []
        action = "跳过"
        if predicate.mode == "null":
            action = "跳过（位图无空值）"
        row = _evidence_row(cursor, page, action, offsets, "snapshot", "按接单 Zone Map 跳页，未取瓦数")
        state["rows"].append(row)
        state["page_sources"][str(cursor + 1)] = "snapshot"
        state["processed_pages"].append(cursor + 1)
        state["cursor"] = cursor + 1
        state["status"] = "advanced" if state["cursor"] < len(pages) else "done"
        return state

    require_watts = predicate.mode == "range"
    candidates = _source_order(primary, standby, state)
    fetched: FetchedPage | None = None
    used: DictReadingSource | None = None
    primary_error: Exception | None = None

    for candidate in candidates:
        try:
            fetched = candidate.fetch_page(cursor, page, predicate)
            verify_page(fetched, page, require_watts=require_watts)
            used = candidate
            break
        except (SourceDisconnected, PageNotArrived, SnapshotMismatch) as exc:
            if candidate.name == PRIMARY and standby is not None and candidate is not standby:
                primary_error = exc
                state["primary_unavailable"] = True
                if not state.get("failover_reported"):
                    state["failover_count"] += 1
                    state["failover_reported"] = True
                continue
            if isinstance(exc, PageNotArrived):
                missing = sorted(set(exc.pages or [cursor + 1]))
                raise RequiredPagesMissing(missing, source=exc.source) from exc
            raise

    if fetched is None or used is None:
        if primary_error:
            raise primary_error
        raise SourceDisconnected(STANDBY, "没有可用的备库读数")

    offsets = predicate.select_offsets(
        fetched.power if fetched.power is not None else page.power, fetched.zone
    )
    action = "扫描读数" if predicate.mode == "range" else "扫描位图"
    note = "主库断线后切换" if used.name == STANDBY and state.get("primary_unavailable") else ""
    row = _evidence_row(cursor, page, action, offsets, used.name, note)
    state["rows"].append(row)
    state["matches"].extend(page.serials[offset] for offset in offsets)
    state["page_sources"][str(cursor + 1)] = used.name
    state["processed_pages"].append(cursor + 1)
    state["read_pages"] = int(state.get("read_pages", 0)) + 1
    if predicate.mode == "range":
        state["read_watts"] = int(state.get("read_watts", 0)) + zone.valid_mask.bit_count()
    state["cursor"] = cursor + 1
    state["failover_reported"] = False
    state["status"] = "advanced" if state["cursor"] < len(pages) else "done"
    return state


def run_until_blocked(snapshot: dict[str, Any], primary: DictReadingSource | None = None,
                      standby: DictReadingSource | None = None,
                      progress: dict[str, Any] | None = None,
                      sources: dict[str, DictReadingSource] | None = None,
                      max_pages: int | None = None,
                      on_page: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """纯领域驱动：一直跑到完成、缺页等待或中断。"""
    state = restore_progress(progress)
    processed_this_call = 0
    while int(state["cursor"]) < int(snapshot["page_count"]):
        if max_pages is not None and processed_this_call >= max_pages:
            state["status"] = "checkpoint"
            break
        state = advance_one(snapshot, state, primary=primary, standby=standby, sources=sources)
        processed_this_call += 1
        if on_page:
            on_page(state)
    if int(state["cursor"]) >= int(snapshot["page_count"]):
        state["status"] = "done"
    return state


def _source_summary(snapshot: dict[str, Any], state: dict[str, Any],
                    primary: DictReadingSource | None,
                    standby: DictReadingSource | None) -> dict[str, Any]:
    page_sources = state.get("page_sources", {})
    counts = {PRIMARY: 0, STANDBY: 0, "snapshot": 0}
    for code in page_sources.values():
        counts[code] = counts.get(code, 0) + 1
    read_codes = {code for code in page_sources.values() if code in SOURCE_CODES}
    if not read_codes:
        used = "snapshot"
    elif len(read_codes) == 1:
        used = next(iter(read_codes))
    else:
        used = "mixed"

    def source_info(source: DictReadingSource | None):
        if source is None:
            return None
        return {
            "source": source.name,
            "label": source_label(source.name),
            "online": source.online,
            "reads": source.reads,
            "attempts": source.attempts,
            "missing_indexes": sorted(source.missing_indexes),
            "missing_pages": indexes_to_page_numbers(source.missing_indexes),
        }

    standby_info = source_info(standby)
    skippable_missing: list[int] = []
    required_missing: list[int] = []
    if standby is not None:
        pages = snapshot_pages(snapshot)
        predicate = Predicate.parse(snapshot.get("predicate") or {"mode": "range", "low": 0, "high": 500000})
        for index in standby.missing_indexes:
            if 0 <= index < len(pages):
                may_match = predicate.page_may_match(pages[index].power_zone)
                (required_missing if may_match else skippable_missing).append(index + 1)
    return {
        "reading_source": used,
        "source": used,
        "source_used": used,
        "used_source": used,
        "active_source": used,
        "source_side": used,
        "reading_source_code": used,
        "reading_source_label": source_label(used),
        "source_label": source_label(used),
        "source_used_label": source_label(used),
        "used_sources": sorted(read_codes),
        "page_source_counts": counts,
        "primary_unavailable": bool(state.get("primary_unavailable")),
        "failover_count": int(state.get("failover_count", 0)),
        "primary": source_info(primary),
        "standby": standby_info,
        "backup": standby_info,
        "replica": standby_info,
        "standby_missing_skippable_pages": sorted(skippable_missing),
        "standby_missing_required_pages": sorted(required_missing),
        "backup_missing_skippable_pages": sorted(skippable_missing),
        "backup_missing_required_pages": sorted(required_missing),
    }


def build_receipt(snapshot: dict[str, Any], state: dict[str, Any],
                  primary: DictReadingSource | None = None,
                  standby: DictReadingSource | None = None) -> dict[str, Any]:
    """从检查点状态生成不可变回执内容。"""
    state = restore_progress(state)
    if int(state["cursor"]) < int(snapshot["page_count"]):
        raise ReadingError("回执尚未完成，不能提前签发")
    records = [(row["serial"], row["power_cw"]) for row in snapshot["records"]]
    predicate = Predicate.parse(snapshot["predicate"]) if snapshot.get("predicate") else None
    baseline = [serial for serial, power in records if predicate and predicate.matches_row(power)]
    matches = list(state.get("matches", []))
    if matches != baseline:
        raise RuntimeError("故障切换/续跑结果与逐行基线不同")

    total_pages = int(snapshot["page_count"])
    skipped = sum(1 for row in state["rows"] if str(row.get("动作", "")).startswith("跳过"))
    source_summary = _source_summary(snapshot, state, primary, standby)
    missing_skippable = source_summary["standby_missing_skippable_pages"]
    source_text = source_summary["reading_source_label"]
    if source_summary["reading_source"] == STANDBY:
        prefix = "主库断线后已切换备库，"
    elif source_summary["reading_source"] == "mixed":
        prefix = "主库中断后续跑至备库，"
    else:
        prefix = ""
    missing_text = (f"备库缺少的 {len(missing_skippable)} 个瓦数页均为本单可跳过页，"
                    f"不把缺页当缺测、照常出回执；") if missing_skippable else ""
    summary = (
        f"{prefix}{missing_text}"
        f"跳过 {skipped}/{total_pages} 个页段，命中 {len(matches)} 块试板，"
        f"与逐行校核一致；回执读数来源：{source_text}。"
    )
    receipt = {
        "summary": summary,
        "metrics": {
            "跳过页段": skipped,
            "命中试板": len(matches),
            "读取读数": int(state.get("read_watts", 0)),
            "基线行数": len(records),
            "总页段": total_pages,
            "读取页段": int(state.get("read_pages", 0)),
            "续跑检查点": int(state["cursor"]),
            "主备切换次数": int(state.get("failover_count", 0)),
            "读数来源": source_text,
        },
        "rows": state["rows"],
        "matches": matches,
        "serials": matches,
        "checkpoint": {"cursor": total_pages, "page_count": total_pages, "resumed": True},
        "snapshot": {
            "digest": snapshot["digest"],
            "records_digest": snapshot["records_digest"],
            "parameter": snapshot["parameter"],
            "page_count": total_pages,
            "predicate": snapshot.get("predicate"),
            "manifest": snapshot.get("manifest"),
        },
        "idempotent": True,
        "receipt_kind": "flash_reading_failover_receipt",
    }
    receipt.update(source_summary)
    return receipt


def execute(payload: dict[str, Any] | None = None, parameter: int = 4,
            primary: DictReadingSource | None = None,
            standby: DictReadingSource | None = None,
            missing_pages: Iterable[int] | None = None,
            standby_missing_pages: Iterable[int] | None = None,
            primary_down: bool = False,
            interrupt_after: int | None = None,
            progress: dict[str, Any] | None = None,
            snapshot: dict[str, Any] | None = None,
            sources: dict[str, DictReadingSource] | None = None,
            checkpoint: int | None = None,
            **source_aliases) -> dict[str, Any]:
    """给验收脚本/纯领域测试的一站式入口。

    支持 backup=、replica= 关键字；若未提供来源，则用 payload/快照构造与接单
    数据一致的主、备内存读数库。
    """
    if snapshot is None:
        snapshot = build_snapshot(payload, parameter)
    if sources:
        primary = primary or sources.get(PRIMARY) or sources.get("main") or sources.get("master")
        standby = (standby or sources.get(STANDBY) or sources.get("backup")
                   or sources.get(REPLICA) or sources.get("replica"))
    standby = standby or source_aliases.get("backup") or source_aliases.get("replica")
    if primary is None:
        primary = DictReadingSource.from_snapshot(snapshot, PRIMARY, online=not primary_down)
    if standby is None:
        missing_indexes = {page - 1 for page in normalize_page_numbers(
            standby_missing_pages if standby_missing_pages is not None else missing_pages
        )}
        standby = DictReadingSource.from_snapshot(
            snapshot, STANDBY, missing_indexes=missing_indexes,
            interrupt_after_reads=interrupt_after,
        )
    if progress is None and checkpoint is not None:
        progress = restore_progress({"cursor": checkpoint})
    state = run_until_blocked(
        snapshot, primary=primary, standby=standby, progress=progress, sources=sources
    )
    if state.get("status") != "done":
        if state.get("waiting_pages"):
            raise RequiredPagesMissing(state["waiting_pages"])
        raise ReadingInterrupted(state["cursor"])
    return build_receipt(snapshot, state, primary, standby)


# 面向验收脚本/不同命名习惯的函数别名。
accept_reading_order = build_snapshot
pin_reading = build_snapshot
create_reading_order = build_snapshot
create_manifest = build_snapshot
process_reading_order = execute
run_reading_order = execute
run_failover = execute
execute_failover = execute
