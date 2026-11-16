#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stopcheck —— 判断某个复核任务是否已被用户「停止」。

用法（脚本里）：
    import stopcheck
    if stopcheck.stopped('Q20261030_110939_1'):
        raise SystemExit('任务已被用户停止，中止执行')

用法（命令行）：
    python stopcheck.py Q20261030_110939_1
    退出码 0 = 仍在进行；1 = 已停止
"""
import json
import os
import time

QUEUE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'work', 'queue')
STOPPED = ('已停止', '已取消', '失败', '已完成')


def status(task_id, retries=3):
    """返回任务当前状态；文件不存在返回 None；解析失败重试后仍失败返回 '__parse_error__'。

    G13（2026-11-11）：任务文件可能正撞上原子写窗口，读失败重试几次再下结论，
    而不是一次瞬时读失败就被当成"已停止"。
    """
    if not task_id:
        return None
    fp = os.path.join(QUEUE, task_id + '.json')
    for _ in range(retries):
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, encoding='utf-8') as fh:
                return (json.load(fh) or {}).get('状态')
        except Exception:
            time.sleep(0.15)
    return '__parse_error__'


def stopped(task_id, allow=('待处理', '处理中', '复核未完成')):
    """任务是否应当停止执行。
       allow 默认只放行「待处理/处理中」；已停止/已完成/失败/不存在 都返回 True。
       解析失败（__parse_error__）→ 不当"已停止"，返回 False，避免误中止整单复核。"""
    st = status(task_id)
    if st == '__parse_error__':
        return False
    return st not in allow


def assert_running(task_id):
    if stopped(task_id):
        raise SystemExit('任务 %s 当前状态为「%s」，已中止执行'
                         % (task_id, status(task_id)))
    return True


if __name__ == '__main__':
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else ''
    st = status(tid)
    print('任务 %s 状态：%s' % (tid, st))
    sys.exit(1 if stopped(tid) else 0)
