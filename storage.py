# -*- coding: utf-8 -*-
"""storage.py —— 存储层：原子写 + 备份 + 日志/反馈落盘，便于单测。

从 server.py 拆出（P2-1 第三步）：backup_once / atomic_write_json /
rescue_log_add / front_log_add / issue_add / issue_list，以及它们依赖的路径常量。
"""
import datetime
import json
import os
import shutil
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, 'work')
RESCUE_LOG = os.path.join(WORK, 'rescue_log.json')
FRONT_LOG = os.path.join(WORK, 'front_log.json')   # 前端上报的运行/错误日志（最多 300 条）
ISSUE_LOG = os.path.join(WORK, '复核问题反馈.json')   # agent 复核中发现的问题反馈（最多 500 条）


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def backup_once(path):
    """保存前备份一份 .bak（只保留最近一次）"""
    try:
        if os.path.isfile(path):
            b = path + '.bak'
            if os.path.isfile(b):
                os.remove(b)
            shutil.copy2(path, b)
    except Exception:
        traceback.print_exc()


def atomic_write_json(path, obj, backup=False):
    """M3（2026-11-12）：原子写 JSON——tmp + os.replace，失败不留半截、不截断旧件。

    backup=True 时先调用 backup_once 备份 .bak（用于复核结果这类关键文件）。
    返回是否成功。
    """
    tmp = path + '.tmp.' + str(os.getpid())
    try:
        if backup and os.path.isfile(path):
            backup_once(path)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        traceback.print_exc()
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def rescue_log_add(kind, tid, msg, **kw):
    """记一条兜底日志（最多留 300 条）。"""
    try:
        items = []
        if os.path.isfile(RESCUE_LOG):
            try:
                with open(RESCUE_LOG, encoding='utf-8') as f:
                    items = json.load(f) or []
            except Exception:
                items = []
        entry = {'时间': now(), '类型': kind, '任务': tid, '说明': msg}
        entry.update(kw)
        items.append(entry)
        items = items[-300:]
        os.makedirs(WORK, exist_ok=True)
        with open(RESCUE_LOG, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception:
        traceback.print_exc()


def front_log_add(level, msg, **kw):
    """记一条前端上报日志（最多留 300 条），落盘 work\\front_log.json。"""
    try:
        items = []
        if os.path.isfile(FRONT_LOG):
            try:
                with open(FRONT_LOG, encoding='utf-8') as f:
                    items = json.load(f) or []
            except Exception:
                items = []
        entry = {'时间': now(), '级别': level, '消息': msg}
        entry.update(kw)
        items.append(entry)
        items = items[-300:]
        os.makedirs(WORK, exist_ok=True)
        with open(FRONT_LOG, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception:
        traceback.print_exc()


def issue_add(pname, unit, category, note, **kw):
    """记一条复核问题反馈（最多留 500 条），落盘 work\\复核问题反馈.json。"""
    try:
        items = []
        if os.path.isfile(ISSUE_LOG):
            try:
                with open(ISSUE_LOG, encoding='utf-8') as f:
                    items = json.load(f) or []
            except Exception:
                items = []
        entry = {'时间': now(), '项目': pname, '投标单位': unit,
                 '类别': category, '现象': note}
        entry.update(kw)
        items.append(entry)
        items = items[-500:]
        os.makedirs(WORK, exist_ok=True)
        with open(ISSUE_LOG, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception:
        traceback.print_exc()


def issue_list(pname=''):
    """读复核问题反馈（可按项目过滤），最新的在前。"""
    items = []
    if os.path.isfile(ISSUE_LOG):
        try:
            with open(ISSUE_LOG, encoding='utf-8') as f:
                items = json.load(f) or []
        except Exception:
            items = []
    if pname:
        items = [x for x in items if (x.get('项目') or '') == pname]
    items.reverse()
    return items
