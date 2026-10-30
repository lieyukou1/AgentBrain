"""MCP 调用日志：追加写 + 按大小轮转，读写共用同一套路径约定。

为什么要轮转：每次工具调用都追加一行，而 funnel 每次统计都要全量读一遍。
不设上限，日志会无限涨、stats 会越来越慢。保留一份历史文件（.1）就够——
使用漏斗只关心近 N 天，更早的逐条明细没有查询需求。
"""
from __future__ import annotations

import json
import os

from . import db

MAX_BYTES = 4_000_000


def path():
    return db.home() / "logs" / "mcp-calls.jsonl"


def rotated():
    p = path()
    return p.with_name(p.name + ".1")


def _rotate(p) -> None:
    """超过上限就把当前文件顶成 .1，覆盖旧的历史。

    失败（文件被另一个进程占用）时直接放弃轮转：宁可日志继续涨，
    也不能因为整理日志而丢掉这次调用记录。
    """
    try:
        if p.stat().st_size < MAX_BYTES:
            return
        os.replace(p, rotated())
    except OSError:
        return


def append(row: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _rotate(p)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read() -> list[dict]:
    """按时间顺序返回：先历史文件，再当前文件。坏行跳过，不让一行脏数据毁掉统计。"""
    rows: list[dict] = []
    for p in (rotated(), path()):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
