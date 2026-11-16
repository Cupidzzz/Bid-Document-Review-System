# -*- coding: utf-8 -*-
"""start_server.cmd 的辅助检查器（把危险的内联 python -c 挪到这里，避免 cmd 引号地狱）。

用法：
    python _start_check.py wake        # 打印实际唤醒目标；解析失败退出码 1
    python _start_check.py port        # 8767 已被监听 → 退出码 0；空闲 → 1
    python _start_check.py mark        # 打印 server.out.log 当前行数（启动前调用）
    python _start_check.py banner N    # 只打印第 N 行之后的内容，并判断启动是否成功
"""
import os
import socket
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 本脚本在子目录；HERE 仍指 pipeline 根
LOG = os.path.join(HERE, 'server.out.log')
sys.path.insert(0, HERE)


def wake_target():
    try:
        import wake
        sid, src, _t = wake.resolve_session()
    except Exception as e:
        print('Wake  : 解析失败（%r）' % (e,))
        return 1
    if not sid:
        print('Wake  : (none)   来源：%s' % src)
        print('        FAIL: 没有唤醒目标，前端会显示 wake failed')
        return 1
    print('Wake  : %s   [来源: %s]' % (sid, src))
    print('        OK: 唤醒目标已解析')
    return 0


def port_busy():
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(('127.0.0.1', 8767)) == 0
    finally:
        s.close()


def mark():
    n = 0
    if os.path.isfile(LOG):
        with open(LOG, 'rb') as f:
            for _ in f:
                n += 1
    print(n)
    return 0


def banner(offset):
    if not os.path.isfile(LOG):
        print('(server.out.log 不存在)')
        return 1
    lines = []
    with open(LOG, encoding='utf-8', errors='replace') as f:
        for i, ln in enumerate(f):
            if i >= offset:
                lines.append(ln.rstrip())
    if not lines:
        print('(启动后日志仍然是空的——服务可能没起来)')
        return 1
    keys = ('评分复核服务已启动', '版本', '唤醒', '绑定', '本机', '局域网', '注意')
    picked = [x for x in lines if any(k in x for k in keys)]
    bad = [x for x in lines
           if ('Traceback' in x) or ('无法绑定' in x) or ('Error' in x and 'python' in x.lower())]
    if bad:
        print('!!! 启动失败，日志如下：')
        for x in bad[:8]:
            print('   ' + x)
        return 1
    if picked:
        for x in picked[:8]:
            print('   ' + x)
        return 0
    print('(未找到启动横幅，最近几行如下)')
    for x in lines[-6:]:
        print('   ' + x)
    return 1


def main(argv):
    cmd = argv[0] if argv else ''
    if cmd == 'wake':
        return wake_target()
    if cmd == 'port':
        return 0 if port_busy() else 1
    if cmd == 'mark':
        return mark()
    if cmd == 'banner':
        off = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
        return banner(off)
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
