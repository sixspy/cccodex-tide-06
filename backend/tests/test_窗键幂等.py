"""窗键幂等验收：首灌 / 重号 / 并发 / 迟到侧 / 成片对照 / 贯验互证。

夹具沿用 fixtures 下的三份样例，跑前清空库（autouse），互不串数。
"""
from pathlib import Path
import os
import sqlite3
import tempfile
import threading

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("数据目录", _tmp)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

from app import seed
from app.db import 连接, 库路径
from app.services import 装载, 关窗, 贯验, 目录, 夹具, 幂等
from app.modules import 窗键幂等


def _库文件():
    return (库路径, Path(str(库路径) + "-wal"), Path(str(库路径) + "-shm"))


@pytest.fixture(autouse=True)
def _清库():
    for p in _库文件():
        if p.exists():
            p.unlink()
    seed.初始化库()
    目录.改窗规({"幂等形态": "封印"})
    yield
    # 收尾清库，避免与本目录其它测试文件共库时串数。
    for p in _库文件():
        if p.exists():
            p.unlink()


def _事件(编号, 运单="运单-A", 网点="网点-东站", 时="2026-11-24T00:15:00Z"):
    return {"事件编号": 编号, "运单键": 运单, "网点键": 网点, "事件时": 时}


def _现行行数():
    conn = 连接()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM 观测点").fetchone()["c"]
    finally:
        conn.close()


def _归档行(编号):
    conn = 连接()
    try:
        return conn.execute(
            "SELECT * FROM 幂等归档 WHERE 事件编号=?", (编号,)
        ).fetchall()
    finally:
        conn.close()


# ---------- 首灌 ----------

def test_首灌入缓冲成功():
    报 = 装载.装一批({"批次编号": "首-1", "事件": [_事件("k1")]})
    assert 报["入流"] == 1 and 报["入缓冲"] == 1
    assert 报["幂等形态"] == "封印"
    assert any(x["命运"] == "在缓冲" for x in 目录.缓冲列表())
    账 = 幂等.回执列表("k1")
    assert len(账) == 1 and 账[0]["valid"] == 1 and 账[0]["拒因"] == ""


# ---------- 封印形态 ----------

def test_封印重号现行不增():
    装载.装一批({"批次编号": "首-1", "事件": [_事件("k2"), _事件("k3")]})
    首行 = _现行行数()
    报 = 装载.装一批({"批次编号": "重-1", "事件": [_事件("k2")]})
    assert _现行行数() == 首行, "封印形态重号不得给现行加行"
    assert 报["入流"] == 0 and 报["重号"] == 1
    assert 报["重号回执"][0]["形态标记"] == "封印"
    assert 报["重号回执"][0]["拒因"]
    assert 报["重号回执"][0]["归档键"] == ""
    assert _归档行("k2") == []
    # 回执可回读：拒因 + valid
    账 = 幂等.回执列表("k2")
    assert len(账) == 2  # 首灌 + 封印拒
    assert 账[-1]["形态标记"] == "封印" and 账[-1]["拒因"] and 账[-1]["valid"] == 1


# ---------- 归档形态 ----------

def test_归档重号现行不增且归档有旧行():
    目录.改窗规({"幂等形态": "归档"})
    装载.装一批({"批次编号": "首-1", "事件": [_事件("k4", 运单="运单-R")]})
    首行 = _现行行数()
    报 = 装载.装一批({"批次编号": "重-1", "事件": [_事件("k4", 运单="运单-R")]})
    assert _现行行数() == 首行
    assert 报["重号"] == 1 and 报["归档"] == 1
    回执 = 报["重号回执"][0]
    assert 回执["形态标记"] == "归档" and 回执["归档键"]
    旧 = _归档行("k4")
    assert len(旧) == 1 and 旧[0]["归档键"] == 回执["归档键"]
    assert 旧[0]["事件编号"] == "k4" and 旧[0]["原命运"]
    # 归档行只读、引用可拆读
    assert [r["事件编号"] for r in 幂等.归档列表()] == ["k4"]
    对 = 幂等.对账列表("k4")[0]
    assert 对["现行行数"] == 1 and 对["归档行数"] == 1


# ---------- 并发同号 ----------

def test_并发同号不得双行():
    目录.改窗规({"幂等形态": "归档"})
    结果 = []
    锁 = threading.Lock()

    def 灌(i):
        try:
            r = 装载.装一批({"批次编号": f"并-{i}", "事件": [_事件("race-1")]})
            with 锁:
                结果.append(r)
        except sqlite3.OperationalError as e:  # 写锁竞争超时也不得写坏
            with 锁:
                结果.append({"错误": str(e)})

    线 = [threading.Thread(target=灌, args=(i,)) for i in range(6)]
    for t in 线:
        t.start()
    for t in 线:
        t.join()

    成功 = [r for r in 结果 if "入流" in r]
    assert len(成功) == 6
    assert sum(r["入流"] for r in 成功) == 1, "并发同号只允许一次首灌"
    assert sum(r["重号"] for r in 成功) == 5
    assert _现行行数() == 1
    assert len(_归档行("race-1")) == 5, "每个被拒同号应各留一条归档旧行"


# ---------- 迟到侧同号 ----------

def test_迟到侧同号不双行():
    目录.改窗规({"幂等形态": "归档"})
    迟 = _事件("late-k", 时="2026-11-24T05:00:00Z")
    assert 装载.装一批({"批次编号": "迟-1", "事件": [迟]})["迟到"] == 1
    n迟到 = len(目录.迟到列表())
    报 = 装载.装一批({"批次编号": "迟-2", "事件": [迟]})
    assert 报["重号"] == 1 and 报["迟到"] == 0
    assert len(目录.迟到列表()) == n迟到, "迟到匣不得因重号多一行"
    assert len(_归档行("late-k")) == 1
    assert _现行行数() == 1


# ---------- 成片对照 ----------

def test_重号成片对照不漂():
    目录.改窗规({"幂等形态": "归档"})
    体 = 夹具.读取("窗内成片样例")
    首 = 装载.装一批(体)
    assert 首["入缓冲"] == 3
    n缓冲 = len(目录.缓冲列表())

    # 成片前再灌同号：现行缓冲不得多行
    重 = 装载.装一批(体)
    assert 重["重号"] == 3 and 重["入缓冲"] == 0
    assert len(目录.缓冲列表()) == n缓冲

    关 = 关窗.关当前窗()
    assert 关["成片数"] == 2 and 关["缓冲消耗"] == 3
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is True, 验["说明"]
    # 成片后再灌同号：切片/缓冲/现行都不漂
    n切 = len(目录.切片列表())
    重2 = 装载.装一批(体)
    assert 重2["重号"] == 3 and 重2["入流"] == 0
    assert len(目录.切片列表()) == n切
    assert 贯验.跑一次()["是否对齐"] is True


# ---------- 贯验互证 ----------

def test_贯验能读出幂等互证():
    装载.装一批({"事件": [_事件("v1")]})
    验 = 贯验.跑一次()
    assert 验["幂等互证"] == [] and 验["是否对齐"] is True


def test_窗规切换形态可读():
    目录.改窗规({"幂等形态": "归档"})
    assert 目录.窗规()["幂等形态"] == "归档"
    形 = 窗键幂等.规范形态("xx")
    assert 形 == "封印"  # 未知值回落默认
    概 = 窗键幂等.describe()
    assert set(概["形态"]) == {"封印", "归档"}


# ---------- 四文件（页面）含测 ----------

def test_四页面读中文幂等():
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        页 = {
            "/观测流.html": ["装载回执", "窗键幂等"],
            "/入窗缓冲.html": ["单号对账", "幂等痕迹"],
            "/迟到匣.html": ["迟到卡片", "迟到侧同号对账"],
            "/窗规.html": ["窗键幂等策略", "归档（双写两轨）"],
        }
        for path, 需 in 页.items():
            r = c.get(path)
            assert r.status_code == 200, path
            for 字 in 需:
                assert 字 in r.text, f"{path} 缺文案 {字}"
        assert "窗键幂等" not in c.get("/api/空桩").json()["空桩"]
        概 = c.get("/api/窗键幂等").json()
        assert 概["当前形态"] == "封印" and "封印" in 概["当前形态说明"]
