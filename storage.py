# -*- coding: utf-8 -*-
"""storage.py —— 存储工具（原子写 + 备份），零依赖 server 其他部分，便于单测。

从 server.py 拆出（P2-1 第二步）：backup_once / atomic_write_json。
"""
import json
import os
import shutil
import traceback


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
