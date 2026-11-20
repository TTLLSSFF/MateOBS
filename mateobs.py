#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MateOBS — 直播伴侣「obs直播」模式解锁 + 内存优化（v1.3）

单文件工具，纯本地、无需联网，只做三件事：
  1. 找到直播伴侣安装目录与 resources/app/app.content；
  2. 用字节级规则打开 OBS 模式的两道开关（自动备份、可一键还原）；
  3. 以管理员身份拉起伴侣 / OBS，并可优化进程内存。

用法：
    python mateobs.py                  # 打开 GUI
    python mateobs.py --status         # 控制台：检测 + 规则命中情况
    python mateobs.py --apply          # 控制台：打补丁（需管理员）
    python mateobs.py --restore        # 控制台：从备份还原
    python mateobs.py --launch mate    # 启动伴侣（管理员）
    python mateobs.py --launch obs     # 启动 OBS
    python mateobs.py --mate-dir DIR   # 指定伴侣安装目录（默认自动探测）
    python mateobs.py --keep-running   # 打补丁时不结束伴侣进程
    python mateobs.py --memopt mate    # 立即优化伴侣 + MediaSDK 内存
    python mateobs.py --memopt all     # 立即优化全部进程内存（默认只碰 >200MB 的）
    python mateobs.py --memopt-watch mate --interval 60
                                       # 常驻定时优化（间隔 1..3600 秒，Ctrl+C 停）
"""

import ctypes
import ctypes.wintypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

APP_NAME = "MateOBS"
APP_VER = "1.3"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
BACKUP_ROOT = os.path.join(HERE, "backups")
LOG_PATH = os.path.join(HERE, "mateobs.log")

# ----------------------------------------------------------------------------
# 规则表：全部是"字节级正则 → 替换"，不依赖文件编码，也不依赖压缩后的变量名。
# scope: None=全部 js；否则文件名必须命中其中之一（子串匹配，大小写不敏感）。
# ----------------------------------------------------------------------------
RULES = [
    dict(id="R03", scope=None, desc="appStore 初始 hasObsStream false → true",
         pat=rb"hasObsStream:!1",
         rep=rb"hasObsStream:!0",
         ver=rb"hasObsStream:!0"),
    dict(id="R05", scope=None, desc="开播权限弹窗直接放行",
         pat=rb"t=await this\.confirmNewPermission\(\);",
         rep=rb"t={code:nc.hn.Success};",
         ver=rb"t=\{code:nc\.hn\.Success\};"),
    dict(id="R06", scope=None, desc="get_pc_obs_status：status_code 分支永假",
         pat=rb"0!==(\w)\|\|!t\)throw new Error\(",
         rep=rb"0===1)throw new Error(",
         ver=rb"0===1\)throw new Error\("),
    dict(id="R07", scope=None, desc="pc_obs_status!==1 的『权限校验失败』永假",
         pat=rb";if\(1!==(\w)\)throw new Error\(",
         rep=rb";if(0===1)throw new Error(",
         ver=rb";if\(0===1\)throw new Error\("),
    dict(id="R08", scope=None, desc="pc_obs_status 解构兜底（接口无 data 也不炸）",
         pat=rb"const\{pc_obs_status:(\w)\}=t;",
         rep=rb"const{pc_obs_status:\1}={pc_obs_status:1};",
         ver=rb"const\{pc_obs_status:\w\}=\{pc_obs_status:1\};"),
    dict(id="R10", scope=None, desc="建房参数 push_stream_type 恒为 1（OBS 模式也按普通房间建房，绕过服务端 OBS 权限门）",
         pat=rb'push_stream_type:"obs"===this\.mode\?2:1',
         rep=rb"push_stream_type:1",
         ver=rb"push_stream_type:1,auto_cover"),
    dict(id="R09", scope=None, desc="模式下拉：无条件渲染 OBS 项",
         pat=rb"!de\|\|At\.APP_ID===w\.APP_ID\.NEW_HUOSHAN",
         rep=rb"!1",
         ver=rb"LIVE_MODE\.OBS&&\(!1\)"),
]


MAX_JS_BYTES = 24 * 1024 * 1024     # 单文件上限，超过跳过（app.content 里没有这么大的）


# ----------------------------------------------------------------------------
# 基础设施
# ----------------------------------------------------------------------------
def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate(argv=None):
    """以管理员身份重启自己（UAC 一次）。"""
    params = " ".join('"%s"' % a for a in (argv or sys.argv))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, HERE, 1)
    return rc > 32


def shell_runas(path, args=""):
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, args or None, os.path.dirname(path), 1)
    return rc > 32


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# 探测：伴侣安装目录 / app.content / OBS
# ----------------------------------------------------------------------------
def find_mate_dirs():
    """返回候选安装根目录列表（含 launcher_config.json 指向的当前版本优先）。"""
    cands = []
    cfg = load_config()
    if cfg.get("mate_dir"):
        cands.append(cfg["mate_dir"])
    for base in (
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "webcast_mate"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "webcast_mate"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "webcast_mate"),
    ):
        if not base or not os.path.isdir(base):
            continue
        cands.append(base)
        lc = os.path.join(base, "launcher_config.json")
        if os.path.isfile(lc):
            try:
                with open(lc, encoding="utf-8") as f:
                    cur = json.load(f).get("cur_path")
                if cur:
                    cands.insert(0, os.path.join(base, cur))
            except (OSError, ValueError):
                pass
    out, seen = [], set()
    for c in cands:
        c = os.path.normpath(c)
        if c.lower() in seen or not os.path.isdir(c):
            continue
        seen.add(c.lower())
        out.append(c)
    return out


def find_app_content(mate_dir):
    """伴侣根目录或某个版本子目录都可以传进来。"""
    if not mate_dir or not os.path.isdir(mate_dir):
        return None
    p = os.path.join(mate_dir, "resources", "app", "app.content")
    if os.path.isdir(p):
        return p
    # 根目录 → 找到版本子目录（取版本号最大的那个）
    subs = []
    for d in os.listdir(mate_dir):
        sub = os.path.join(mate_dir, d)
        if not os.path.isdir(sub):
            continue
        if os.path.isdir(os.path.join(sub, "resources", "app", "app.content")):
            subs.append((version_key(d), sub))
    if subs:
        subs.sort()
        return os.path.join(subs[-1][1], "resources", "app", "app.content")
    return None


def version_key(name):
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


def version_of(mate_dir):
    return os.path.basename(os.path.normpath(mate_dir))


def ver_from_content(app_content):
    """.../<ver>/resources/app/app.content → <ver>"""
    d = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(app_content))))
    return os.path.basename(d)


def pick_target(mate_dir=None):
    """确定 (mate_dir, app_content)。"""
    if mate_dir:
        ac = find_app_content(mate_dir)
        return (os.path.normpath(mate_dir), ac) if ac else (None, None)
    for d in find_mate_dirs():
        ac = find_app_content(d)
        if ac:
            return d, ac
    return None, None


def find_obs():
    cfg = load_config()
    if cfg.get("obs_exe") and os.path.isfile(cfg["obs_exe"]):
        return cfg["obs_exe"]
    for p in (
        r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    ):
        if os.path.isfile(p):
            return p
    steam = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "Steam", "steamapps", "common", "OBS Studio", "bin", "64bit", "obs64.exe")
    return steam if os.path.isfile(steam) else None


def mate_exe(mate_dir):
    for name in ("直播伴侣.exe",):
        p = os.path.join(mate_dir, name)
        if os.path.isfile(p):
            return p
    for d in sorted(os.listdir(mate_dir), reverse=True):
        p = os.path.join(mate_dir, d, "直播伴侣.exe")
        if os.path.isfile(p):
            return p
    return None


def iter_js(app_content):
    for dp, _dn, fn in os.walk(app_content):
        for f in fn:
            if f.lower().endswith(".js"):
                p = os.path.join(dp, f)
                try:
                    if os.path.getsize(p) <= MAX_JS_BYTES:
                        yield p
                except OSError:
                    continue


# ----------------------------------------------------------------------------
# 扫描 / 打补丁 / 还原
# ----------------------------------------------------------------------------
def _scope_ok(rule, path):
    if not rule["scope"]:
        return True
    low = path.lower()
    return any(s.lower() in low for s in rule["scope"])


def _count(pat, data):
    return len(re.findall(pat, data))



def needs_admin(app_content):
    """目标目录是否写不进去（Program Files 下必然是 True）。"""
    probe = os.path.join(app_content, ".mateobs_probe")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True

def scan(app_content, verbose=False):
    """统计每条规则在每个文件里的 [待打, 已打] 命中。"""
    rows = []
    files = list(iter_js(app_content))
    for rule in RULES:
        pending = applied = 0
        hits = []
        for p in files:
            if not _scope_ok(rule, p):
                continue
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            a = _count(rule["pat"], data)
            b = _count(rule["ver"], data)
            if a or b:
                pending += a
                applied += b
                hits.append((os.path.relpath(p, app_content), a, b))
        if verbose and hits:
            print("      " + "; ".join("%s 待打=%d 已打=%d" % h for h in hits))
        rows.append(dict(rule=rule, pending=pending, applied=applied, files=hits))
    return rows


def summarize(rows):
    pend = sum(r["pending"] for r in rows)
    appl = sum(r["applied"] for r in rows)
    return pend, appl


def backup_file(app_content, path, ver):
    rel = os.path.relpath(path, app_content)
    dst = os.path.join(BACKUP_ROOT, ver, rel)
    if os.path.exists(dst):
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(path, dst)
    return dst


def kill_mate(wait=2.0):
    for img in ("直播伴侣.exe", "MediaSDK_Server.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True, timeout=15)
        except Exception:
            pass
    time.sleep(wait)


def apply_patch(app_content, ver, keep_running=False):
    """按规则打补丁，返回 (改动文件数, 改动处数, 消息列表)。"""
    msgs = []
    if not keep_running:
        log("结束直播伴侣进程（保证文件可写）")
        kill_mate()
    pending_total = applied_files = 0
    for rule in RULES:
        for p in iter_js(app_content):
            if not _scope_ok(rule, p):
                continue
            try:
                data = open(p, "rb").read()
            except OSError as e:
                msgs.append("读取失败 %s (%s)" % (p, e))
                continue
            new, n = re.subn(rule["pat"], rule["rep"], data)
            if not n:
                continue
            try:
                backup_file(app_content, p, ver)
                with open(p, "wb") as f:
                    f.write(new)
            except OSError as e:
                msgs.append("写入失败 %s (%s)" % (p, e))
                continue
            applied_files += 1
            pending_total += n
            msg = "%s %s x%d ← %s" % (rule["id"], os.path.relpath(p, app_content), n, rule["desc"])
            msgs.append(msg)
            log(msg)
    return applied_files, pending_total, msgs


def restore(app_content, ver, keep_running=False):
    src_root = os.path.join(BACKUP_ROOT, ver)
    if not os.path.isdir(src_root):
        return 0, "没有 %s 的备份目录：%s" % (ver, src_root)
    if not keep_running:
        kill_mate()
    n = 0
    for dp, _dn, fn in os.walk(src_root):
        for f in fn:
            src = os.path.join(dp, f)
            rel = os.path.relpath(src, src_root)
            dst = os.path.join(app_content, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
                log("还原 %s" % rel)
            except OSError as e:
                log("还原失败 %s (%s)" % (rel, e))
    return n, "已还原 %d 个文件（来自 %s）" % (n, src_root)


def status_text(app_content, ver):
    rows = scan(app_content)
    pend, appl = summarize(rows)
    lines = ["伴侣版本：%s" % ver, "app.content：%s" % app_content, ""]
    for r in rows:
        st = "已打(%d)" % r["applied"] if r["pending"] == 0 and r["applied"] else ("待打(%d)" % r["pending"] if r["pending"] else "不适用")
        lines.append("%s %-4s %s" % (r["rule"]["id"], st, r["rule"]["desc"]))
    lines.append("")
    lines.append("汇总：待打 %d 处 / 已生效 %d 处" % (pend, appl))
    if pend == 0 and appl:
        lines.append("结论：OBS 模式已开启（重启伴侣即可见『obs直播』）")
    elif pend:
        lines.append("结论：尚未打完，建议以管理员运行本工具点『一键开启』")
    else:
        lines.append("结论：规则与当前版本不匹配（可能是新版本，需更新规则表）")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cli(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    mate_dir = None
    if "--mate-dir" in argv:
        mate_dir = argv[argv.index("--mate-dir") + 1]
    keep = "--keep-running" in argv
    d, ac = pick_target(mate_dir)
    if not ac:
        print(log("未找到直播伴侣的 app.content，请用 --mate-dir 指定安装目录"))
        return 2
    ver = ver_from_content(ac)
    print(log("目标：%s（%s）" % (d, ver)))

    if "--status" in argv or len(argv) == 0:
        print(status_text(ac, ver))
        return 0

    if "--repair" in argv:
        if needs_admin(ac) and not is_admin():
            print(log("需要管理员权限，正在请求 UAC …"))
            return 0 if elevate() else 2
        n, msg = restore(ac, ver, keep)
        print(log(msg))
        files, hits, msgs = apply_patch(ac, ver, keep)
        for m in msgs:
            print("  " + m)
        print(log("重建完成：%d 个文件、%d 处替换；备份在 %s" % (files, hits, os.path.join(BACKUP_ROOT, ver))))
        pend, appl = summarize(scan(ac))
        print(log("复检：待打 %d / 已生效 %d" % (pend, appl)))
        return 0

    if "--apply" in argv:
        if needs_admin(ac) and not is_admin():
            print(log("需要管理员权限，正在请求 UAC …"))
            return 0 if elevate() else 2
        files, hits, msgs = apply_patch(ac, ver, keep)
        for m in msgs:
            print("  " + m)
        print(log("完成：%d 个文件、%d 处替换；备份在 %s" % (files, hits, os.path.join(BACKUP_ROOT, ver))))
        pend, appl = summarize(scan(ac))
        print(log("复检：待打 %d / 已生效 %d" % (pend, appl)))
        return 0

    if "--restore" in argv:
        if needs_admin(ac) and not is_admin():
            print(log("需要管理员权限，正在请求 UAC …"))
            return 0 if elevate() else 2
        n, msg = restore(ac, ver, keep)
        print(log(msg))
        return 0

    if "--memopt" in argv or "--memopt-watch" in argv:
        watch = "--memopt-watch" in argv
        key = "--memopt-watch" if watch else "--memopt"
        i = argv.index(key)
        scope = "mate"
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            scope = argv[i + 1]
        interval = 60
        if "--interval" in argv:
            try:
                interval = int(argv[argv.index("--interval") + 1])
            except (ValueError, IndexError):
                interval = 60
        min_mb = 200.0
        if "--min-mb" in argv:
            try:
                min_mb = float(argv[argv.index("--min-mb") + 1])
            except (ValueError, IndexError):
                min_mb = 200.0
        if not is_admin():
            print(log("提示：伴侣以管理员身份运行，非管理员进程优化不了它的内存；本命令以管理员运行效果最好"))
        if not watch:
            memopt_once(scope, min_mb)
            return 0
        runner = MemOptRunner(interval, (scope,), min_mb,
                              on_tick=lambda sc, t, sn, f: print(log(
                                  "  第 %s 次：命中 %d / 成功 %d / 释放 %.1f MB" %
                                  (time.strftime("%H:%M:%S"), sn, t, f))))
        runner.start()
        print(log("定时内存优化已启动：%s，每 %d 秒一次，Ctrl+C 停止" %
                  ("全部进程" if scope == "all" else "伴侣相关进程", runner.interval)))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            runner.stop()
            print(log("定时内存优化：已停止"))
        return 0

    if "--launch" in argv:
        what = argv[argv.index("--launch") + 1]
        if what == "obs":
            exe = find_obs()
            if not exe:
                print(log("没找到 obs64.exe，请在 GUI 里指定"))
                return 2
            return 0 if shell_runas(exe) else 2
        exe = mate_exe(d)
        if not exe:
            print(log("没找到 直播伴侣.exe"))
            return 2
        return 0 if shell_runas(exe) else 2

    print(__doc__)
    return 0


# ----------------------------------------------------------------------------
# 内存优化（working set trim）
# ----------------------------------------------------------------------------
# 常用做法：
#   _trimProcess(name)  -> Injector.findProcesses(name) 逐个
#                          OpenProcess(pid, ..., 0xa20)
#                          SetProcessWorkingSetSize(h, -2, -2)
#                          CloseHandle(h)
#   _trimAllProcesses() -> tasklist /V /FO CSV /NH -> 取「内存使用」列 -> int 解析
#                          -> 跳过系统账户（NT AUTHORITY\*、\SYSTEM、\LOCAL SERVICE、
#                             \NETWORK SERVICE）与内置 19 个排除项
#                          -> 同样 OpenProcess + SetProcessWorkingSetSize + CloseHandle
#   _startMemOpt()      -> 读间隔（int.tryParse，默认 60，clamp 1..3600 秒）
#                          -> Timer.periodic(interval, _trimAllProcesses)
#   两个开关：enable_memopt_mate（优化直播伴侣内存）/ enable_memopt_all（优化全部进程内存）
# 这里用同样的机制重新实现，只是把 tasklist 换成 CreateToolhelp32Snapshot（更快、无子进程）。
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MATE_PROC_NAMES = ("直播伴侣.exe", "MediaSDK_Server.exe", "直播伴侣 Launcher.exe", "obs64.exe")
TRIM_SKIP = {
    "system", "registry", "idle", "secure system", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "winlogon.exe", "fontdrvhost.exe", "logonui.exe", "dwm.exe", "audiodg.exe",
    "deepseek harness.exe", "obs64.exe",
}

if os.name == "nt":
    _wintypes = ctypes.wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", _wintypes.DWORD),
            ("cntUsage", _wintypes.DWORD),
            ("th32ProcessID", _wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", _wintypes.DWORD),
            ("cntThreads", _wintypes.DWORD),
            ("th32ParentProcessID", _wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", _wintypes.DWORD),
            ("szExeFile", _wintypes.WCHAR * 260),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", _wintypes.DWORD),
            ("PageFaultCount", _wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _k32.CreateToolhelp32Snapshot.restype = _wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [_wintypes.DWORD, _wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [_wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [_wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.OpenProcess.restype = _wintypes.HANDLE
    _k32.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
    _k32.CloseHandle.argtypes = [_wintypes.HANDLE]
    _k32.SetProcessWorkingSetSize.argtypes = [_wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
    _k32.SetProcessWorkingSetSize.restype = _wintypes.BOOL
    for _dll in (_k32, _psapi):
        if hasattr(_dll, "EmptyWorkingSet"):
            _dll.EmptyWorkingSet.argtypes = [_wintypes.HANDLE]
            _dll.EmptyWorkingSet.restype = _wintypes.BOOL


def list_processes():
    """[(pid, exe_name)]，按快照顺序。"""
    if os.name != "nt":
        return []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    out = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def proc_ws_bytes(pid):
    """进程工作集（字节）；取不到返回 0。"""
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return 0
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        fn = getattr(_k32, "K32GetProcessMemoryInfo", None) or _psapi.GetProcessMemoryInfo
        if not fn(h, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        _k32.CloseHandle(h)


def trim_pid(pid):
    """把单个进程的工作集交还系统。返回 (ok, before_bytes, after_bytes)。"""
    if os.name != "nt" or not pid or pid <= 4:
        return False, 0, 0
    before = proc_ws_bytes(pid)
    h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False, before, before
    try:
        ok = False
        for dll in (_k32, _psapi):
            fn = getattr(dll, "EmptyWorkingSet", None)
            if fn:
                if fn(h):
                    ok = True
                break
        # 兜底 / 补充：SetProcessWorkingSetSize(h, -1, -1)
        if _k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value):
            ok = True
        return ok, before, proc_ws_bytes(pid)
    finally:
        _k32.CloseHandle(h)


def trim_targets(names, min_mb=0.0):
    """按进程名优化（伴侣/MediaSDK/OBS）。min_mb 用于跳过已经很小的进程。"""
    wanted = {n.lower() for n in names}
    rows = []
    for pid, name in list_processes():
        if name.lower() not in wanted:
            continue
        ws = proc_ws_bytes(pid)
        if min_mb and ws and ws < min_mb * 1024 * 1024:
            continue
        ok, before, after = trim_pid(pid)
        rows.append((pid, name, ok, before, after))
    return rows


def trim_all(min_mb=200.0):
    """优化全部用户进程（跳过系统进程与小于 min_mb 的进程）。"""
    me = os.getpid()
    rows = []
    for pid, name in list_processes():
        if pid <= 4 or pid == me or name.lower() in TRIM_SKIP:
            continue
        ws = proc_ws_bytes(pid)
        if not ws or ws < min_mb * 1024 * 1024:
            continue
        ok, before, after = trim_pid(pid)
        rows.append((pid, name, ok, before, after))
    return rows


def summarize_trim(rows):
    freed = sum(max(0, b - a) for _, _, ok, b, a in rows if ok)
    touched = sum(1 for _, _, ok, _, _ in rows if ok)
    return touched, len(rows), freed / 1024.0 / 1024.0


def memopt_once(scope="mate", min_mb=200.0, quiet=False):
    if scope == "all":
        rows = trim_all(min_mb)
        label = "全部进程（>%d MB）" % int(min_mb)
    else:
        rows = trim_targets(MATE_PROC_NAMES)
        label = "直播伴侣相关进程"
    touched, seen, freed = summarize_trim(rows)
    msg = "内存优化：%s → 命中 %d 个进程、成功 %d 个、释放约 %.1f MB" % (label, seen, touched, freed)
    if not quiet:
        print(log(msg))
        for pid, name, ok, before, after in rows:
            print("    %-28s pid=%-6d %s %6.1f → %6.1f MB" %
                  (name, pid, "OK " if ok else "skip", before / 1048576.0, after / 1048576.0))
    return touched, seen, freed


class MemOptRunner(object):
    """等价 Timer.periodic：按间隔反复 trim，可随时停。"""

    def __init__(self, interval=60, scopes=("mate",), min_mb=200.0, on_tick=None):
        import threading
        self.interval = max(1, min(3600, int(interval)))
        self.scopes = tuple(scopes) if isinstance(scopes, (list, tuple)) else (scopes,)
        self.min_mb = min_mb
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        while not self._stop.is_set():
            try:
                for sc in self.scopes:
                    touched, seen, freed = memopt_once(sc, self.min_mb, quiet=True)
                    if self.on_tick:
                        self.on_tick(sc, touched, seen, freed)
            except Exception:
                log("内存优化出错：\n" + traceback.format_exc())
            if self._stop.wait(self.interval):
                break

    def start(self):
        import threading
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memopt", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=3)
        self._thread = None


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("%s v%s — 直播伴侣 OBS 模式解锁器" % (APP_NAME, APP_VER))
    root.geometry("980x760")
    root.minsize(880, 660)

    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill="x")
    ttk.Label(top, text="伴侣目录").pack(side="left")
    dir_var = tk.StringVar()
    ent = ttk.Entry(top, textvariable=dir_var)
    ent.pack(side="left", fill="x", expand=True, padx=6)

    def browse():
        p = filedialog.askdirectory(title="选择直播伴侣安装目录（含 resources 的那一层）")
        if p:
            dir_var.set(p)
            refresh()

    ttk.Button(top, text="浏览", command=browse, width=8).pack(side="left")

    body = ttk.Frame(root, padding=(10, 0))
    body.pack(fill="both", expand=True)

    cols = ("id", "state", "desc")
    tree = ttk.Treeview(body, columns=cols, show="headings", height=12)
    for c, w, t in (("id", 60, "规则"), ("state", 96, "状态"), ("desc", 700, "作用")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w")
    tree.pack(fill="both", expand=True)

    out = tk.Text(root, height=9, wrap="none")
    out.pack(fill="both", expand=False, padx=10, pady=(6, 0))

    bar = ttk.Frame(root, padding=(10, 8))
    bar.pack(fill="x")

    status_var = tk.StringVar(value="就绪")

    def append(msg):
        out.insert("end", msg + "\n")
        out.see("end")

    # ---- 内存优化面板 ----
    import threading

    mf = ttk.LabelFrame(root, text="内存优化 —— 优化直播伴侣内存 / 优化全部进程内存", padding=(8, 6))
    mf.pack(fill="x", padx=10, pady=(0, 4))

    mc = load_config()
    v_mate = tk.BooleanVar(value=bool(mc.get("memopt_mate", True)))
    v_all = tk.BooleanVar(value=bool(mc.get("memopt_all", False)))
    v_int = tk.StringVar(value=str(mc.get("memopt_interval", 60)))
    v_min = tk.StringVar(value=str(mc.get("memopt_min_mb", 200)))
    mem_state = tk.StringVar(value="未运行")
    runner_box = {"r": None}

    row1 = ttk.Frame(mf)
    row1.pack(fill="x")
    ttk.Checkbutton(row1, text="伴侣进程（直播伴侣.exe / MediaSDK_Server）", variable=v_mate).pack(side="left")
    ttk.Checkbutton(row1, text="全部进程", variable=v_all).pack(side="left", padx=(12, 0))
    ttk.Label(row1, text="间隔(秒)").pack(side="left", padx=(16, 2))
    ttk.Spinbox(row1, from_=1, to=3600, width=6, textvariable=v_int).pack(side="left")
    ttk.Label(row1, text="全部进程阈值(MB)").pack(side="left", padx=(12, 2))
    ttk.Spinbox(row1, from_=0, to=8192, width=7, textvariable=v_min).pack(side="left")

    row2 = ttk.Frame(mf)
    row2.pack(fill="x", pady=(6, 0))
    mem_btn = ttk.Button(row2, text="开始定时优化", width=14)
    mem_btn.pack(side="left")

    def _scopes():
        sc = [s for s, v in (("mate", v_mate.get()), ("all", v_all.get())) if v]
        return sc or ["mate"]

    def _min_mb():
        try:
            return float(v_min.get())
        except ValueError:
            return 200.0

    def _save_mem_cfg():
        c = load_config()
        c.update(memopt_mate=bool(v_mate.get()), memopt_all=bool(v_all.get()),
                 memopt_interval=v_int.get(), memopt_min_mb=v_min.get())
        save_config(c)

    def memopt_now():
        scopes = _scopes()
        mem_state.set("优化中…")
        append("内存优化：开始（%s）" % " + ".join("全部进程" if s == "all" else "伴侣进程" for s in scopes))

        def work():
            out = []
            for sc in scopes:
                t, seen, freed = memopt_once(sc, _min_mb(), quiet=True)
                out.append("%s 命中%d/成功%d 释放%.1fMB" %
                           ("全部" if sc == "all" else "伴侣", seen, t, freed))
                append("  " + out[-1])
            root.after(0, lambda: mem_state.set(" | ".join(out)))

        threading.Thread(target=work, daemon=True).start()

    ttk.Button(row2, text="立即优化一次", width=14, command=memopt_now).pack(side="left", padx=6)

    def memopt_toggle():
        r = runner_box.get("r")
        if r is not None and r.running:
            r.stop()
            runner_box["r"] = None
            mem_btn.config(text="开始定时优化")
            mem_state.set("已停止")
            append("定时内存优化：已停止")
            return
        try:
            interval = int(float(v_int.get() or 60))
        except ValueError:
            interval = 60
        interval = max(1, min(3600, interval))
        v_int.set(str(interval))
        _save_mem_cfg()
        scopes = _scopes()

        def tick(sc, t, sn, f):
            root.after(0, lambda: mem_state.set(
                "%s 每 %ds：%s 释放 %.1f MB" % (time.strftime("%H:%M:%S"), interval,
                                              "全部" if sc == "all" else "伴侣", f)))

        runner_box["r"] = MemOptRunner(interval, scopes, _min_mb(), on_tick=tick)
        runner_box["r"].start()
        mem_btn.config(text="停止定时优化")
        mem_state.set("已启动，每 %d 秒一次" % interval)
        append("定时内存优化：已启动（%s，每 %d 秒）" %
               (" + ".join("全部进程" if s == "all" else "伴侣进程" for s in scopes), interval))

    mem_btn.config(command=memopt_toggle)
    ttk.Label(row2, textvariable=mem_state, foreground="#0a6").pack(side="left", padx=10)

    def on_close():
        r = runner_box.get("r")
        if r is not None:
            r.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def current():
        d, ac = pick_target(dir_var.get().strip() or None)
        if not ac:
            messagebox.showerror(APP_NAME, "没找到 app.content。请点『浏览』选到\nC:\\Program Files (x86)\\webcast_mate 这一层。")
            return None, None, None
        dir_var.set(d)
        cfg = load_config()
        cfg["mate_dir"] = d
        save_config(cfg)
        return d, ac, ver_from_content(ac)

    def refresh():
        d, ac, ver = current()
        if not ac:
            return
        rows = scan(ac)
        tree.delete(*tree.get_children())
        for r in rows:
            if r["pending"] == 0 and r["applied"]:
                st = "已生效 %d" % r["applied"]
            elif r["pending"]:
                st = "待打 %d" % r["pending"]
            else:
                st = "不适用"
            tree.insert("", "end", values=(r["rule"]["id"], st, r["rule"]["desc"]))
        pend, appl = summarize(rows)
        status_var.set("%s ｜ 待打 %d ｜ 已生效 %d" % (ver, pend, appl))
        append(log("检测：%s ｜ 待打 %d ｜ 已生效 %d" % (ver, pend, appl)))
        if pend == 0 and appl:
            append("→ OBS 模式已开启，重启伴侣即可在左上角看到『obs直播』。")
        elif pend:
            append("→ 还有 %d 处未打，点『一键开启』。" % pend)
        else:
            append("→ 规则与这个版本不匹配（可能伴侣已升级），需要更新规则表。")

    def do_apply():
        d, ac, ver = current()
        if not ac:
            return
        if needs_admin(ac) and not is_admin():
            if messagebox.askyesno(APP_NAME, "打补丁要写 Program Files，需要管理员权限。\n现在请求 UAC 提权并重开本工具？"):
                append(log("请求 UAC …"))
                if not elevate([__file__, "--apply"]):
                    messagebox.showwarning(APP_NAME, "提权被取消。")
            return
        if not messagebox.askyesno(APP_NAME, "将修改 %s 下的 JS 并备份。\n伴侣会被先关闭。继续？" % ver):
            return
        status_var.set("正在打补丁 …")
        root.update_idletasks()
        files, hits, msgs = apply_patch(ac, ver, keep_running=False)
        for m in msgs:
            append(m)
        append(log("完成：%d 文件 / %d 处" % (files, hits)))
        refresh()

    def do_restore():
        d, ac, ver = current()
        if not ac:
            return
        if needs_admin(ac) and not is_admin():
            if messagebox.askyesno(APP_NAME, "还原要写 Program Files，需要管理员权限。\n现在请求 UAC 提权？"):
                if not elevate([__file__, "--restore"]):
                    messagebox.showwarning(APP_NAME, "提权被取消。")
            return
        if not messagebox.askyesno(APP_NAME, "从备份把 %s 的原始 JS 还原回去？" % ver):
            return
        n, msg = restore(ac, ver)
        append(log(msg))
        refresh()

    def do_launch_mate():
        d, ac, ver = current()
        if not ac:
            return
        exe = mate_exe(d)
        if not exe:
            messagebox.showerror(APP_NAME, "目录里没找到 直播伴侣.exe")
            return
        append(log("启动 %s" % exe))
        shell_runas(exe)

    def do_launch_obs():
        exe = find_obs()
        if not exe:
            p = filedialog.askopenfilename(title="选择 obs64.exe", filetypes=[("OBS", "obs64.exe"), ("exe", "*.exe")])
            if not p:
                return
            cfg = load_config()
            cfg["obs_exe"] = p
            save_config(cfg)
            exe = p
        append(log("启动 %s" % exe))
        shell_runas(exe)

    ttk.Button(bar, text="检测", command=refresh, width=10).pack(side="left")
    ttk.Button(bar, text="一键开启 OBS 模式", command=do_apply, width=20).pack(side="left", padx=6)
    ttk.Button(bar, text="还原", command=do_restore, width=8).pack(side="left")
    ttk.Button(bar, text="启动伴侣", command=do_launch_mate, width=10).pack(side="left", padx=6)
    ttk.Button(bar, text="启动 OBS", command=do_launch_obs, width=10).pack(side="left")
    ttk.Button(bar, text="打开备份", command=lambda: os.startfile(BACKUP_ROOT) if os.path.isdir(BACKUP_ROOT) else None,
               width=10).pack(side="left", padx=6)
    ttk.Label(bar, textvariable=status_var).pack(side="right")

    append("%s v%s｜%s" % (APP_NAME, APP_VER, "管理员" if is_admin() else "普通权限（写盘前会请求 UAC）"))
    append("日志：%s" % LOG_PATH)
    root.after(120, refresh)
    root.mainloop()
    return 0


def main():
    argv = sys.argv[1:]
    try:
        if not argv or "--gui" in argv:
            return gui()
        return cli(argv)
    except Exception:
        log("崩溃：\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
