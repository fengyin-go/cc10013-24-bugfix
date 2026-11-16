"""采集监控业务规则：阈值版本、窗口判定、告警恢复与异常待处理区。

版本口径：
- 一条监控指标（规则）可以发布多个阈值版本。每个版本会一次性写入成功率阈值、
  延迟阈值、错误率阈值、检测频率、静默时间以及判定口径、标准范围和触发条件，
  不会只保存其中一部分。
- 窗口在登记时快照“启动阈值版本”。之后规则即使发布了新版本，该窗口的判定
  口径也只使用启动版本，避免旧窗口混入新规则。
- 新版本发布时如果存在尚未结束的窗口，会记录冲突并把优先级标为“待确认”；
  进行中的窗口继续使用启动版本，确认后的新版本只用于之后新登记的窗口。
- 失败窗口进入监控异常待处理区。告警恢复按窗口幂等：重复提交只保留一条恢复
  记录，不会重复累计；恢复前必须核对窗口使用的阈值版本。
- 同一窗口只能有一个判定版本和一个结论；跨窗样本仍标记实际判定版本，但不参与
  其他版本的窗口结论，避免同一窗口出现两套结论。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "monitor"
WINDOW_MODULE = "_monitor_window"
RECOVERY_MODULE = "_monitor_recovery"

REQUIRED_FIELDS = ["指标编号", "关联端点", "成功率阈值"]
THRESHOLD_FIELDS = ["成功率阈值", "延迟阈值", "错误率告警"]
VERSION_FIELDS = [*THRESHOLD_FIELDS, "检测频率", "静默分钟"]
DETAIL_FIELDS = ["判定口径", "标准范围", "触发条件"]
RULE_FIELDS = [*THRESHOLD_FIELDS, "检测频率", "通知渠道", "静默分钟", *DETAIL_FIELDS]

STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用监控": "已启用", "停用监控": "已停用"}
NEGATIVE_ACTIONS = ["停用监控"]

RECOVERY_ACTIONS = {"恢复告警", "恢复", "告警恢复"}
PUBLISH_ACTIONS = {"发布阈值版本", "发布阈值", "更新阈值", "保存阈值", "更新监控指标", "更新规则"}
CONFIRM_ACTIONS = {"确认阈值版本", "确认版本", "确认发布"}
REGISTER_ACTIONS = {"登记窗口", "登记阈值窗口"}
JUDGE_ACTIONS = {"判定窗口", "窗口判定", "判定"}

OPEN_STATUS = "进行中"
PASS_STATUS = "通过"
FAIL_STATUS = "失败"
RECOVERED_STATUS = "已恢复"
PENDING = "待确认"

DEFAULT_RULE_DETAIL = {
    "判定口径": "窗口内每个采样点都按窗口启动阈值版本判定，任一样本越界即判定失败。",
    "标准范围": "成功率 ≥ 成功率阈值；延迟 ≤ 延迟阈值；错误率 ≤ 错误率阈值。",
    "触发条件": "采样点成功率低于阈值、延迟高于阈值或错误率高于阈值时触发失败窗口告警。",
}

FIELD_ALIASES = {
    "latency": "延迟阈值",
    "latency_ms": "延迟阈值",
    "latency_threshold": "延迟阈值",
    "latencyThreshold": "延迟阈值",
    "success_rate": "成功率阈值",
    "successRate": "成功率阈值",
    "success_threshold": "成功率阈值",
    "error_rate": "错误率告警",
    "errorRate": "错误率告警",
    "error_threshold": "错误率告警",
    "frequency": "检测频率",
    "interval": "检测频率",
    "check_interval": "检测频率",
    "silence": "静默分钟",
    "silence_minutes": "静默分钟",
    "silenceMinutes": "静默分钟",
    "channel": "通知渠道",
    "criteria": "判定口径",
    "standard_range": "标准范围",
    "trigger": "触发条件",
    "version": "阈值版本",
    "version_no": "阈值版本",
}

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def metric_number(value: Any) -> float | None:
    """把 “850ms”“99.5%”“10 分钟” 之类的阈值读成数值，便于比较。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER_RE.search(str(value))
    return float(match.group(0)) if match else None


def _number_text(value: Any) -> str:
    number = metric_number(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return ("%f" % number).rstrip("0").rstrip(".")


def _rate_percent(value: Any) -> float | None:
    """成功率/错误率统一读成百分数：写 0.995 视为 99.5%，写 “0.1%” 保持 0.1%。"""
    number = metric_number(value)
    if number is None:
        return None
    if "%" in str(value):
        return number
    return number * 100 if number <= 1 else number


def _canonical_values(values: dict[str, Any] | None) -> dict[str, Any]:
    """把接口里常用的英文别名归一到业务字段，显式传入 0 也会保留。"""
    raw = dict(values or {})
    nested = raw.get("阈值")
    if isinstance(nested, dict):
        for key, value in nested.items():
            raw.setdefault(str(key), value)
    result: dict[str, Any] = {}
    for key, value in raw.items():
        name = FIELD_ALIASES.get(str(key), str(key))
        if name in VERSION_FIELDS or name in DETAIL_FIELDS or name == "阈值版本":
            result[name] = value
    return result


def _version_number(value: Any, fallback: int = 1) -> str:
    text = str(value or "").strip()
    if not text:
        return f"v{fallback}"
    match = re.search(r"\d+", text)
    if match:
        return f"v{int(match.group(0))}"
    return text


def _version_index(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


class MonitorService:
    # ------------------------------------------------------------------ 规则读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("指标编号", ""))
                or keyword in str(row.get("关联端点", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._public_rule(row, detail=False) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        rule = store.find(MODULE, entry_id)
        return self._public_rule(rule, detail=True) if rule else None

    def summary(self) -> dict[str, Any]:
        """指标页卡片口径：指标总量、已启用、失败窗口与待确认版本。"""
        rows = store.rows(MODULE)
        for rule in rows:
            self._ensure_rule_defaults(rule)
        failed_windows = [
            window for window in store.rows(WINDOW_MODULE) if window.get("status") == FAIL_STATUS
        ]
        pending_versions = [
            (rule, version)
            for rule in rows
            for version in rule.get("阈值版本列表", [])
            if version.get("状态") == PENDING
        ]
        return {
            "指标总数": len(rows),
            "已启用指标": sum(1 for rule in rows if rule.get("status") == "已启用"),
            "待校验指标": sum(1 for rule in rows if rule.get("status") == "待校验"),
            "异常待处理窗口": len(failed_windows),
            "待确认阈值版本": len(pending_versions),
            "静默中指标": sum(1 for rule in rows if self.silence_state(rule).get("静默中")),
        }

    def get_rule(self, reference: int | str) -> dict[str, Any] | None:
        """按 id、指标编号（MON-408）或纯数字编号定位规则。"""
        rule = self._find_rule(reference)
        return self._public_rule(rule, detail=True) if rule else None

    def _find_rule(self, reference: int | str | dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(reference, dict):
            return reference
        text = str(reference).strip()
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        upper = text.upper()
        for row in store.rows(MODULE):
            code = str(row.get("指标编号", "")).upper()
            if code == upper or code.replace("MONI-", "MON-") == upper:
                return row
        return None

    def _ensure_rule_defaults(self, rule: dict[str, Any]) -> None:
        for field, default in DEFAULT_RULE_DETAIL.items():
            if not str(rule.get(field) or "").strip():
                rule[field] = default
        if not rule.get("阈值版本列表"):
            version = self._build_version(rule, _version_number(rule.get("当前阈值版本"), 1), "已生效")
            rule["阈值版本列表"] = [version]
        if not rule.get("当前阈值版本"):
            rule["当前阈值版本"] = rule["阈值版本列表"][-1]["版本"]
        self._apply_active_version(rule)

    def _build_version(
        self,
        rule: dict[str, Any],
        version_no: str,
        status: str,
        *,
        values: dict[str, Any] | None = None,
        conflict: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {field: rule.get(field) for field in VERSION_FIELDS}
        payload.update({key: value for key, value in (values or {}).items() if key in payload})
        detail = {field: rule.get(field) for field in DETAIL_FIELDS}
        detail.update({key: value for key, value in (values or {}).items() if key in detail})
        version = {
            "版本": version_no,
            "指标编号": rule.get("指标编号"),
            **payload,
            **detail,
            "状态": status,
            "发布时间": _iso(),
            "冲突": conflict,
        }
        return version

    def _active_version(self, rule: dict[str, Any]) -> dict[str, Any] | None:
        versions = rule.get("阈值版本列表") or []
        current = rule.get("当前阈值版本")
        for version in versions:
            if version.get("版本") == current and version.get("状态") in {"已生效", "已归档"}:
                return version
        return versions[-1] if versions else None

    def _apply_active_version(self, rule: dict[str, Any]) -> None:
        version = self._active_version(rule)
        if not version:
            return
        for field in [*VERSION_FIELDS, *DETAIL_FIELDS]:
            if field in version and version[field] not in (None, ""):
                rule[field] = version[field]

    def _public_rule(self, rule: dict[str, Any], *, detail: bool) -> dict[str, Any]:
        self._ensure_rule_defaults(rule)
        data = dict(rule)
        versions = [dict(version) for version in rule.get("阈值版本列表", [])]
        data["阈值版本"] = data.get("当前阈值版本")
        data["阈值版本列表"] = versions
        data["阈值版本数"] = len(versions)
        data["检测阈值"] = {
            "成功率阈值": data.get("成功率阈值"),
            "延迟阈值": data.get("延迟阈值"),
            "错误率阈值": data.get("错误率告警"),
        }
        silence = self.silence_state(rule)
        data["静默状态"] = silence
        data["静默状态文本"] = (
            f"静默中（剩余 {silence['剩余分钟']} 分钟）" if silence.get("静默中") else "未静默"
        )
        data["窗口数"] = len(self._rule_windows(rule))
        data["失败窗口数"] = len(
            [window for window in self._rule_windows(rule) if window.get("status") == FAIL_STATUS]
        )
        recovered = [
            window for window in self._rule_windows(rule) if window.get("status") == RECOVERED_STATUS
        ]
        data["恢复窗口数"] = len(recovered)
        data["恢复记录数"] = sum(int(window.get("恢复次数") or 0) for window in recovered)
        data["最近恢复时间"] = max(
            (str(window.get("恢复时间")) for window in recovered if window.get("恢复时间")),
            default=None,
        )
        if detail:
            data["可用阈值版本"] = versions
            data["窗口列表"] = [dict(window) for window in self._rule_windows(rule)]
            data["异常待处理窗口"] = [
                dict(window) for window in self._rule_windows(rule) if window.get("status") == FAIL_STATUS
            ]
        return data

    # ------------------------------------------------------------------ 规则登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in RULE_FIELDS:
            if field in values:
                entry[field] = values.get(field)
        for field, default in DEFAULT_RULE_DETAIL.items():
            entry.setdefault(field, default)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["当前阈值版本"] = _version_number(values.get("阈值版本"), 1)
        entry["阈值版本列表"] = [self._build_version(entry, entry["当前阈值版本"], "已生效")]
        rows.append(entry)
        return self._public_rule(entry, detail=True), []

    # ------------------------------------------------------------------ 规则动作
    def run_action(
        self,
        entry_id: int | str,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        rule = self._find_rule(entry_id)
        if rule is None:
            return None, f"监控指标 {entry_id} 不存在或已归档"
        values = values or {}
        if action in PUBLISH_ACTIONS:
            return self.publish_version(
                rule,
                values,
                operator=str(values.get("operator") or values.get("操作人") or ""),
                confirm=bool(values.get("confirm") or values.get("确认")),
            )
        if action in CONFIRM_ACTIONS:
            version_no = str(values.get("阈值版本") or values.get("version") or "")
            if not version_no:
                return None, "确认阈值版本时需要提供阈值版本"
            return self.confirm_version(rule, _version_number(version_no))
        if action in REGISTER_ACTIONS:
            return self.register_window(
                rule,
                window_no=str(values.get("窗口编号") or values.get("window_id") or ""),
                version_no=str(values.get("阈值版本") or values.get("version") or ""),
                values=values,
                operator=str(values.get("operator") or values.get("操作人") or ""),
            )
        if action in JUDGE_ACTIONS:
            window_no = str(values.get("窗口编号") or values.get("window_id") or "")
            if not window_no:
                return None, "判定窗口时需要提供窗口编号"
            return self.judge_window(
                window_no,
                samples=values.get("样本记录") or values.get("samples"),
                version_no=str(values.get("阈值版本") or values.get("version") or ""),
                values=values,
            )
        if action in RECOVERY_ACTIONS:
            window_no = str(values.get("窗口编号") or values.get("window_id") or "") or rule.get("指标编号")
            return self.recover_alert(
                window_no,
                version_no=str(values.get("阈值版本") or values.get("version") or ""),
                operator=str(values.get("operator") or values.get("操作人") or ""),
                reason=str(values.get("恢复原因") or values.get("reason") or ""),
                values=values,
            )
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        rule["status"] = target
        rule["pending"] = target != STATUS_ORDER[-1]
        rule["abnormal"] = action in NEGATIVE_ACTIONS
        return self._public_rule(rule, detail=True), f"监控指标已{action}"

    # ------------------------------------------------------------------ 阈值版本
    def list_versions(self, reference: int | str) -> tuple[list[dict[str, Any]], int] | None:
        rule = self._find_rule(reference)
        if rule is None:
            return None
        self._ensure_rule_defaults(rule)
        versions = [dict(version) for version in rule.get("阈值版本列表", [])]
        return versions, len(versions)

    def publish_version(
        self,
        reference: int | str,
        values: dict[str, Any] | None = None,
        *,
        operator: str = "",
        confirm: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """发布阈值版本：阈值、检测频率和静默时间随版本一次性完整写入。"""
        rule = self._find_rule(reference)
        if rule is None:
            return None, f"监控指标 {reference} 不存在或已归档"
        self._ensure_rule_defaults(rule)
        values = values or {}
        canonical = _canonical_values(values)
        explicit_version = _canonical_values(values).get("阈值版本") or values.get("版本")

        if canonical.get("阈值版本") in (None, "") and explicit_version:
            canonical["阈值版本"] = explicit_version

        versions: list[dict[str, Any]] = rule.setdefault("阈值版本列表", [])
        last_index = max((_version_index(version.get("版本")) for version in versions), default=0)
        version_no = _version_number(
            canonical.get("阈值版本") or values.get("version") or values.get("阈值版本"),
            last_index + 1,
        )

        for version in versions:
            if version.get("版本") == version_no:
                return (
                    self._public_rule(rule, detail=True),
                    f"阈值版本 {version_no} 已存在，未重复写入",
                )

        # 新版本以当前生效版本为底稿，再覆盖本次提交的字段；所有版本字段都会落库，
        # 不会出现只保存成功率、漏掉延迟或错误率的情况。
        base = self._active_version(rule) or {}
        merged: dict[str, Any] = {}
        for field in [*VERSION_FIELDS, *DETAIL_FIELDS]:
            merged[field] = canonical.get(field, base.get(field, rule.get(field)))
        missing = [
            field
            for field in [*VERSION_FIELDS, *DETAIL_FIELDS]
            if merged.get(field) in (None, "") or str(merged.get(field)).strip() == ""
        ]
        if missing:
            return None, f"阈值版本缺少字段：{'、'.join(missing)}"

        open_windows = [
            window
            for window in self._rule_windows(rule)
            if window.get("status") == OPEN_STATUS
            and window.get("启动阈值版本") != version_no
        ]
        conflict: dict[str, Any] | None = None
        version_status = "已生效"
        if open_windows:
            conflict = {
                "优先级": PENDING,
                "说明": "新阈值发布跨越尚未结束的窗口，优先级待确认；进行中的窗口继续使用启动版本。",
                "冲突窗口": [window.get("窗口编号") for window in open_windows],
                "启动版本": sorted({str(window.get("启动阈值版本")) for window in open_windows}),
                "待确认版本": version_no,
            }
            version_status = PENDING

        version = self._build_version(
            rule,
            version_no,
            version_status,
            values=merged,
            conflict=conflict,
        )
        version["指标编号"] = rule.get("指标编号")
        version["操作人"] = operator
        versions.append(version)

        if conflict is None:
            for old in versions:
                if old is not version and old.get("状态") == "已生效":
                    old["状态"] = "已归档"
            rule["当前阈值版本"] = version_no
            self._apply_active_version(rule)
            message = f"阈值版本 {version_no} 已发布并生效，阈值、检测频率和静默时间已随版本写入"
        else:
            message = (
                f"阈值版本 {version_no} 已登记，但与进行中的窗口 "
                f"{'、'.join(conflict['冲突窗口'])} 冲突，优先级待确认；"
                "这些窗口继续使用启动版本"
            )

        if confirm and conflict is not None:
            confirmed_rule, confirm_message = self.confirm_version(rule, version_no)
            return confirmed_rule, f"{message}；{confirm_message}"
        return self._public_rule(rule, detail=True), message

    def confirm_version(
        self,
        reference: int | str | dict[str, Any],
        version_no: str,
    ) -> tuple[dict[str, Any] | None, str]:
        rule = reference if isinstance(reference, dict) else self._find_rule(reference)
        if rule is None:
            return None, f"监控指标 {version_no} 不存在或已归档"
        self._ensure_rule_defaults(rule)
        versions = rule.get("阈值版本列表", [])
        target = next((version for version in versions if version.get("版本") == version_no), None)
        if target is None:
            return None, f"阈值版本 {version_no} 不存在"
        for version in versions:
            if version is not target and version.get("状态") == "已生效":
                version["状态"] = "已归档"
        target["状态"] = "已生效"
        if isinstance(target.get("冲突"), dict):
            target["冲突"]["优先级"] = "已确认"
            target["冲突"]["确认时间"] = _iso()
        rule["当前阈值版本"] = version_no
        self._apply_active_version(rule)
        return (
            self._public_rule(rule, detail=True),
            f"阈值版本 {version_no} 已确认，之后的窗口按该版本启动；进行中的窗口仍使用各自的启动版本",
        )

    # ------------------------------------------------------------------ 窗口读写
    def _rule_windows(self, rule: dict[str, Any]) -> list[dict[str, Any]]:
        code = str(rule.get("指标编号", ""))
        return [
            row
            for row in store.rows(WINDOW_MODULE)
            if str(row.get("指标编号", "")) == code or row.get("规则id") == rule.get("id")
        ]

    def _find_window(self, reference: int | str) -> dict[str, Any] | None:
        text = str(reference).strip()
        if text.isdigit():
            found = next(
                (row for row in store.rows(WINDOW_MODULE) if int(row.get("id", 0)) == int(text)),
                None,
            )
            if found is not None:
                return found
        upper = text.upper()
        for row in store.rows(WINDOW_MODULE):
            if str(row.get("窗口编号", "")).upper() == upper:
                return row
        return None

    def _window_snapshot(self, rule: dict[str, Any], version_no: str) -> dict[str, Any]:
        self._ensure_rule_defaults(rule)
        version = next(
            (item for item in rule.get("阈值版本列表", []) if item.get("版本") == version_no),
            None,
        )
        if version is None:
            version = self._active_version(rule) or self._build_version(rule, "v1", "已生效")
        return {
            field: version.get(field, rule.get(field))
            for field in [*VERSION_FIELDS, *DETAIL_FIELDS]
        }

    def list_windows(
        self,
        *,
        rule: int | str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(WINDOW_MODULE)
        if rule is not None:
            rule_row = self._find_rule(rule)
            code = str(rule_row.get("指标编号")) if rule_row else str(rule)
            rows = [row for row in rows if str(row.get("指标编号")) == code]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("窗口编号", ""))
                or keyword in str(row.get("指标编号", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_window(self, reference: int | str) -> dict[str, Any] | None:
        window = self._find_window(reference)
        return dict(window) if window else None

    def register_window(
        self,
        reference: int | str,
        *,
        window_no: str | None = None,
        version_no: str | None = None,
        values: dict[str, Any] | None = None,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """登记阈值窗口：同一窗口编号重复登记时复用旧窗口，不新增第二个窗口。"""
        rule = self._find_rule(reference)
        if rule is None:
            return None, f"监控指标 {reference} 不存在或已归档"
        self._ensure_rule_defaults(rule)
        values = values or {}
        window_no = str(
            window_no or values.get("窗口编号") or values.get("window_id") or values.get("window") or ""
        ).strip()

        if window_no:
            existing = self._find_window(window_no)
            if existing is not None:
                if str(existing.get("指标编号")) != str(rule.get("指标编号")):
                    return (
                        None,
                        f"窗口编号 {window_no} 已被监控指标 {existing.get('指标编号')} 占用，"
                        "不能登记到当前指标",
                    )
                return dict(existing), f"窗口 {window_no} 已登记，本次重复登记未新增阈值窗口"

        if not window_no:
            sequence = len(self._rule_windows(rule)) + 1
            while True:
                candidate = f"WIN-{rule.get('指标编号')}-{sequence:03d}"
                if self._find_window(candidate) is None:
                    window_no = candidate
                    break
                sequence += 1

        version_no = _version_number(
            version_no or values.get("阈值版本") or values.get("version") or rule.get("当前阈值版本"),
            _version_index(rule.get("当前阈值版本") or "v1"),
        )
        known_versions = {str(item.get("版本")) for item in rule.get("阈值版本列表", [])}
        if version_no not in known_versions:
            return None, f"阈值版本 {version_no} 不属于监控指标 {rule.get('指标编号')}"

        snapshot = self._window_snapshot(rule, version_no)
        rows = store.rows(WINDOW_MODULE)
        window = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "窗口编号": window_no,
            "指标编号": rule.get("指标编号"),
            "规则id": rule.get("id"),
            "关联端点": rule.get("关联端点"),
            "启动阈值版本": version_no,
            "实际判定版本": version_no,
            "阈值版本": version_no,
            "判定依据版本": None,
            "status": OPEN_STATUS,
            "判定结论": None,
            "开始时间": str(values.get("开始时间") or _iso()),
            "结束时间": None,
            "登记时间": _iso(),
            "登记人": operator,
            **snapshot,
            "样本记录": [],
            "恢复次数": 0,
            "恢复记录": [],
            "恢复状态": "未恢复",
            "异常待处理": False,
            "冲突": None,
        }
        rows.append(window)
        rule["abnormal"] = False
        return dict(window), f"窗口 {window_no} 已按启动版本 {version_no} 登记"

    # ------------------------------------------------------------------ 窗口判定
    def _normalise_sample(self, sample: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        raw = dict(sample or {})
        version = raw.get("阈值版本") or raw.get("version") or raw.get("实际判定版本")
        version = _version_number(version or window.get("启动阈值版本") or "v1")
        latency = raw.get("延迟毫秒", raw.get("latency", raw.get("latency_ms", raw.get("延迟"))))
        success = raw.get("成功率", raw.get("success_rate", raw.get("successRate")))
        error = raw.get("错误率", raw.get("error_rate", raw.get("errorRate")))
        return {
            "样本编号": raw.get("样本编号") or raw.get("sample_id") or _iso(),
            "样本时间": raw.get("样本时间") or raw.get("time") or _iso(),
            "延迟毫秒": latency,
            "成功率": success,
            "错误率": error,
            "阈值版本": version,
            "实际判定版本": version,
            "原始样本": raw,
        }

    def _judge_sample(self, sample: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        failures: list[str] = []
        actual_latency = metric_number(sample.get("延迟毫秒"))
        threshold_latency = metric_number(snapshot.get("延迟阈值"))
        if actual_latency is not None and threshold_latency is not None:
            if actual_latency > threshold_latency:
                failures.append(
                    f"延迟 {_number_text(actual_latency)}ms 超过阈值 {_number_text(threshold_latency)}ms"
                )

        actual_success = _rate_percent(sample.get("成功率"))
        threshold_success = _rate_percent(snapshot.get("成功率阈值"))
        if actual_success is not None and threshold_success is not None:
            if actual_success < threshold_success:
                failures.append(
                    f"成功率 {_number_text(actual_success)}% 低于阈值 {_number_text(threshold_success)}%"
                )

        actual_error = _rate_percent(sample.get("错误率"))
        threshold_error = _rate_percent(snapshot.get("错误率告警"))
        if actual_error is not None and threshold_error is not None:
            if actual_error > threshold_error:
                failures.append(
                    f"错误率 {_number_text(actual_error)}% 高于阈值 {_number_text(threshold_error)}%"
                )

        sample["判定"] = FAIL_STATUS if failures else PASS_STATUS
        sample["判定说明"] = "；".join(failures) if failures else "样本在阈值范围内"
        sample["判定阈值"] = {
            "成功率阈值": snapshot.get("成功率阈值"),
            "延迟阈值": snapshot.get("延迟阈值"),
            "错误率告警": snapshot.get("错误率告警"),
        }
        return sample

    def judge_window(
        self,
        reference: int | str,
        *,
        samples: list[dict[str, Any]] | None = None,
        version_no: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """按窗口启动版本判定；同一窗口不允许再按另一个阈值版本生成第二套结论。"""
        window = self._find_window(reference)
        if window is None:
            return None, f"窗口 {reference} 不存在"
        startup_version = str(window.get("启动阈值版本") or "v1")
        submitted_version = _version_number(version_no) if version_no else None

        if submitted_version and submitted_version != startup_version:
            return (
                None,
                f"同一窗口不能混用两个阈值版本：窗口 {window.get('窗口编号')} 的启动版本是 "
                f"{startup_version}，提交的是 {submitted_version}",
            )
        if window.get("判定结论") and window.get("判定依据版本") not in (None, startup_version):
            return (
                None,
                f"窗口 {window.get('窗口编号')} 已有 {window.get('判定依据版本')} 的结论，"
                "同一窗口不能出现两套结论",
            )
        if window.get("status") == RECOVERED_STATUS:
            return (
                dict(window),
                f"窗口 {window.get('窗口编号')} 的告警已恢复，不再重复判定，避免同一窗口出现两套结论",
            )

        rule = self._find_rule(str(window.get("指标编号") or window.get("规则id")))
        if rule is None:
            return None, f"窗口 {window.get('窗口编号')} 关联的监控指标不存在"
        snapshot = self._window_snapshot(rule, startup_version)

        payload = values or {}
        incoming = samples
        if incoming is None:
            incoming = payload.get("样本记录") or payload.get("samples")
        if incoming is None and any(
            key in payload for key in ("延迟毫秒", "latency", "latency_ms", "成功率", "success_rate", "错误率", "error_rate")
        ):
            incoming = [payload]

        evaluated: list[dict[str, Any]] = []
        for raw in incoming or []:
            if not isinstance(raw, dict):
                continue
            sample = self._normalise_sample(raw, window)
            version = sample["实际判定版本"]
            if version == startup_version:
                sample["计入窗口结论"] = True
                sample = self._judge_sample(sample, snapshot)
            else:
                # 跨窗样本：仍然按实际判定版本标记结果，但不参与本窗口结论，
                # 保证同一窗口只保留启动版本的一套结论。
                version_detail = next(
                    (
                        item
                        for item in rule.get("阈值版本列表", [])
                        if item.get("版本") == version
                    ),
                    snapshot,
                )
                sample["计入窗口结论"] = False
                sample["说明"] = "跨窗样本，按实际判定版本标记，不参与本窗口结论"
                sample = self._judge_sample(sample, version_detail)
            evaluated.append(sample)

        existing = list(window.get("样本记录") or [])
        seen = {
            (str(item.get("样本编号")), str(item.get("实际判定版本")))
            for item in existing
        }
        for sample in evaluated:
            key = (str(sample.get("样本编号")), str(sample.get("实际判定版本")))
            if key not in seen:
                existing.append(sample)
                seen.add(key)
        window["样本记录"] = existing

        counted = [item for item in existing if item.get("计入窗口结论", True)]
        if not counted:
            return dict(window), f"窗口 {window.get('窗口编号')} 暂无属于启动版本 {startup_version} 的样本"

        failed = [item for item in counted if item.get("判定") == FAIL_STATUS]
        conclusion = FAIL_STATUS if failed else PASS_STATUS
        window["判定结论"] = conclusion
        window["判定依据版本"] = startup_version
        window["实际判定版本"] = startup_version
        window["判定阈值"] = {
            "成功率阈值": snapshot.get("成功率阈值"),
            "延迟阈值": snapshot.get("延迟阈值"),
            "错误率告警": snapshot.get("错误率告警"),
            "检测频率": snapshot.get("检测频率"),
            "静默分钟": snapshot.get("静默分钟"),
        }
        window["判定说明"] = (
            "；".join(item.get("判定说明", "") for item in failed)
            if failed
            else "窗口内样本均在启动版本阈值范围内"
        )
        window["异常待处理"] = conclusion == FAIL_STATUS
        if conclusion == FAIL_STATUS:
            window["status"] = FAIL_STATUS
            window["失败时间"] = _iso()
            window["结束时间"] = window["失败时间"]
            rule["abnormal"] = True
            message = (
                f"窗口 {window.get('窗口编号')} 判定失败，已进入监控异常待处理区，"
                f"判定版本 {startup_version}"
            )
        else:
            window["status"] = PASS_STATUS
            window["结束时间"] = _iso()
            rule["abnormal"] = bool(
                [item for item in self._rule_windows(rule) if item.get("status") == FAIL_STATUS]
            )
            message = f"窗口 {window.get('窗口编号')} 判定通过，判定版本 {startup_version}"
        return dict(window), message

    # ------------------------------------------------------------------ 告警恢复
    def recover_alert(
        self,
        reference: int | str,
        *,
        version_no: str | None = None,
        operator: str = "",
        reason: str = "",
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """恢复失败窗口告警：按窗口幂等，重复提交只认第一次，不重复累计。"""
        values = values or {}
        window = self._find_window(reference)
        if window is None:
            rule = self._find_rule(reference)
            if rule is not None:
                candidates = [
                    item for item in self._rule_windows(rule) if item.get("status") == FAIL_STATUS
                ] or [item for item in self._rule_windows(rule) if item.get("status") == RECOVERED_STATUS]
                window = candidates[0] if candidates else None
        if window is None:
            return None, f"窗口或监控指标 {reference} 不存在或没有可恢复的失败窗口"

        expected_version = str(window.get("启动阈值版本") or window.get("判定依据版本") or "v1")
        submitted = (
            version_no
            or values.get("阈值版本")
            or values.get("version")
            or values.get("启动阈值版本")
        )
        submitted_version = _version_number(submitted) if submitted else None

        if submitted_version and submitted_version != expected_version:
            return (
                None,
                f"恢复告警前请核对阈值版本：窗口 {window.get('窗口编号')} 使用 {expected_version}，"
                f"提交的是 {submitted_version}，版本不一致不允许恢复",
            )
        if window.get("status") == RECOVERED_STATUS:
            return (
                dict(window),
                f"告警已于 {window.get('恢复时间')} 恢复，本次重复提交未重复记录、未重复累计",
            )
        if window.get("status") not in {FAIL_STATUS, OPEN_STATUS} or not window.get("判定结论"):
            return None, f"窗口 {window.get('窗口编号')} 当前状态为 {window.get('status')}，没有待恢复告警"

        recovery = {
            "窗口编号": window.get("窗口编号"),
            "指标编号": window.get("指标编号"),
            "阈值版本": expected_version,
            "恢复时间": _iso(),
            "恢复人": operator,
            "恢复原因": reason or "值班确认指标恢复正常",
            "重复提交": False,
        }
        window["status"] = RECOVERED_STATUS
        window["恢复状态"] = "已恢复"
        window["恢复次数"] = 1
        window["恢复时间"] = recovery["恢复时间"]
        window["恢复阈值版本"] = expected_version
        window["恢复记录"] = [recovery]
        window["异常待处理"] = False
        window["结束时间"] = window.get("结束时间") or recovery["恢复时间"]

        records = store.rows(RECOVERY_MODULE)
        if not any(record.get("窗口编号") == window.get("窗口编号") for record in records):
            records.append(dict(recovery))

        rule = self._find_rule(str(window.get("指标编号") or window.get("规则id")))
        if rule is not None:
            rule["abnormal"] = bool(
                [item for item in self._rule_windows(rule) if item.get("status") == FAIL_STATUS]
            )
        return (
            dict(window),
            f"窗口 {window.get('窗口编号')} 告警已恢复，版本 {expected_version} 核对通过，恢复只记录一次",
        )

    # ------------------------------------------------------------------ 异常待处理区
    def abnormal_windows(
        self,
        *,
        rule: int | str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(WINDOW_MODULE)
        if rule is not None:
            rule_row = self._find_rule(rule)
            code = str(rule_row.get("指标编号")) if rule_row else str(rule)
            rows = [row for row in rows if str(row.get("指标编号")) == code]
        failed = [row for row in rows if row.get("status") == FAIL_STATUS]
        total = len(failed)
        start = max(page - 1, 0) * size
        items = []
        for row in failed[start:start + size]:
            item = dict(row)
            item["恢复状态"] = "未恢复"
            item["恢复次数"] = int(row.get("恢复次数") or 0)
            item["阈值版本"] = row.get("启动阈值版本") or row.get("判定依据版本")
            rule = self._find_rule(str(row.get("指标编号") or row.get("规则id")))
            if rule is not None:
                silence = self.silence_state(rule)
                item["静默状态"] = silence["状态"]
                item["静默状态文本"] = (
                    f"静默中（剩余 {silence['剩余分钟']} 分钟）"
                    if silence.get("静默中")
                    else "未静默"
                )
                item["静默阈值版本"] = silence.get("阈值版本")
            items.append(item)
        return items, total

    def get_abnormal(self, reference: int | str) -> dict[str, Any] | None:
        items, _ = self.abnormal_windows(rule=reference, page=1, size=10000)
        if not items:
            rule = self._find_rule(reference)
            if rule is not None:
                return {"指标编号": rule.get("指标编号"), "items": [], "total": 0}
            return None
        return {
            "指标编号": items[0].get("指标编号"),
            "阈值版本": items[0].get("阈值版本"),
            "items": items,
            "total": len(items),
        }

    # ------------------------------------------------------------------ 静默状态
    def silence_state(self, rule: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        """静默状态按最后一次恢复告警所用窗口的启动版本计算，和恢复记录保持一致。"""
        current_version = rule.get("当前阈值版本")
        version = next(
            (
                item
                for item in rule.get("阈值版本列表", [])
                if item.get("版本") == current_version
            ),
            None,
        )
        silence_minutes = metric_number(
            version.get("静默分钟") if version is not None else rule.get("静默分钟")
        )
        silence_version = current_version
        last_recovery: str | None = None
        for window in self._rule_windows(rule):
            if window.get("status") != RECOVERED_STATUS or not window.get("恢复时间"):
                continue
            if last_recovery is None or str(window.get("恢复时间")) > last_recovery:
                last_recovery = str(window.get("恢复时间"))
                silence_version = window.get("启动阈值版本") or window.get("判定依据版本")
                silence_minutes = metric_number(window.get("静默分钟")) or silence_minutes
        text_minutes = _number_text(silence_minutes) if silence_minutes is not None else ""
        if silence_minutes is None or last_recovery is None:
            return {
                "状态": "未静默",
                "静默中": False,
                "静默分钟": text_minutes,
                "阈值版本": current_version,
                "静默截止": None,
                "剩余分钟": 0,
            }
        try:
            recovered_at = datetime.fromisoformat(last_recovery)
        except ValueError:
            recovered_at = now or _now()
        until = recovered_at + timedelta(minutes=silence_minutes)
        current = now or _now()
        remaining = max(int((until - current).total_seconds() // 60), 0)
        return {
            "状态": "静默中" if current < until else "未静默",
            "静默中": current < until,
            "静默分钟": text_minutes,
            "阈值版本": silence_version,
            "静默开始": recovered_at.isoformat(),
            "静默截止": until.isoformat(),
            "剩余分钟": remaining,
        }


monitor_service = MonitorService()
