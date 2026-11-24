import os
import sqlite3
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "潮切.db"


def 连接(写锁: bool = False) -> sqlite3.Connection:
    """打开库连接。

    写锁=True 时关闭 Python 侧隐式事务，调用方须显式 BEGIN IMMEDIATE 再 commit，
    用于把「查重 → 写现行/旁路 → 写回执」锁成一个事务，挡住并发同号各成一行。
    """
    数据目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(库路径, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
    if 写锁:
        c.isolation_level = None
    return c
