# MateOBS — 直播伴侣「obs直播」模式解锁 + 内存优化

一个单文件小工具，给 **直播伴侣 13.3.5** 补上它自己藏起来的 **OBS 第三方推流模式**，顺带把「优化内存」也做进去。纯 Python 标准库，无第三方依赖，GUI + 命令行两用。

```
python mateobs.py                 # 打开图形界面
```

> 目标版本：直播伴侣 13.3.5.564315535（Windows）。其它版本可能规则失配，工具会提示“待打 N 处 / 已生效 M 处”。

---

## 它做什么

直播伴侣本身内置了 `obs直播` 模式（本地起 nginx，给你 `rtmp://127.0.0.1:1935/rtmp` + 推流码，由 OBS 往这儿推），但这个入口对普通账号是隐藏的，而且即使进去了，建房参数还会被服务端拦。MateOBS 做两件事：

1. **让入口出现**：改掉模式下拉的三个校验分支 + 开播前的权限弹窗；
2. **让建房通过**：把建房参数里的推流类型固定成普通房间，绕开服务端对第三方推流的限制。

改的是伴侣安装目录下 `resources\app\app.content` 里的 JS：**按字节精确替换**，不解析语法、不重新编码。写盘前自动备份到 `backups\<版本>\<相对路径>.orig`，`--restore` 逐字节写回。

## 规则表（v1.3，7 条）

| # | 文件 | 原串 → 新串 | 作用 |
|---|---|---|---|
| R03 | `8083.*.js` | `hasObsStream:!1` → `!0` | 不再要求“已检测到推流”才让开播 |
| R05 | `15099.*.js` | `t=await this.confirmNewPermission();` → `t={code:nc.hn.Success};` | 跳过开播权限弹窗 |
| R06 | `55508`/`93341`/`source-float` | `0!==X\|\|!t)throw` → `0===1)throw` | 状态码校验分支永假（不再报“网络异常”） |
| R07 | 同上 | `;if(1!==X)throw` → `;if(0===1)throw` | `pc_obs_status!==1` 分支永假（不再报“权限校验失败”） |
| R08 | 同上 | `const{pc_obs_status:X}=t;` → `={pc_obs_status:1};` | 接口没 data 也不炸 |
| R09 | `55508.*.js` | `!de\|\|APP_ID===NEW_HUOSHAN` → `!1` | 模式下拉里无条件显示「obs直播」 |
| **R10** | `15099.*.js` | `push_stream_type:"obs"===this.mode?2:1` → `push_stream_type:1` | 建房按普通房间上报，避免「没有直播权限」 |

编号不复用（R01/R02/R04 是早期版本用过的，已删除）。

## 用法

```
python mateobs.py                    # GUI：检测 / 一键开启 / 还原 / 启动伴侣 / 启动 OBS / 内存优化
python mateobs.py --status           # 只读：待打多少处、已生效多少处
python mateobs.py --apply            # 打补丁（会先结束伴侣进程）
python mateobs.py --repair           # 先还原再打（幂等，推荐）
python mateobs.py --restore          # 撤销，回到原版
python mateobs.py --kill             # 只结束伴侣进程
python mateobs.py --memopt mate      # 优化伴侣进程内存
python mateobs.py --memopt all --min-mb 200
python mateobs.py --memopt-watch mate --interval 60
```

`--mate-dir` 可以给安装根目录（`C:\Program Files (x86)\webcast_mate`）也可以给版本子目录，不给就自动探测（读 `launcher_config.json` 或扫版本号最大的目录）。

**权限**：工具会先往 `app.content` 写一个探针文件，写不进去才弹 UAC 自提权；补丁本身需要对 `Program Files` 有写权限。内存优化要降伴侣的占用时，工具也必须以管理员运行（伴侣本身是管理员进程）。

## 内存优化

等价于“把进程的工作集还给系统”：

| 环节 | 实现 |
|---|---|
| 枚举 | `CreateToolhelp32Snapshot` + `Process32FirstW/NextW` |
| 读数 | `K32GetProcessMemoryInfo` |
| 释放 | `EmptyWorkingSet` + `SetProcessWorkingSetSize(h,-1,-1)` |
| 伴侣目标 | `直播伴侣.exe`、`MediaSDK_Server.exe`、`直播伴侣 Launcher.exe`、`obs64.exe` |
| 全部进程 | 跳过系统关键进程与本进程，只处理工作集 > 阈值（默认 200 MB）的 |
| 定时 | 线程 + `Event.wait(interval)`，间隔 1–3600 秒 |

实测：`--memopt all --min-mb 100` 一次命中 11 个进程、合计释放约 5.2 GB；单个 400 MB 级的进程可降到 0–20 MB（工作集被换出，物理内存紧张时会自动换回，属正常行为）。

## 排错

- **弹「没有直播权限」**：看 `%APPDATA%\webcast_mate\biz-logs\*.log` 里 `RESP_ERROR /webcast/room/create/` 的 `status_code`。`20800` = 建房参数被服务端拒，说明 R10 没打上 → 跑 `--repair`。
- **下拉里没有「obs直播」**：R06–R09 没打上 → `--status` 看待打数。
- **切模式后行为不对**（例如常规模式也在等推流）：旧规则残留 → `--repair`。
- **伴侣升级/重装后**：补丁会被覆盖，重新 `--repair` 一次即可；若规则大量失配，说明目标版本变了，需要按新版本重新定位这几处字符串。

## 目录

```
mateobs.py        单文件工具（GUI + CLI）
apply_admin.bat   以管理员运行 mateobs.py（打开 GUI）
repair_admin.bat  以管理员运行 --repair（一键重打）
restore.bat       以管理员运行 --restore（撤销）
启动.bat          同 apply_admin.bat，中文名方便双击
```

备份与日志都写在脚本同级目录（`backups\`、`mateobs.log`、`config.json`），删掉工具目录不影响伴侣已打好的补丁。
