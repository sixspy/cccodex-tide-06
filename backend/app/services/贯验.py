"""窗贯验：缓冲/切片/迟到互证，外加窗键幂等互证。"""
from __future__ import annotations
from app.db import 连接
from app.seed import 此刻
from app.services import 幂等
from app.modules.窗键幂等 import extra_verify_checks


def 跑一次() -> dict:
    conn = 连接()
    try:
        缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='在缓冲'"
        ).fetchone()["c"]
        已物化缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化'"
        ).fetchone()["c"]
        切片事件 = conn.execute(
            "SELECT COALESCE(SUM(事件数),0) AS c FROM 窗切片账"
        ).fetchone()["c"]
        迟到 = conn.execute("SELECT COUNT(*) AS c FROM 迟到匣").fetchone()["c"]
        # 基线口径：已物化缓冲事件数应等于切片事件数之和
        基线对齐 = int(已物化缓冲) == int(切片事件)
        基线说明 = (
            "缓冲物化与切片事件数一致"
            if 基线对齐
            else f"撕裂：已物化缓冲={已物化缓冲} 切片事件={切片事件}"
        )
        幂等错 = extra_verify_checks(幂等.贯验快照())
        对齐 = 基线对齐 and not 幂等错
        if 基线对齐 and 幂等错:
            说明 = "基线对齐，但幂等互证不齐：" + "; ".join(幂等错)
        elif 幂等错:
            说明 = 基线说明 + "；幂等互证不齐：" + "; ".join(幂等错)
        else:
            说明 = 基线说明
        时刻 = 此刻()
        conn.execute(
            """INSERT INTO 窗贯验(时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明)
               VALUES (?,?,?,?,?,?)""",
            (时刻, 缓冲, int(切片事件), 迟到, 1 if 对齐 else 0, 说明),
        )
        conn.commit()
        return {
            "时刻": 时刻,
            "在缓冲": 缓冲,
            "已物化缓冲": int(已物化缓冲),
            "切片事件": int(切片事件),
            "迟到": 迟到,
            "基线对齐": bool(基线对齐),
            "幂等互证": 幂等错,
            "是否对齐": bool(对齐),
            "说明": 说明,
        }
    finally:
        conn.close()
