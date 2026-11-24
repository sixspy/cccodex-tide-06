"""窗键幂等只读：回执、归档、对账、贯验快照。

对账把「装载校验 → 观测现行 → 入窗缓冲/迟到匣 → 幂等回执」串成一行，
重复灌后可直接核对：现行行数变没变、归档有没有旧行。
"""
from __future__ import annotations
from app.db import 连接


from app.services import 校验


def 回执列表(事件编号: str | None = None, 限制: int = 200) -> list[dict]:
    conn = 连接()
    try:
        if 事件编号:
            rows = conn.execute(
                """SELECT 编号,事件编号,形态 AS 形态标记,拒因,归档键,valid,批次编号,时刻
                   FROM 幂等账 WHERE 事件编号=? ORDER BY 编号""",
                (事件编号,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT 编号,事件编号,形态 AS 形态标记,拒因,归档键,valid,批次编号,时刻
                   FROM 幂等账 ORDER BY 编号 DESC LIMIT ?""",
                (限制,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 归档列表(事件编号: str | None = None, 限制: int = 200) -> list[dict]:
    conn = 连接()
    try:
        if 事件编号:
            rows = conn.execute(
                "SELECT * FROM 幂等归档 WHERE 事件编号=? ORDER BY 归档时,归档键", (事件编号,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 幂等归档 ORDER BY 归档时 DESC, 归档键 DESC LIMIT ?", (限制,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 对账列表(事件编号: str | None = None, 限制: int = 200) -> list[dict]:
    """逐号对账：现行行数 / 归档行数 / 最近回执形态、拒因、归档键、valid。

    事件编号取幂等账与归档的并集；只读，不改任何行。
    """
    conn = 连接()
    try:
        编号条件 = "WHERE 事件编号=?" if 事件编号 else ""
        归档条件 = "WHERE 事件编号=?" if 事件编号 else ""
        参 = ((事件编号, 事件编号) if 事件编号 else ()) + (限制,)
        编号集 = conn.execute(
            f"""SELECT 事件编号 FROM 幂等账 {编号条件}
                UNION SELECT 事件编号 FROM 幂等归档 {归档条件}
                ORDER BY 事件编号 LIMIT ?""",
            参,
        ).fetchall()
        结果 = []
        for r in 编号集:
            编号 = r["事件编号"]
            现行行数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 观测点 WHERE 事件编号=?", (编号,)
            ).fetchone()["c"]
            归档行数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 幂等归档 WHERE 事件编号=?", (编号,)
            ).fetchone()["c"]
            账 = conn.execute(
                """SELECT 形态,拒因,归档键,valid FROM 幂等账
                   WHERE 事件编号=? ORDER BY 编号 DESC LIMIT 1""",
                (编号,),
            ).fetchone()
            现行 = conn.execute(
                "SELECT 命运,事件时,运单键,网点键,批次编号 FROM 观测点 WHERE 事件编号=?",
                (编号,),
            ).fetchone()
            缓冲 = conn.execute(
                "SELECT 命运 FROM 入窗缓冲 WHERE 事件编号=?", (编号,)
            ).fetchone()
            迟到 = conn.execute(
                "SELECT COUNT(*) AS c FROM 迟到匣 WHERE 事件编号=?", (编号,)
            ).fetchone()["c"]
            校验错 = (
                校验.校验事件(
                    {
                        "事件编号": 编号,
                        "运单键": 现行["运单键"],
                        "网点键": 现行["网点键"],
                        "事件时": 现行["事件时"],
                    }
                )
                if 现行
                else []
            )
            结果.append(
                {
                    "事件编号": 编号,
                    "现行行数": int(现行行数),
                    "归档行数": int(归档行数),
                    "形态标记": 账["形态"] if 账 else "",
                    "拒因": 账["拒因"] if 账 else "",
                    "归档键": 账["归档键"] if 账 else "",
                    "valid": bool(账["valid"]) if 账 else False,
                    "装载批次": 现行["批次编号"] if 现行 else "",
                    "校验": "通过" if 现行 and not 校验错 else 校验.摘要错误(校验错),
                    "观测命运": 现行["命运"] if 现行 else "",
                    "缓冲命运": 缓冲["命运"] if 缓冲 else "",
                    "迟到行数": int(迟到),
                }
            )
        return 结果
    finally:
        conn.close()


def 贯验快照() -> dict:
    """供窗贯验 extra_verify_checks 的互证快照（只读）。"""
    conn = 连接()
    try:
        迟到重复 = [
            r["事件编号"]
            for r in conn.execute(
                """SELECT 事件编号 FROM 迟到匣 GROUP BY 事件编号
                   HAVING COUNT(*) > 1 ORDER BY 事件编号"""
            ).fetchall()
        ]
        缓冲重复 = [
            r["事件编号"]
            for r in conn.execute(
                """SELECT 事件编号 FROM 入窗缓冲 GROUP BY 事件编号
                   HAVING COUNT(*) > 1 ORDER BY 事件编号"""
            ).fetchall()
        ]
        归档缺现行 = [
            r["事件编号"]
            for r in conn.execute(
                """SELECT DISTINCT a.事件编号 FROM 幂等归档 a
                   LEFT JOIN 观测点 o ON o.事件编号 = a.事件编号
                   WHERE o.事件编号 IS NULL ORDER BY a.事件编号"""
            ).fetchall()
        ]
        回执缺归档 = [
            r["归档键"]
            for r in conn.execute(
                """SELECT z.归档键 AS 归档键 FROM 幂等账 z
                   LEFT JOIN 幂等归档 a ON a.归档键 = z.归档键
                   WHERE z.形态='归档' AND z.归档键 <> '' AND a.归档键 IS NULL
                   ORDER BY z.编号"""
            ).fetchall()
        ]
        封印缺首灌 = [
            r["事件编号"]
            for r in conn.execute(
                """SELECT DISTINCT z.事件编号 FROM 幂等账 z
                   LEFT JOIN 观测点 o ON o.事件编号 = z.事件编号
                   WHERE z.形态='封印' AND z.拒因 <> '' AND o.事件编号 IS NULL
                   ORDER BY z.事件编号"""
            ).fetchall()
        ]
        return {
            "迟到重复编号": 迟到重复,
            "缓冲重复编号": 缓冲重复,
            "归档缺现行": 归档缺现行,
            "回执缺归档": 回执缺归档,
            "封印缺首灌": 封印缺首灌,
        }
    finally:
        conn.close()
