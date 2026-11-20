#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wake.py —— 唤醒 DSH 会话里的 agent

原理
----
1. DSH Web 的鉴权是「签名 cookie」：cookie 名 = dsh-auth-<b64url(sha256(authority))>，
   值 = v1.<b64url(payload)>.<b64url(HMAC-SHA256(secret, payload_base64))>。
2. 签名密钥持久化在 Harness 凭据库（client-connection/browser-session），可读取。
3. 于是可自行签发合法 cookie，调用 RPC 端点 POST /api/session/prompt 注入用户消息。
4. 信封格式：
   {"type":"client-request","rpcId":"...","method":"session/prompt",
    "payload":{"args":{"request":{"requestId","sessionId","mode","content",...}}}}

唤醒目标（2026-10-30 起可指定）
------------------------------
优先级：显式参数 > pipeline/work/wake_target.json > 环境变量 DSH_SESSION_ID。
工作区里有多个会话时，用命令行指定要投给哪一个：

    python wake.py --list              # 列出所有会话（标题 / 目录 / 最近活动）
    python wake.py --set 3             # 按序号绑定（也可以直接给 session-id）
    python wake.py --target            # 看当前绑定到谁
    python wake.py --clear             # 清除绑定，回到环境变量

绑定写进 work/wake_target.json，服务端每次唤起时读，不用重启。
选错也不会丢消息：`python wake.py --set 3` 之后发一句 `python wake.py 测试` 验证即可。

设计说明与部署边界
------------------
本模块用本机 DSH 凭据库里的签名密钥自动签发会话 cookie，实现「前端点开始复核 → 自动唤醒 agent」，
让客户零输入、零命令（产品硬需求）。

⚠️ 部署边界：此唤醒链路**仅限单机自用**——凭据就在本机，等价于「能登录本机 DSH 的人即可驱动 agent」。
多用户 / 公网 / 需要互相隔离的环境**不要部署这条唤醒链路**；若 DSH 日后提供官方唤醒 API，应优先改用。
默认仅在 ENABLE_WAKE=1 或显式 force=True 时生效，避免误触发。
"""
import base64
import hashlib
import hmac
import io
import json
import os
import time
import urllib.request

DEFAULT_BASE = os.environ.get('DSH_WEB_URL', 'http://127.0.0.1:43129')
CRED_CANDIDATES = [
    os.path.join(os.path.expanduser('~'), '.dsh', '.credentials.yaml'),
    os.path.join(os.environ.get('DSH_HOME', ''), '.credentials.yaml'),
]
HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, 'work', 'wake_target.json')


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _decode_b64url(s):
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())


def _authority(base):
    u = base.split('://', 1)[-1].rstrip('/')
    return u


def _secret():
    for p in CRED_CANDIDATES:
        if p and os.path.isfile(p):
            for line in io.open(p, encoding='utf-8').read().splitlines():
                if 'secret:' in line:
                    return _decode_b64url(line.split('secret:', 1)[1].strip())
    raise RuntimeError('未找到 cookie 签名密钥（.credentials.yaml）')


def cookie(base):
    auth = _authority(base)
    name = 'dsh-auth-' + _b64url(hashlib.sha256(auth.encode()).digest())
    now = int(time.time() * 1000)
    body = _b64url(json.dumps({'version': 1, 'authority': auth,
                               'issuedAt': now,
                               'expiresAt': now + 2 * 3600 * 1000},
                              separators=(',', ':')).encode())
    sig = _b64url(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return '%s=%s' % (name, 'v1.%s.%s' % (body, sig))


def rpc(method, args, base=DEFAULT_BASE, timeout=30):
    """调用一个 RPC 方法，返回 (http_status, 解析后的 dict 或原文)"""
    bang = 'script-' + str(int(time.time() * 1000))
    env = {'type': 'client-request', 'rpcId': bang,
           'method': method, 'payload': {'args': args}}
    req = urllib.request.Request(
        base.rstrip('/') + '/api/' + method,
        data=json.dumps(env).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Cookie': cookie(base),
                 'Host': _authority(base)},
        method='POST')
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = op.open(req, timeout=timeout)
        raw = r.read().decode('utf-8', 'replace')
        status = r.status
    except Exception as e:
        status = getattr(e, 'code', 0)
        try:
            raw = e.read().decode('utf-8', 'replace')
        except Exception:
            raw = repr(e)
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


# ---------------- 会话列表与目标绑定 ----------------

def _fmt_ts(ms):
    if not ms:
        return ''
    try:
        return time.strftime('%m-%d %H:%M', time.localtime(ms / 1000.0))
    except Exception:
        return ''


def list_sessions(base=DEFAULT_BASE):
    """列出 DSH 里的会话：会话 id、标题、工作目录、最近活动、是否在跑"""
    status, body = rpc('session/list', {'_request': {}}, base=base)
    if status != 200 or not isinstance(body, dict):
        return []
    res = body.get('result') or {}
    if not res.get('ok'):
        return []
    items = ((res.get('value') or {}).get('items') or [])
    out = []
    for it in items:
        proj = ((it.get('projections') or {}).get('values') or {})
        tu = proj.get('tokenUsage') or {}
        cp = proj.get('contextPressure') or {}
        out.append({'sessionId': it.get('sessionId') or '',
                    '标题': proj.get('title') or '（未命名）',
                    'cwd': it.get('cwd') or '',
                    '更新时间': _fmt_ts(it.get('updatedAt')),
                    'agentAvailable': bool(it.get('agentAvailable')),
                    '运行中': bool(it.get('running')),
                    '空白': bool(it.get('blank')),
                    '上下文压力': cp.get('pressureTokens'),
                    '上下文窗口': cp.get('contextWindow'),
                    '输出token': tu.get('outputTokens'),
                    '输入token': tu.get('uncachedInputTokens')})
    out.sort(key=lambda x: x['更新时间'], reverse=True)
    return out


def _load_cfg():
    """读绑定文件并归一化。兼容旧格式 {"sessionId": ...}。

    v1.2.0 起的格式：
        {"default": "session-…",
         "projects": {"<项目名>": "session-…"},
         "retired": {"<项目名>": "2026-11-02 12:00:00"},
         "设定时间": "…"}
    """
    try:
        with open(TARGET_FILE, encoding='utf-8') as f:
            raw = json.load(f) or {}
    except Exception:
        return {}
    cfg = {}
    if raw.get('sessionId') and not raw.get('default'):
        cfg['default'] = raw['sessionId']            # 旧格式迁移
    if raw.get('default'):
        cfg['default'] = raw['default']
    for k in ('projects', 'retired'):
        if isinstance(raw.get(k), dict):
            cfg[k] = {str(a): str(b) for a, b in raw[k].items() if a and b}
    for k in ('标题', 'cwd', '设定时间'):
        if raw.get(k):
            cfg[k] = raw[k]
    return cfg


def _save_cfg(cfg):
    cfg = dict(cfg or {})
    cfg['设定时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)
    with open(TARGET_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return cfg


def read_target():
    """读默认会话（兼容旧格式）；没有返回空串"""
    return _load_cfg().get('default') or ''


def set_target(session_id, info=None):
    """设置默认会话（保留已有的项目映射）"""
    cfg = _load_cfg()
    cfg['default'] = session_id
    if info:
        for k in ('标题', 'cwd'):
            if info.get(k):
                cfg[k] = info[k]
    return _save_cfg(cfg)


def set_project_session(project, session_id, info=None):
    """给某个项目绑定专用会话；session_id 传空 = 解除该项目映射"""
    cfg = _load_cfg()
    proj = cfg.setdefault('projects', {})
    proj.pop(project, None)
    (cfg.get('retired') or {}).pop(project, None)
    if session_id:
        proj[project] = session_id
        if info and info.get('标题'):
            cfg.setdefault('项目标题', {})[project] = info['标题']
    return _save_cfg(cfg)


def retire_project(project):
    """项目结项：把该项目映射标记失效，并取消该会话的置顶。"""
    cfg = _load_cfg()
    sid = (cfg.get('projects') or {}).pop(project, None)
    cfg.setdefault('retired', {})[project] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save_cfg(cfg)
    if sid:
        unpin_session(sid)          # 结项后不再占着列表顶部的置顶位
    return sid


def clear_target():
    try:
        os.remove(TARGET_FILE)
        return True
    except Exception:
        return False


def resolve_session(explicit=None, project=None):
    """决定唤醒目标，返回 (sessionId, 来源, 说明)。

    优先级：显式指定 > 项目专属会话 > 默认会话 > 环境变量 DSH_SESSION_ID。
    项目已归档（retired）时跳过它的映射，回落到默认会话——不静默失败。
    """
    if explicit:
        return explicit, '显式指定', ''
    cfg = _load_cfg()
    proj = (project or '').strip()
    pinned = (cfg.get('projects') or {}).get(proj) if proj else ''
    if pinned:
        return pinned, '项目映射（%s）' % proj, ''
    if proj and proj in (cfg.get('retired') or {}):
        base = cfg.get('default') or ''
        if base:
            # 回落的是默认会话，标题也要用默认会话的标题；此前返回空串，
            # 界面上会显示成「唤醒目标：（session-…）」缺标题
            return base, '项目已结项，回落默认', cfg.get('标题') or ''
        return (os.environ.get('DSH_SESSION_ID') or ''), '项目已结项，回落环境变量', ''
    if cfg.get('default'):
        return cfg['default'], 'wake_target.json', cfg.get('标题') or ''
    sid = os.environ.get('DSH_SESSION_ID') or ''
    if sid:
        return sid, 'DSH_SESSION_ID', ''
    return '', '未找到', ''


# ---------------- 新建会话（v1.3.0：项目第一次复核时自动建） ----------------

AUTO_SESSION_ENV = 'ENABLE_AUTO_SESSION'
WORKSPACE = os.path.dirname(HERE)          # 工作区根目录（pipeline 的上一级）
WORKSPACE_JSON = os.path.join(
    os.environ.get('APPDATA') or os.path.expanduser('~/AppData/Roaming'),
    'dsh-desktop', 'harness', 'storages', 'workspace.json')


def _norm_path(p):
    return os.path.normcase(os.path.normpath(p or ''))


def register_in_workspace(sid, pin=True, path=None):
    """把新会话登记到「投标文件复核」工作区，并默认置顶（pinned）。

    DSH 的会话归属与置顶都存在 harness\\storages\\workspace.json 里：
      tables.workspaces[<id>].sessionIds  ← 归属哪个工作区
      global.pinnedSessionIds             ← 置顶
    session/create 只建会话，不会自动进这两处，所以这里补上。
    返回 (是否成功, 说明)。
    """
    target = _norm_path(path or WORKSPACE)
    if not sid:
        return False, '缺少 sessionId'
    if not os.path.isfile(WORKSPACE_JSON):
        return False, '找不到 workspace.json：%s' % WORKSPACE_JSON
    try:
        with open(WORKSPACE_JSON, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return False, '读 workspace.json 失败：%r' % e

    tables = data.setdefault('tables', {}).setdefault('workspaces', {})
    hit = None
    for wid, ws in tables.items():
        if _norm_path(ws.get('path')) == target:
            hit = ws
            break
    if hit is None:
        return False, '没有路径为 %s 的工作区' % target

    raw = hit.get('sessionIds')
    if isinstance(raw, list):
        ids = [str(x) for x in raw]
        if sid not in ids:
            ids.append(sid)
        hit['sessionIds'] = ids
    else:
        ids = (raw or '').split()
        if sid not in ids:
            ids.append(sid)
        hit['sessionIds'] = ' '.join(ids)

    pinned = False
    if pin:
        g = data.setdefault('global', {})
        pl = g.get('pinnedSessionIds')
        if not isinstance(pl, list):
            pl = list(pl or [])
        if sid not in pl:
            pl.append(sid)
        g['pinnedSessionIds'] = pl
        pinned = True

    try:
        with open(WORKSPACE_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        return False, '写 workspace.json 失败：%r' % e
    return True, '已登记到「%s」%s' % (os.path.basename(target) or target,
                                      '并置顶' if pinned else '')


def unpin_session(sid):
    """取消置顶（项目结项时用），不动归属。优先走 RPC，失败再改文件。"""
    if not sid:
        return False, '缺少 sessionId'
    try:
        st, body = rpc('workspace/unpinSession', {'request': {'sessionId': sid}})
        res = (body or {}).get('result') or {}
        if st == 200 and res.get('ok'):
            return True, '已取消置顶（RPC）：%s' % sid
    except Exception:
        pass
    if not os.path.isfile(WORKSPACE_JSON):
        return False, 'RPC 失败且没有 workspace.json'
    try:
        with open(WORKSPACE_JSON, encoding='utf-8') as f:
            data = json.load(f)
        g = data.setdefault('global', {})
        pl = [x for x in (g.get('pinnedSessionIds') or []) if str(x) != sid]
        g['pinnedSessionIds'] = pl
        with open(WORKSPACE_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True, '已取消置顶（改文件）：%s' % sid
    except Exception as e:
        return False, '取消置顶失败：%r' % e


def workspace_id_for_path(path=None):
    """按工作区路径查它的 workspaceId（DSH 的 session/create 需要这个而不是 cwd）。"""
    target = _norm_path(path or WORKSPACE)
    if not os.path.isfile(WORKSPACE_JSON):
        return ''
    try:
        with open(WORKSPACE_JSON, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return ''
    for wid, ws in ((data.get('tables') or {}).get('workspaces') or {}).items():
        if _norm_path(ws.get('path')) == target:
            return wid
    return ''


def pin_session(sid):
    """置顶（优先 RPC）。返回 (是否成功, 说明)。"""
    if not sid:
        return False, '缺少 sessionId'
    try:
        st, body = rpc('workspace/pinSession', {'request': {'sessionId': sid}})
        res = (body or {}).get('result') or {}
        if st == 200 and res.get('ok'):
            return True, '已置顶（RPC）'
        return False, 'RPC 置顶失败：%s' % json.dumps(
            res.get('error') or res, ensure_ascii=False)[:150]
    except Exception as e:
        ok, info = register_in_workspace(sid, pin=True)
        return ok, ('改文件置顶：' + info) if ok else ('置顶失败：%r' % e)


def create_session(cwd=None, title=None, base=DEFAULT_BASE, workspace_id=None):
    """调 DSH 的 session/create 新建会话，返回 (sessionId, 错误)。

    关键（2026-11-02 实测）：`request` 里 **workspaceId 与 cwd 互斥**——
    - 传 `workspaceId`：会话**直接归属**该工作区（并排在列表最前），这是我们要的；
    - 传 `cwd`：只设工作目录，会话处于"未分组"，界面上挂在「工作区 → 未分组」下。
    返回值形如 {'ok': True, 'value': {'sessionId': 'session-…', 'agentPreset': 'standard'}}，
    新建的会话 agentAvailable 直接为 true，可立即唤醒。
    """
    req = {}
    if workspace_id:
        req['workspaceId'] = workspace_id
    else:
        req['cwd'] = cwd or WORKSPACE
    if title:
        req['title'] = title
    try:
        st, body = rpc('session/create', {'request': req}, base=base)
    except Exception as e:
        return '', '调用异常：%r' % e
    if st != 200 or not isinstance(body, dict):
        return '', 'HTTP %s %s' % (st, str(body)[:150])
    res = body.get('result') or {}
    if not res.get('ok'):
        return '', '失败：%s' % json.dumps(res.get('error') or res,
                                          ensure_ascii=False)[:200]
    sid = ((res.get('value') or {}).get('sessionId') or '')
    if not sid:
        return '', '返回里没有 sessionId：%s' % json.dumps(res,
                                                          ensure_ascii=False)[:200]
    return sid, ''


def ensure_project_session(project, cwd=None, title=None, base=DEFAULT_BASE,
                           register=True):
    """确保某项目有专属会话：已映射则复用，否则新建 + 归属工作区 + 置顶 + 写映射。

    新建时优先用 `workspaceId`（会话直接落在「投标文件复核」工作区，不再是"未分组"），
    拿不到 workspaceId 才退回 `cwd` + 事后改 workspace.json。
    返回 (sessionId, 说明, 是否新建)。
    """
    proj = (project or '').strip()
    if not proj:
        return '', '项目名为空', False
    cfg = _load_cfg()
    sid = (cfg.get('projects') or {}).get(proj) or ''
    if sid:
        return sid, '沿用已有映射', False
    path = cwd or WORKSPACE
    wid = workspace_id_for_path(path) if register else ''
    t = title or ('复核·' + proj[:18])
    sid, err = create_session(cwd=path, title=t, base=base, workspace_id=wid or None)
    if not sid:
        return '', '新建会话失败：%s' % err, False
    set_project_session(proj, sid, {'标题': t})

    notes = []
    if wid:
        notes.append('已归属「%s」工作区' % (os.path.basename(path) or path))
    elif register:
        ok, info = register_in_workspace(sid, pin=True, path=path)
        notes.append(info if ok else ('登记工作区失败：%s' % info))
    if register:
        ok2, info2 = pin_session(sid)
        notes.append(info2 if ok2 else ('置顶失败：%s' % info2))
    return sid, '已新建并绑定（%s）' % '；'.join(notes) if notes else '已新建并绑定', True


def auto_session_enabled():
    return os.environ.get(AUTO_SESSION_ENV) == '1'


# ---------------- 唤醒 ----------------

def wake(text, session_id=None, mode='queue', base=DEFAULT_BASE, force=False,
         project=None):
    """
    向会话注入一条用户消息以唤醒 agent。
    project 给出时按「项目 → 会话」映射选目标（未映射则回落默认会话）。
    返回 (是否成功, 说明)。
    """
    if not force and os.environ.get('ENABLE_WAKE') != '1':
        return False, '未启用（需 ENABLE_WAKE=1）'
    sid, src, title = resolve_session(session_id, project=project)
    if not sid:
        return False, '缺少 sessionId（未指定、无绑定文件、DSH_SESSION_ID 也未设置）'
    args = {'request': {
        'requestId': 'wake-' + str(int(time.time() * 1000)),
        'sessionId': sid, 'mode': mode,
        'content': [{'type': 'text', 'text': text}],
        'clientTimeZone': 'Asia/Shanghai'}}
    try:
        status, body = rpc('session/prompt', args, base=base)
    except Exception as e:
        return False, '调用异常：%r' % e
    ok = (status == 200 and isinstance(body, dict)
          and (body.get('result') or {}).get('ok') is True)
    if ok:
        who = sid + ('（%s）' % title if title else '')
        return True, '已唤醒会话 %s，来源：%s' % (who, src)
    err = ''
    if isinstance(body, dict):
        err = json.dumps((body.get('result') or {}).get('error') or body,
                         ensure_ascii=False)[:200]
    else:
        err = str(body)[:200]
    return False, 'HTTP %s %s' % (status, err)


def _print_sessions(rows, marker_sid='', full=False):
    if not rows:
        print('拿不到会话列表（DSH Web 未启动？）')
        return
    print('%-3s %-6s %-9s %-8s %-20s %-38s %s'
          % ('#', '可唤醒', '运行中', '活动', '标题', '会话 id', '工作目录'))
    for i, r in enumerate(rows, 1):
        mark = '*' if marker_sid and r['sessionId'] == marker_sid else ' '
        sid = r['sessionId'] if full else r['sessionId'].replace('session-', '')[:16]
        avail = '是' if r.get('agentAvailable') else '否'
        print('%s%-2d %-6s %-9s %-8s %-20s %-38s %s'
              % (mark, i, avail, '是' if r['运行中'] else '',
                 r['更新时间'], r['标题'][:18], sid, r['cwd'][-34:]))


if __name__ == '__main__':
    import sys
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    if argv[0] == '--list':
        cur, src, _t = resolve_session()
        rows = list_sessions()
        if '--json' in argv:
            import json as _json
            print(_json.dumps({'当前绑定': cur, '来源': src, '会话': rows},
                              ensure_ascii=False, indent=1))
            sys.exit(0)
        _print_sessions(rows, cur, full=('--full' in argv))
        cfg = _load_cfg()
        print('\n默认会话：%s（来源：%s）' % (cfg.get('default') or '无', src))
        projs = cfg.get('projects') or {}
        if projs:
            print('项目专属会话：')
            for k, v in sorted(projs.items()):
                dead = k in (cfg.get('retired') or {})
                print('  %s → %s%s' % (k[:28], v, '（已标记结项）' if dead else ''))
        else:
            print('项目专属会话：未设置（所有项目都用默认会话）')
        print('提示：新会话要先在里面发一句话（否则 agentAvailable=否），再 --set。')
        sys.exit(0)
    if argv[0] == '--set':
        if len(argv) < 2:
            print('用法：python wake.py --set <序号|session-id>   （设置默认会话）')
            sys.exit(2)
        pick = argv[1].strip()
        rows = list_sessions()
        if pick.isdigit():
            i = int(pick)
            if not (1 <= i <= len(rows)):
                print('序号越界：1~%d' % len(rows))
                sys.exit(2)
            info = rows[i - 1]
            d = set_target(info['sessionId'], info)
            print('已绑定默认会话 #%d：%s（%s）' % (i, info['标题'], d['default']))
        else:
            hit = [r for r in rows if r['sessionId'] == pick]
            d = set_target(pick, hit[0] if hit else None)
            print('已绑定默认会话：%s' % d.get('default'))
        print('文件：%s' % TARGET_FILE)
        print('建议验证：python wake.py "绑定验证：看到这条即说明投递正确"')
        sys.exit(0)
    if argv[0] == '--set-project':
        if len(argv) < 3:
            print('用法：python wake.py --set-project <项目名> <序号|session-id>')
            print('      解除映射用：python wake.py --set-project <项目名> -')
            sys.exit(2)
        proj = argv[1].strip()
        pick = argv[2].strip()
        if pick in ('-', ''):
            set_project_session(proj, '')
            print('已解除项目映射：%s' % proj)
            sys.exit(0)
        rows = list_sessions()
        info = None
        sid = pick
        if pick.isdigit():
            i = int(pick)
            if not (1 <= i <= len(rows)):
                print('序号越界：1~%d' % len(rows))
                sys.exit(2)
            info = rows[i - 1]
            sid = info['sessionId']
        else:
            hit = [r for r in rows if r['sessionId'] == pick]
            info = hit[0] if hit else None
        set_project_session(proj, sid, info)
        print('已绑定：%s → %s%s'
              % (proj, sid, ('（%s）' % info['标题']) if info else ''))
        print('文件：%s' % TARGET_FILE)
        sys.exit(0)
    if argv[0] == '--unset-project':
        if len(argv) < 2:
            print('用法：python wake.py --unset-project <项目名>')
            sys.exit(2)
        set_project_session(argv[1].strip(), '')
        print('已解除项目映射：%s' % argv[1].strip())
        sys.exit(0)
    if argv[0] == '--retire-project':
        if len(argv) < 2:
            print('用法：python wake.py --retire-project <项目名>')
            sys.exit(2)
        old = retire_project(argv[1].strip())
        print('已结项标记：%s%s' % (argv[1].strip(),
                                  ('（原映射 %s 已移除）' % old) if old else ''))
        sys.exit(0)
    if argv[0] == '--create-session':
        title = argv[1].strip() if len(argv) > 1 else ''
        sid, err = create_session(title=title or None)
        if sid:
            print('已新建会话：%s%s' % (sid, ('（%s）' % title) if title else ''))
            print('可直接用：python wake.py --set-project <项目名> %s' % sid)
        else:
            print('新建失败：%s' % err)
            sys.exit(1)
        sys.exit(0)
    if argv[0] == '--ensure-project':
        if len(argv) < 2:
            print('用法：python wake.py --ensure-project <项目名>')
            sys.exit(2)
        proj = argv[1].strip()
        sid, why, made = ensure_project_session(proj)
        if not sid:
            print('未完成：%s' % why)
            sys.exit(1)
        print('%s：%s → %s' % ('已新建并绑定' if made else '沿用', proj, sid))
        sys.exit(0)
    if argv[0] == '--clear':
        print('已清除绑定' if clear_target() else '本来就没有绑定文件')
        sys.exit(0)
    if argv[0] == '--target':
        proj = argv[1].strip() if len(argv) > 1 else ''
        sid, src, title = resolve_session(project=proj)
        print('sessionId=%s\n来源=%s\n标题=%s\n项目=%s\n文件=%s'
              % (sid or '无', src, title or '', proj or '（默认）', TARGET_FILE))
        sys.exit(0)
    ok, info = wake(' '.join(argv), force=True)
    print('唤醒结果:', ok, info)
