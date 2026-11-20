# -*- coding: utf-8 -*-
"""版本号与构建标识（单一口径，别再散落到别处）

规则见 `版本与发布.md`：
    MAJOR  接口/数据结构不兼容（队列 JSON、复核结果 JSON 字段、API 路径改了）
    MINOR  新增功能（新端点、新任务类型、新字段、新的判分能力）
    PATCH  缺陷修复、性能优化、口径不变的重构

**版本号只有一个**（整体发布号）。但"构建标识"分两套，因为两者的生效方式不同：
    - 服务端构建：改完必须**重启进程**才生效
    - 界面构建  ：改完只需**刷新浏览器**（界面文件是每次请求从磁盘读的）
分开发算，才能一眼看出"是哪一边旧了"，这是以前踩过的坑
（界面已更新、服务端没重启 → 前端调用不存在的接口 → 404）。

另有一个 `接口契约`（API_VERSION）：只在**端点或字段不兼容**时递增，
界面声明自己需要的契约号，服务端返回自己的，谁旧了一比对就知道。

改版本号只改这一处；`/api/version`、启动横幅、界面徽标都读它。
"""

import os

__version__ = '1.12.0'

# 接口契约号：新增/改字段不动它，**不兼容**才 +1
API_VERSION = 1

# 服务端：改这些必须重启进程。
# L18：目录扫描（排除 _ 前缀），不再手列——手列漏文件会导致"服务端需重启"不亮。
def _scan_server_py():
    _d = os.path.dirname(os.path.abspath(__file__))
    return tuple(sorted(fn for fn in os.listdir(_d)
                        if fn.endswith('.py') and not fn.startswith('_')))

SERVER_FILES = _scan_server_py()

# 界面：改这些只需刷新浏览器
UI_FILES = ('ui/main.html',)

# 兼容旧调用（历史版本把两边算在一起）
CORE_FILES = SERVER_FILES + UI_FILES


def _stamp(base_dir, files):
    """构建标识 = 文件里最新的修改时间 + 总字节数；用于区分"同版本的不同构建"。

    不依赖 git（沙箱里跑不了子进程管道），纯本地可算。
    """
    import os
    import time
    newest = 0.0
    total = 0
    for rel in files:
        p = os.path.join(base_dir, rel)
        try:
            st = os.stat(p)
        except OSError:
            continue
        newest = max(newest, st.st_mtime)
        total += st.st_size
    if not newest:
        return '', 0.0
    return ('%s+%d' % (time.strftime('%Y-%m-%d %H:%M', time.localtime(newest)), total),
            newest)


def build_stamp(base_dir):
    """旧口径：服务端+界面合在一起（保留给历史调用）"""
    return _stamp(base_dir, CORE_FILES)[0]


def info(base_dir):
    """给 /api/version 与启动横幅用。

    '服务端需重启' 的判断：磁盘上服务端代码的最新 mtime 晚于进程启动时间
    → 说明"代码改了但没重启"，这是最容易踩的坑。
    """
    s_stamp, s_mtime = _stamp(base_dir, SERVER_FILES)
    u_stamp, u_mtime = _stamp(base_dir, UI_FILES)
    return {
        '版本': __version__,
        '接口契约': API_VERSION,
        '服务端构建': s_stamp,
        '界面构建': u_stamp,
        '构建': s_stamp,          # 历史字段，等同服务端构建
        '服务端修改时刻': round(s_mtime, 0) if s_mtime else None,
        '界面修改时刻': round(u_mtime, 0) if u_mtime else None,
    }
