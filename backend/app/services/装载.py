"""观测点装载 → 入窗或迟到。

窗键幂等（同一事件编号重复装载只保留首条命运）在装载编排内落地：
查重、写现行/旁路、写幂等回执三者锁在同一 BEGIN IMMEDIATE 事务里，
并发同号不会各成一行。形态（封印 / 归档）取自窗规 `幂等形态`，可按批覆盖。
"""
from __future__ import annotations
import json
import uuid
from app.db import 连接
from app.seed import 此刻
from app.services import 校验, 目录, 时间
from app.modules.窗键幂等 import (
    归档回执,
    封印回执,
    是归档形态,
    规范形态,
)


def _读形态(conn, 体: dict) -> str:
    体形 = (体 or {}).get("幂等形态")
    if 体形:
        return 规范形态(体形)
    行 = conn.execute("SELECT 值 FROM 窗规 WHERE 键='幂等形态'").fetchone()
    return 规范形态(行["值"] if 行 else None)


def _写回执(conn, 回执: dict, 批次: str, 时刻: str) -> None:
    conn.execute(
        """INSERT INTO 幂等账(事件编号,形态,拒因,归档键,valid,批次编号,时刻)
           VALUES (?,?,?,?,?,?,?)""",
        (
            回执["事件编号"],
            回执["形态"],
            回执.get("拒因", ""),
            回执.get("归档键", ""),
            1 if 回执.get("valid") else 0,
            批次 if not 回执.get("批次编号") else 回执["批次编号"],
            时刻,
        ),
    )


def _归档旧行(conn, 现行: dict, 批次: str, 时刻: str) -> str:
    """把现行旧行快照复制进只读归档，返回归档键。归档键绑事件编号+批次+序。"""
    归档键 = f"档-{现行['事件编号']}-{批次}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """INSERT INTO 幂等归档(归档键,事件编号,运单键,网点键,事件时,原命运,来源批次,归档时)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            归档键,
            现行["事件编号"],
            现行["运单键"],
            现行["网点键"],
            现行["事件时"],
            现行["命运"],
            批次,
            时刻,
        ),
    )
    return 归档键


def 装一批(体: dict) -> dict:
    if not isinstance(体, dict):
        体 = {}
    批次 = 体.get("批次编号") or f"潮批-{uuid.uuid4().hex[:8]}"
    事件 = 体.get("事件") or []
    报告 = {
        "批次编号": 批次,
        "入流": 0,
        "入缓冲": 0,
        "迟到": 0,
        "驳回": 0,
        "重号": 0,
        "归档": 0,
        "幂等形态": "",
        "重号回执": [],
        "明细": [],
    }
    for e in 校验.校验批次(体):
        报告["明细"].append({"命运": "批次预检", "说明": e})
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "2026-11-24T00:00:00Z")
    窗止 = 规.get("窗止", "2026-11-24T01:00:00Z")
    # 写锁连接 + 显式 IMMEDIATE：查重与写库同事务，串行化并发写者。
    conn = 连接(写锁=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        形态 = _读形态(conn, 体)
        报告["幂等形态"] = 形态
        归档态 = 是归档形态(形态)
        for ev in 事件:
            行错 = 校验.校验事件(ev)
            if 行错:
                报告["驳回"] += 1
                报告["明细"].append({"命运": "驳回-校验", "说明": ";".join(行错)})
                continue
            编号 = ev["事件编号"]
            时刻 = 此刻()
            现行 = conn.execute(
                "SELECT * FROM 观测点 WHERE 事件编号=?", (编号,)
            ).fetchone()
            if 现行:
                # 已见编号：现行一律不加行，命运与回执同票。
                报告["重号"] += 1
                if 归档态:
                    归档键 = _归档旧行(conn, dict(现行), 批次, 时刻)
                    回执 = 归档回执(编号, 归档键, 批次)
                    报告["归档"] += 1
                    报告["明细"].append(
                        {"命运": "重号-归档", "事件编号": 编号, "归档键": 归档键}
                    )
                else:
                    回执 = 封印回执(编号, 批次)
                    报告["驳回"] += 1
                    报告["明细"].append(
                        {"命运": "重号-封印", "事件编号": 编号, "拒因": 回执["拒因"]}
                    )
                _写回执(conn, 回执, 批次, 时刻)
                报告["重号回执"].append(回执)
                continue
            处理 = 此刻()
            conn.execute(
                """INSERT INTO 观测点(事件编号,运单键,网点键,事件时,处理时,命运,批次编号)
                   VALUES (?,?,?,?,?,?,?)""",
                (编号, ev["运单键"], ev["网点键"], ev["事件时"], 处理, "已装载", 批次),
            )
            报告["入流"] += 1
            首灌回执 = {
                "事件编号": 编号,
                "形态": 形态,
                "形态标记": 形态,
                "拒因": "",
                "归档键": "",
                "批次编号": 批次,
                "valid": True,
            }
            _写回执(conn, 首灌回执, 批次, 处理)
            if 时间.在窗内(ev["事件时"], 窗起, 窗止):
                conn.execute(
                    """INSERT INTO 入窗缓冲(事件编号,运单键,网点键,事件时,窗号,命运,入缓冲时)
                       VALUES (?,?,?,?,?,?,?)""",
                    (编号, ev["运单键"], ev["网点键"], ev["事件时"], 窗号, "在缓冲", 处理),
                )
                conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已入缓冲", 编号))
                报告["入缓冲"] += 1
                报告["明细"].append({"命运": "入缓冲", "事件编号": 编号, "窗号": 窗号})
            else:
                conn.execute(
                    """INSERT INTO 迟到匣(事件编号,运单键,原因,事件时,入匣时)
                       VALUES (?,?,?,?,?)""",
                    (编号, ev["运单键"], "事件时出窗", ev["事件时"], 处理),
                )
                conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已迟到", 编号))
                报告["迟到"] += 1
                报告["明细"].append({"命运": "迟到", "事件编号": 编号})
        conn.execute(
            """INSERT INTO 装载批次(编号,时刻,入流,入缓冲,迟到,报告) VALUES (?,?,?,?,?,?)
               ON CONFLICT(编号) DO UPDATE SET 时刻=excluded.时刻,入流=excluded.入流,
                 入缓冲=excluded.入缓冲,迟到=excluded.迟到,报告=excluded.报告""",
            (批次, 此刻(), 报告["入流"], 报告["入缓冲"], 报告["迟到"], json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 报告
