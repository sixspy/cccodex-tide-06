"""窗键幂等：同一事件编号重复装载只保留首条命运。

实现两种可切换形态（窗规键 `幂等形态`）：

- 封印：已见编号整票拒回刷。重复装载不写现行、不进缓冲/迟到匣，
  只落一条封印回执（拒因 + valid），现行行数不变。
- 归档：双写两轨。现行只认未见编号；重复装载把现行旧行快照复制进只读
  归档（归档键），现行行数不变、归档多一行旧行，下游按归档键分开读取。

查重、写现行/旁路、写回执在同一事务里完成（装载服务显式 BEGIN IMMEDIATE），
并发同号不会各成一行。

扩展切口（与目录同名空桩约定一致）：
- 装载前后钩子：before_load / after_load
- 入窗判定钩子：decide_window
- 关窗前后钩子：before_close / after_close
- 贯验扩展：extra_verify_checks
"""

from __future__ import annotations

NAME = "窗键幂等"
CONTRACT = "同一事件编号重复装载只保留首条命运"

形态_封印 = "封印"
形态_归档 = "归档"
形态集 = (形态_封印, 形态_归档)

形态说明 = {
    形态_封印: "已见编号封印拒回刷：二次同号整票拒，写封印回执，现行不加行",
    形态_归档: "双写两轨：旧号快照进只读归档，现行只认未见编号，按归档键分开读",
}

默认形态 = 形态_封印

HOOKS = (
    "before_load",
    "after_load",
    "decide_window",
    "before_close",
    "after_close",
    "extra_verify_checks",
)


def 规范形态(值) -> str:
    """把窗规里的形态值收敛成两种规范形态之一。未知值回落到默认封印。"""
    s = str(值 or "").strip()
    if s in (形态_归档, "双写", "归档轨", "archive", "双写两轨"):
        return 形态_归档
    return 形态_封印


def 是归档形态(值) -> bool:
    return 规范形态(值) == 形态_归档


def 形态文案(值) -> str:
    形 = 规范形态(值)
    return f"{形}｜{形态说明[形]}"


def 封印回执(事件编号: str, 批次编号: str = "") -> dict:
    """封印形态下的重号回执：整票拒，不给归档键。"""
    return {
        "事件编号": 事件编号,
        "形态": 形态_封印,
        "形态标记": 形态_封印,
        "拒因": "封印拒回刷：事件编号已见",
        "归档键": "",
        "批次编号": 批次编号,
        "valid": True,
    }


def 归档回执(事件编号: str, 归档键: str, 批次编号: str = "") -> dict:
    """归档形态下的重号回执：旧号进只读归档，现行不认该号。"""
    return {
        "事件编号": 事件编号,
        "形态": 形态_归档,
        "形态标记": 形态_归档,
        "拒因": "现行只认未见编号：旧号已转只读归档",
        "归档键": 归档键,
        "批次编号": 批次编号,
        "valid": True,
    }


def describe() -> dict:
    return {
        "空桩": NAME,
        "契约": CONTRACT,
        "形态": list(形态集),
        "默认形态": 默认形态,
        "形态说明": dict(形态说明),
        "钩子": list(HOOKS),
        "默认启用": True,
        "说明": "装载服务已接线：查重 + 写现行/归档 + 回执同一事务；页面可切形态",
    }


def before_load(batch: dict) -> dict:
    """装载前：批内显式给了幂等形态就规范它；未给则留空交窗规决定。"""
    if isinstance(batch, dict):
        batch = dict(batch)
        if "幂等形态" in batch:
            batch["幂等形态"] = 规范形态(batch.get("幂等形态"))
    return batch


def after_load(report: dict) -> dict:
    """装载后：把形态标记与重号回执挂到装载回执上（命运与回执同票）。"""
    if not isinstance(report, dict):
        return report
    report.setdefault("幂等形态", 默认形态)
    report.setdefault("重号回执", [])
    return report


def decide_window(event: dict, rules: dict) -> str:
    """按事件时判定：窗内入缓冲，窗外进迟到。基线默认口径，不改窗号。"""
    from app.services.时间 import 在窗内

    事件时 = str((event or {}).get("事件时") or "")
    窗起 = str((rules or {}).get("窗起") or "")
    窗止 = str((rules or {}).get("窗止") or "")
    if not (事件时 and 窗起 and 窗止):
        return "驳回"
    try:
        return "入缓冲" if 在窗内(事件时, 窗起, 窗止) else "迟到"
    except Exception:
        return "驳回"


def before_close(window_id: str) -> None:
    """关窗前检查。幂等不阻断默认关窗。"""
    return None


def after_close(report: dict) -> dict:
    """关窗后处理。幂等不追加动作，默认透传。"""
    return report


def extra_verify_checks(snapshot: dict) -> list[str]:
    """贯验附加项：幂等账与现行/缓冲/迟到互证，返回中文错误文案。"""
    错: list[str] = []
    s = snapshot or {}
    迟到重复 = s.get("迟到重复编号") or []
    for 编号 in 迟到重复:
        错.append(f"迟到匣同号多行：{编号}")
    缓冲重复 = s.get("缓冲重复编号") or []
    for 编号 in 缓冲重复:
        错.append(f"入窗缓冲同号多行：{编号}")
    缺现行 = s.get("归档缺现行") or []
    for 编号 in 缺现行:
        错.append(f"归档旧行缺现行：{编号}")
    缺归档 = s.get("回执缺归档") or []
    for 小编 in 缺归档:
        错.append(f"归档回执缺归档行：{小编}")
    封印缺首灌 = s.get("封印缺首灌") or []
    for 编号 in 封印缺首灌:
        错.append(f"封印回执的事件编号无首灌现行：{编号}")
    return 错
