#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH·微信桥接（无hook）

原理：
  - 收：读微信4.x本地SQLite数据库（只读，要求微信处于登录状态）
  - 发：Windows UI自动化+剪贴板，驱动微信窗口打字发送
  - 中间：把消息推给DSH插件（POST /dsh-wechat-bridge/receive），
          再把待发的取回来（GET /dsh-wechat-bridge/outbox）

一般不用手跑——DSH设置页里点「启动桥接」。

手跑：
  python bridge\\wechat_bridge.py --dsh http://127.0.0.1:3080
"""

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import sys
import tempfile
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request

# 有些嵌入式 Python 探测不到可写的临时目录（C 盘 TEMP 被安全软件拦住时会这样），
# 这里兜底指到桥目录下自己的 tmp/，否则 tempfile.gettempdir() 会直接抛 FileNotFoundError。
try:
    tempfile.gettempdir()
except Exception:
    _兜底临时 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tmp')
    try:
        os.makedirs(_兜底临时, exist_ok=True)
    except Exception:
        pass
    tempfile.tempdir = _兜底临时
    os.environ['TEMP'] = _兜底临时
    os.environ['TMP'] = _兜底临时

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "bridge.log")

# 可选的小补丁：同目录有 uia_compat.py 就加载（某些微信小版本需要）
UIA_COMPAT = True


def log(*a):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    print(line, flush=True)
    try:
        # 日志只增不减的话几个月就几十 MB，超过 8MB 就滚一份成 bridge.log.1（只留一代）
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _滚日志()
    except Exception:
        pass


_滚过的到 = 0.0


def _滚日志():
    """超过 8MB 就把 bridge.log 挪成 bridge.log.1（覆盖旧的那一份）。"""
    global _滚过的到
    now = time.time()
    if now - _滚过的到 < 60:      # 一分钟最多查一次，别每条日志都 stat
        return
    _滚过的到 = now
    try:
        if os.path.getsize(LOG_PATH) < 8 * 1024 * 1024:
            return
        if os.path.exists(LOG_PATH + ".1"):
            os.remove(LOG_PATH + ".1")
        os.rename(LOG_PATH, LOG_PATH + ".1")
    except Exception:
        pass


ARGS = None
DSH = ""
TOKEN = ""
ONLY_CHATS = []
SEND_ENABLED = True
SKIP_OLDER = 15.0
POLL_SEC = 2.0
# 群里只有被点名才转（正文里带这些词之一）。私聊不受这个限制。
ALIASES = []
DEFAULT_ALIASES = ""

# 从 DSH 设置页拉的配置（每 30 秒刷一次）
CFG = {
    "群上下文条数": 6,
    "日记": True,
    "日记目录": "",
    "主人": [],
    "屏蔽": [],
    "回复延迟下限秒": 0.0,
    "回复延迟上限秒": 0.0,
}
CFG_TS = 0.0

# 微信里收到的图片 / 表情包落盘到这里，再把路径交给宿主去看图
INBOUND_DIR = os.path.join(
    os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
    "wechat-inbound",
)
VISION_CACHE = os.path.join(os.path.dirname(INBOUND_DIR), "wechat-vision-cache.json")
# 宿主版本 ≥1.2.0 时它自己会看图；老宿主就由桥接先看好再发文字
HOST_VERSION = (1, 1, 0)


def _活跃群列表():
    """直接从磁盘配置读「活跃群」—— 宿主重启期间也不至于丢。

    宿主 /state 现在也带这个字段了，但读盘更稳：配置一改，
    下一个 30 秒周期就生效，不依赖宿主是否已经 reload。
    """
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    path = os.path.join(home, "wechat-bridge-config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x).strip() for x in (data.get("活跃群") or []) if str(x).strip()]
    except Exception:
        return []


def refresh_config():
    """问宿主要一次配置。设置页改完不用重启桥接。"""
    global CFG, CFG_TS
    r = http_json("/dsh-wechat-bridge/state")
    c = (r or {}).get("配置") if isinstance(r, dict) else None
    if not isinstance(c, dict):
        return False
    try:
        CFG = {
            "群上下文条数": max(0, min(50, int(c.get("群上下文条数") or 0))),
            "日记": bool(c.get("日记", True)),
            "日记目录": str(c.get("日记目录") or "").strip(),
            "主人": [str(x).strip() for x in (c.get("主人") or []) if str(x).strip()],
            "屏蔽": [str(x).strip() for x in (c.get("屏蔽") or []) if str(x).strip()],
            "群回复间隔秒": max(0.0, float(c.get("群回复间隔秒") or 0)),
            "群每小时上限": max(0.0, float(c.get("群每小时上限") or 0)),
            "回复延迟下限秒": max(0.0, float(c.get("回复延迟下限秒") or 0)),
            "回复延迟上限秒": max(0.0, float(c.get("回复延迟上限秒") or 0)),
        }
        CFG["活跃群"] = _活跃群列表()
        CFG_TS = time.time()
        return True
    except Exception as e:
        log("解析配置失败：", repr(e))
        return False


def parse_args():
    global DSH, TOKEN, ONLY_CHATS, SEND_ENABLED, SKIP_OLDER, POLL_SEC, ALIASES

    p = argparse.ArgumentParser(description="DSH WeChat bridge")
    p.add_argument("--dsh", default=os.environ.get("DSH_ORIGIN", "http://127.0.0.1:3080"))
    p.add_argument("--token", default=os.environ.get("DSH_BRIDGE_TOKEN", ""))
    p.add_argument("--only", default="", help="只转这些聊天，逗号分隔（留空 = 全部）")
    p.add_argument("--aliases", default=DEFAULT_ALIASES, help="群里叫这些名字才算在跟我说话")
    p.add_argument("--no-send", action="store_true", help="只收不发")
    p.add_argument("--skip-older", type=float, default=15.0)
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--test-image", default="", help="只看一张本地图片然后退出（调视图用）")
    a = p.parse_args()

    DSH = str(a.dsh).rstrip("/")
    TOKEN = str(a.token or "")
    ONLY_CHATS = [x.strip() for x in str(a.only or "").split(",") if x.strip()]
    ALIASES = [x.strip() for x in str(a.aliases or "").split(",") if x.strip()]
    SEND_ENABLED = not a.no_send
    SKIP_OLDER = float(a.skip_older or 15.0)
    POLL_SEC = float(a.poll or 2.0)
    return a


# @ 某人时微信会把机器 id 塞进正文，看着像乱码 —— 洗掉
_MENTION_ID = re.compile(r"wxid_[0-9a-zA-Z_-]+[:：]?")
_AT_TAG = re.compile(r"@[^\s@]{1,24}\s?")


def clean_text(text):
    t = _MENTION_ID.sub("", text or "")
    t = _AT_TAG.sub("", t)
    return " ".join(t.split()).strip()


# ---------- HTTP ----------

def http_json(path, payload=None, method=None):
    url = DSH + path
    data = None
    headers = {"content-type": "application/json"}
    if TOKEN:
        headers["x-bridge-token"] = TOKEN
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "说": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "说": str(e)}


def main():
    global HOST_VERSION, ARGS
    ARGS = parse_args()
    log("DSH 地址：", DSH, "｜只收不发：", not SEND_ENABLED)

    log("import wechatauto …（第一次会慢）")
    try:
        from wechatauto import WeChatDB, WeChatGUI
        from wechatauto.db import Listener

        # 拟人节流：默认 natural 档是「120 秒最多 6 次写」，撞上就冷却 60 秒左右。
        # 我这边一次回复要发好几段，很容易撞线 —— 撞线时消息发不出去，
        # 上层会以为失败再重试，重试又撞线，就出现了「卡住」「重复」。
        # 调到 fast 档（120 秒 20 次），保留节奏，但不会把正常聊天卡死。
        try:
            from wechatauto import rhythm as _rhythm

            _rhythm.set_profile('fast')
            _rhythm.configure(burst=30)   # 120 秒内 30 次，别再把正常回复卡住
            _p = _rhythm.profile()
            log("发送节流档位：", _p.name, "（%d 秒内 %d 次）" % (int(_p.window), _p.burst))
        except Exception as _e:
            log("节流档位调整失败（不影响用）：", repr(_e))
    except Exception as e:
        log("✗ import wechatauto 失败：", repr(e))
        log("  依赖没装好 —— 自己装好 wechatauto-replica 和 numpy 再启动")
        return 2

    if UIA_COMPAT:
        try:
            import uia_compat  # noqa: F401

            log("uia_compat.apply() =", uia_compat.apply())
        except ImportError:
            pass
        except Exception as e:
            log("⚠ uia_compat 出错（继续跑）：", repr(e))

    log("连接微信数据库（只读）…")
    try:
        db = WeChatDB()
    except Exception as e:
        log("✗ 连不上微信数据库：", repr(e))
        log("  检查：微信登录了吗？微信 → 设置 → 文件管理 → 默认保存位置")
        return 2
    me = getattr(db, "wxid", "")
    log("当前账号 wxid：", me)
    try:
        info = db.get_self_info() or {}
        log("昵称：", info.get("nick_name") or info.get("remark") or "?")
    except Exception:
        pass

    name_cache = {}

    def display_name(username):
        if username in name_cache:
            return name_cache[username]
        name = ""
        try:
            if str(username).endswith("@chatroom"):
                name = db.group_id_to_name(username) or ""
            else:
                name = db.get_nickname(username) or ""
        except Exception:
            name = ""
        if name:
            name_cache[username] = name
        return name

    # ---------- 群回复频率 ----------

    chat_hits = {}      # 群名 -> [时间戳]

    def 频率允许(chat):
        gap = CFG.get("群回复间隔秒") or 0
        per_hour = CFG.get("群每小时上限") or 0
        if not gap and not per_hour:
            return True
        now = time.time()
        hits = [t for t in chat_hits.get(chat, []) if now - t < 3600]
        if gap and hits and now - hits[-1] < gap:
            log("群频率：%s 刚回过，%.0f 秒内不再接" % (chat, gap))
            return False
        if per_hour and len(hits) >= per_hour:
            log("群频率：%s 这一小时已经接了 %d 次，够了" % (chat, len(hits)))
            return False
        hits.append(now)
        chat_hits[chat] = hits
        return True

    # ---------- 屏蔽 / 主人 / 日记 / 群上下文 ----------

    def is_blocked(chat, nickname, cid=""):
        # 屏蔽名单三种写法都认：显示名（群名/昵称）、群里的 wxid、会话 id。
        # 只认显示名的话，一改备注就漏，所以底层那个码永远多认一份。
        for b in CFG["屏蔽"]:
            if b and (b == chat or b == nickname or (cid and b == cid)):
                return True
        return False

    def is_master(sender_id, nickname):
        # 认人看微信里显示的名字（昵称/备注），wxid 也认一份当补充 —— 规矩就这样。
        for m in CFG["主人"]:
            if m and (m == sender_id or (nickname and m == nickname)):
                return True
        return False

    def diary_path(chat):
        base = CFG["日记目录"] or os.path.join(os.path.expanduser("~"), ".dsh", "wechat-diary")
        day = time.strftime("%Y-%m-%d")
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", chat).strip("_")[:60] or "群"
        return os.path.join(base, day, safe + ".md")

    def diary_append(chat, ts, who, content):
        p = diary_path(chat)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            new = not os.path.exists(p)
            with open(p, "a", encoding="utf-8") as f:
                if new:
                    f.write("# %s · %s\n\n" % (chat, time.strftime("%Y-%m-%d")))
                t = time.strftime("%H:%M", time.localtime(ts or time.time()))
                f.write("- %s %s：%s\n" % (t, who or "某人", content))
        except Exception as e:
            log("写日记失败：", repr(e))

    def group_context(username, chat, limit):
        """那群最近在聊什么 —— 拼成一段给模型看。"""
        if limit <= 0:
            return ""
        try:
            rows = db.get_messages(username, limit=limit + 2)
        except Exception as e:
            log("取群上下文失败：", repr(e))
            return ""
        if not rows:
            return ""
        try:
            rows = sorted(rows, key=lambda m: float(m.get("create_time") or 0))
        except Exception:
            pass
        rows = rows[:-1][-limit:]      # 最后一条就是当前这句，去掉
        lines = []
        for m in rows:
            if (m.get("type") or "") != "文本":
                continue
            txt = clean_text(m.get("content") or "")
            if not txt or txt.startswith("<"):
                continue
            ts = float(m.get("create_time") or 0)
            t = time.strftime("%H:%M", time.localtime(ts)) if ts else "??:??"
            sid = m.get("sender_username") or ""
            lines.append("[%s] %s：%s" % (t, display_name(sid) or "某人", txt))
        if not lines:
            return ""
        return "（「%s」刚才在聊：\n%s\n）\n" % (chat, "\n".join(lines))

    # ---------- 表情包：正文里的 [表情: 描述] 变成真图片 ----------

    # 和前端配图器（dsh-meme client.js 的 matchDesc）同一套规则，不然同一条回复
    # 在 Web 里配到的图，和微信里发出去的会是两张。
    _RE_MEME = re

    def _fold_caption(s):
        s = str(s or "").strip()
        s = _RE_MEME.sub(r'[\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u00ab\u00bb]', '"', s)
        return _RE_MEME.sub(r"\s+", " ", s)

    def _loose_caption(s):
        return _RE_MEME.sub(r'["\'\s]', "", _fold_caption(s))

    # 字符类里别再写 "\\[\\]" 这种：上一版（2026-11-13）就是 ] 提前闭合、整条正则报废，
    # 一个词都切不出来，find_meme 的模糊匹配全程失效 —— 于是 [表情: 得意地叉腰，等着被夸]
    # 被当成普通文字原样发给了主人（11-14 早上发现）。
    _TOKEN_SPLIT = _RE_MEME.compile(r"[\s,.!?;:，。！？；：、\"“”'‘’()（）\[\]【】《》…·～~\-]+")

    def _split_desc_tokens(s):
        parts = _TOKEN_SPLIT.split(_fold_caption(s))
        toks = [_RE_MEME.sub(r'["\']', "", p) for p in parts]
        toks = [t for t in toks if len(t) >= 2]
        toks.sort(key=len, reverse=True)
        return toks

    MEME = {"captions": {}, "keywords": {}, "rows": []}

    def load_memes():
        import glob
        import sqlite3

        dbs = []
        try:
            base = os.path.join(os.path.expanduser("~"), ".dsh", "profiles")
            dbs += glob.glob(os.path.join(base, "*", "node_modules", "dsh-meme", "memes", "*", "index.db"))
        except Exception:
            pass
        try:
            # 订阅图库和「我的收藏」不在插件包里，在 ~/.dsh/meme-packs/<图库>/index.db。
            # 不扫这段，播报的「表情库 N 张」就只有插件自带的那 24 张。
            packs = os.path.join(os.path.expanduser("~"), ".dsh", "meme-packs")
            dbs += glob.glob(os.path.join(packs, "*", "index.db"))
            dbs += glob.glob(os.path.join(packs, "*", "*", "index.db"))
            dbs = list(dict.fromkeys(dbs))
        except Exception:
            pass
        def 加键(表, key, path):
            raw = str(key or "").strip()
            if not raw:
                return
            for form in {raw, _fold_caption(raw), _loose_caption(raw)}:
                if form and form not in 表:
                    表[form] = path

        for dbp in dbs:
            pack = os.path.dirname(dbp)
            try:
                con = sqlite3.connect(dbp)
                for path, cap, kw, fn in con.execute("select path, caption, keywords, file_name from memes"):
                    absp = os.path.join(pack, str(path).replace("/", os.sep))
                    if not os.path.exists(absp):
                        continue
                    cap = str(cap or "").strip()
                    kw = str(kw or "")
                    fn = str(fn or "")
                    加键(MEME["captions"], cap, absp)
                    加键(MEME["captions"], fn, absp)
                    加键(MEME["captions"], cap[:80], absp)
                    加键(MEME["captions"], cap[:100], absp)
                    加键(MEME["keywords"], kw, absp)
                    for w in kw.split():
                        加键(MEME["keywords"], w, absp)
                    hay = []
                    parts = []
                    for s in (cap, kw, fn):
                        f = _fold_caption(s)
                        if not f:
                            continue
                        l = _loose_caption(s)
                        hay.append(f)
                        if l and l != f:
                            hay.append(l)
                        parts.append(s)
                    MEME["rows"].append({"hay": hay, "tokens": _split_desc_tokens(" ".join(parts)), "path": absp})
                con.close()
            except Exception as e:
                log("读表情库失败：", repr(e))
        if MEME["rows"]:
            log("表情库：%d 张（%s）" % (len(MEME["rows"]), "、".join(os.path.basename(os.path.dirname(d)) for d in dbs)))

    # 描述 → 图 的最后一道兜底用的词表。只放库里真有的情绪词（344 张里出现过的），
    # 编出来的词（比如「得意地叉腰」）分词再好也可能一个都命中不了。
    _情绪词 = [
        "开心", "高兴", "好耶", "大笑", "得意", "撒娇", "卖萌", "可爱", "比耶", "喜欢", "心动",
        "害羞", "脸红", "委屈", "难过", "哭", "生气", "愤怒", "哼", "嫌弃", "嘲讽", "不屑",
        "无语", "无奈", "疑惑", "懵", "问号", "震惊", "惊讶", "崩溃", "累", "困", "睡觉", "晚安",
        "摸鱼", "躺平", "上班", "加班", "干饭", "吃饭", "饿", "休息", "期待", "拒绝", "对不起", "打招呼",
    ]

    def find_meme(desc):
        """和前端 matchDesc 同一套优先级：caption 精确 > keyword 精确 > 分词最长命中。"""
        raw = str(desc or "").strip()
        if not raw:
            return ""
        forms = [raw, _fold_caption(raw), _loose_caption(raw)]
        for 表 in (MEME["captions"], MEME["keywords"]):
            for form in forms:
                hit = 表.get(form)
                if hit:
                    return hit
        needles = []
        for t in _split_desc_tokens(raw):
            l = _loose_caption(t)
            if l:
                needles.append(l)
        best, best_len, best_cnt = "", 0, 0
        for row in MEME["rows"]:
            longest = 0
            count = 0
            for needle in needles:
                length = len(needle) if any(needle in h for h in row["hay"]) else 0
                for tok in row["tokens"]:
                    t = _loose_caption(tok)
                    if t and (needle in t or t in needle):
                        length = max(length, min(len(needle), len(t)))
                if length:
                    longest = max(longest, length)
                    count += 1
            if longest > best_len or (longest == best_len and count > best_cnt):
                best, best_len, best_cnt = row["path"], longest, count
        if best:
            return best
        # 兜底：描述对不上任何一条（或成分词了）时，按里面的情绪词随机挑一张。
        # 宁可发一张沾边的图，也不能把 "[表情: …]" 原样当文字发出去。
        for 词 in _情绪词:
            if 词 in raw:
                候选 = [r["path"] for r in MEME["rows"] if any(词 in h for h in r["hay"])]
                if 候选:
                    return random.choice(候选)
        return ""

    # ---------- 发：从 DSH 取队列，用 UIA 发出去 ----------

    gui_box = {"gui": None}
    send_lock = threading.Lock()
    # 刚发出去的消息：同一个微信号，自己发的都会进库 ——
    # 靠这个列表把「我们发的」跟「主人自己在手机上打的」区分开，不然会自问自答。
    recently_sent = []
    # 上一次往外发的时间。文件传输助手发完立刻会被读回来，几秒内一律不看（防回环的兜底）
    STATE = {"last_send": 0.0}

    def 静音窗口操作(gui):
        """把 gui 上「会主动把微信拉到前台」的方法换成空操作。

        wechatauto 发送前习惯先 ensure_visible()（最小化遮挡窗口 + 置顶 +
        ShowWindow(SW_RESTORE)），这是窗口在后台发送时自己冒头的最大来源。
        UIA 写控件不需要窗口在前台，所以这里直接让它「当作已经可见」。
        图片发送走的是另一条显式前台路径（_ensure_wechat_ready(前台=True)），
        不受这里影响。
        """
        try:
            if getattr(gui, "_quiet_gui", False):
                return
            gui.ensure_visible = lambda *a, **k: True
            gui.bring_to_front = lambda *a, **k: True
            gui._minimize_blockers = lambda *a, **k: 0
            gui._quiet_gui = True
        except Exception as e:
            log("静音窗口操作失败（不影响发送）：", repr(e))

    def get_gui():
        if gui_box["gui"] is None:
            gui_box["gui"] = WeChatGUI()
            log("WeChatGUI 已就绪")
        return gui_box["gui"]

    def _foreground_wechat():
        """把微信主窗口拉到前面。只在发送失败重试时用 —— 平时不抢你的前台。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 9)          # SW_RESTORE
                win32gui.SetForegroundWindow(found[0])    # 拉到前面
                time.sleep(0.5)
                return True
        except Exception as e:
            log("把微信窗口拉到前面失败：", repr(e))
        return False

    def _minimize_wechat():
        """发完把微信收起来 —— 不然它被拉到前台，主人正在干别的就被打断了。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 6)          # SW_MINIMIZE
                return True
        except Exception as e:
            log("最小化微信失败：", repr(e))
        return False

    def _强设前台(hwnd):
        """把窗口真正顶到前台。

        单纯的 SetForegroundWindow 会被 Windows 的前台锁定拒绝（调用进程不是
        当前前台进程时）。先用 AttachThreadInput 把自己的输入队列接到当前前台
        窗口上，这个限制就绕过去了。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = user32.GetWindowThreadProcessId(hwnd, None)
            连上 = False
            # AttachThreadInput 会一直等到目标线程处理消息为止：微信 UI 线程忙的时候
            # （正在跑 OCR / 切会话）能卡住几分钟不返回，2026-11-16 22:21 那次键盘通道
            # 就是卡在这里。所以先探一下两个线程谁不应答，不应答就不去连。
            def _在响应(h):
                try:
                    return bool(user32.SendMessageTimeoutW(
                        h, 0x0000, 0, 0, 0x0002, 400))   # WM_NULL，SMTO_ABORTIFHUNG
                except Exception:
                    return True

            if tid_fg and tid_me and tid_fg != tid_me and _在响应(hwnd) and _在响应(fg):
                连上 = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                if 连上:
                    user32.AttachThreadInput(tid_me, tid_fg, False)
            # 以前这里直接 return True：切不过去也当成功，后面的粘贴就落到别的窗口里，
            # 微信窗口还停在旧画面上不动（2026-11-14 的「假卡死」）。现在按实际结果返回。
            return _前台是(hwnd)
        except Exception as e:
            log("强设前台失败：", repr(e))
            return False

    def _前台是(hwnd):
        try:
            import ctypes
            return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    def _敲一下Alt():
        """按一下 Alt 松开：系统把这当成真人操作，随后 SetForegroundWindow 才放行。"""
        try:
            import ctypes
            u32 = ctypes.windll.user32
            VK_MENU = 0x12
            u32.keybd_event(VK_MENU, 0, 0, 0)
            u32.keybd_event(VK_MENU, 0, 2, 0)
            return True
        except Exception:
            return False

    def _确保前台(hwnd, 超时=1.2):
        """把微信真的顶到前台并确认。

        做不到就返回 False —— 调用方必须放弃粘贴。绝不能“以为切过去了”
        就按 Ctrl+V，否则图会贴到当前前台窗口（比如 DSH 的输入框）里。
        """
        if not hwnd:
            return False
        if _前台是(hwnd):
            return True
        t0 = time.time()
        while time.time() - t0 < 超时:
            _敲一下Alt()
            _强设前台(hwnd)
            for _ in range(6):
                if _前台是(hwnd):
                    return True
                time.sleep(0.05)
            time.sleep(0.1)
        return _前台是(hwnd)

    def _剪贴板快照():
        """记住剪贴板里原来的东西，发完图还原回去（不然主人复制的内容被顶掉）。"""
        快照 = {}
        try:
            import win32clipboard as wc
            import win32con
            wc.OpenClipboard()
            try:
                for fmt, 名 in ((win32con.CF_UNICODETEXT, "text"),
                                (win32con.CF_DIB, "dib")):
                    try:
                        if wc.IsClipboardFormatAvailable(fmt):
                            快照[名] = wc.GetClipboardData(fmt)
                    except Exception:
                        pass
            finally:
                wc.CloseClipboard()
        except Exception:
            pass
        return 快照

    def _清空剪贴板():
        """发完把剪贴板清掉：不然主人按 Ctrl+V 粘出来的还是刚发出去的那句话 / 那张图。"""
        try:
            import win32clipboard as wc
            wc.OpenClipboard()
            try:
                wc.EmptyClipboard()
            finally:
                wc.CloseClipboard()
        except Exception as e:
            log("清剪贴板失败（不影响发送）：", repr(e))

    def _剪贴板还原(快照):
        if not 快照:
            return
        try:
            import win32clipboard as wc
            import win32con
            wc.OpenClipboard()
            try:
                wc.EmptyClipboard()
                if "text" in 快照:
                    wc.SetClipboardData(win32con.CF_UNICODETEXT, 快照["text"])
                if "dib" in 快照:
                    wc.SetClipboardData(win32con.CF_DIB, 快照["dib"])
            finally:
                wc.CloseClipboard()
        except Exception as e:
            log("剪贴板还原失败（不影响发送）：", repr(e))

    def _剪贴板里还是我们的(快照, 候选):
        """剪贴板里现在是不是我们自己刚粘进去的那份？是就该清，不是就别动。

        - 图：发之前快照里没有图、现在有图 → 是我们粘的位图；
        - 字：现在的文字正好等于刚才发出去的某一气泡 → 是我们粘的；
        - 其它（主人新复制的内容、还原回来的旧内容）→ 不动。
        """
        try:
            import win32clipboard as wc
            import win32con
            wc.OpenClipboard()
            try:
                有图 = False
                有字 = ""
                try:
                    有图 = bool(wc.IsClipboardFormatAvailable(win32con.CF_DIB))
                except Exception:
                    pass
                try:
                    if wc.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        有字 = str(wc.GetClipboardData(win32con.CF_UNICODETEXT) or "").strip()
                except Exception:
                    pass
            finally:
                wc.CloseClipboard()
        except Exception:
            return False
        if 有图 and not (快照 or {}).get("dib"):
            # 位图只可能是我们粘的 —— 但如果剪贴板里同时还压着别人的字（主人刚复制的
            # 一段话、另一个程序放进去的文本），就不能拿「有图」当理由清整块剪贴板：
            # 那行字会一起没。只认「没有字」或者「字就是我们这条」。
            if not 有字:
                return True
            for c in (候选 or []):
                c = str(c or "").strip()
                if c and 有字 == c:
                    return True
            return False
        if not 有字:
            return False
        for c in (候选 or []):
            c = str(c or "").strip()
            if c and 有字 == c:
                return True
        return False

    def _标记不进剪贴板历史():
        """把当前这份剪贴板内容标成「别进 Win+V 历史，也别上云」。

        2026-11-14 加：主人按 Win+V 还能看见我发出去的那句话。原因是 Win+V 的
        历史由系统另一个服务（cbdhsvc）单独攒着，EmptyClipboard 只清当前内容，
        清不掉那份历史。Windows 10 1809 起认两个自定义剪贴板格式：
        CanIncludeInClipboardHistory / CanUploadToCloudClipboard，值写 0 就是
        「这份别记、别传」，密码管理器用的就是这套。要在剪贴板已经打开（同一个
        线程）、正文写完以后再打这个标记，跨进程再 OpenClipboard 会打不进去。
        """
        try:
            import ctypes

            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            u32.RegisterClipboardFormatW.restype = ctypes.c_uint
            u32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            u32.SetClipboardData.restype = ctypes.c_void_p
            u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            k32.GlobalAlloc.restype = ctypes.c_void_p
            k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k32.GlobalFree.restype = ctypes.c_void_p
            k32.GlobalFree.argtypes = [ctypes.c_void_p]
            for 名 in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                fmt = u32.RegisterClipboardFormatW(名)
                if not fmt:
                    continue
                h = k32.GlobalAlloc(0x0042, 4)      # GMEM_MOVEABLE | GMEM_ZEROINIT
                if not h:
                    continue
                if not k32.GlobalLock(h):
                    k32.GlobalFree(h)
                    continue
                k32.GlobalUnlock(h)
                if not u32.SetClipboardData(fmt, h):
                    k32.GlobalFree(h)
        except Exception as e:
            log("标记剪贴板不进历史失败（不影响发送）：", repr(e))

    def _剪贴板设字(文本):
        """把字放进剪贴板，并且打上「不进历史」的标记。

        替代 wechatauto 的 `_clip_set`：它用 pyperclip.copy() 只写
        CF_UNICODETEXT，所以我发出去的每一句都会在 Win+V 里留一份。
        写失败就退回上游那套，宁可留历史也不能发不出去。
        """
        try:
            import win32clipboard as wc
            import win32con

            wc.OpenClipboard()
            try:
                wc.EmptyClipboard()
                wc.SetClipboardData(win32con.CF_UNICODETEXT, 文本)
                _标记不进剪贴板历史()
            finally:
                wc.CloseClipboard()
            return
        except Exception as e:
            log("写剪贴板失败（退回 pyperclip）：", repr(e))
        try:
            import pyperclip

            pyperclip.copy(文本)
        except Exception:
            pass

    # 让上游的「粘贴」也走我们这套，否则 Win+V 里会留下我发出去的每一句话
    try:
        from wechatauto.uia_driver import WeChatUIA as _WeChatUIA

        _WeChatUIA._clip_set = staticmethod(_剪贴板设字)
    except Exception as e:
        log("接管 wechatauto 的剪贴板写入失败（Win+V 里还会看到发出去的字）：", repr(e))

    # 上游还有两条绕开 _clip_set 的写入路：像素通道的 set_clipboard()（guia.py:1780
    # 直接 pyperclip.copy）和发文件用的 copy_files_to_clipboard()（guia.py:2218
    # EmptyClipboard + CF_HDROP）。它们不接管，Win+V 里照样会攒下我发的东西。
    try:
        from wechatauto import guia as _guia_mod

        def _设剪贴板(self, 文字):
            _剪贴板设字(文字)
            return True

        _guia_mod.WeChatGUI.set_clipboard = _设剪贴板

        _原始_复制文件 = getattr(_guia_mod, "copy_files_to_clipboard", None)
        if _原始_复制文件 is not None:

            def _复制文件并标记(路径们):
                成 = _原始_复制文件(路径们)
                if 成:
                    try:
                        import win32clipboard

                        win32clipboard.OpenClipboard()
                        try:
                            _标记不进剪贴板历史()
                        finally:
                            win32clipboard.CloseClipboard()
                    except Exception:
                        pass
                return 成

            _guia_mod.copy_files_to_clipboard = _复制文件并标记
    except Exception as e:
        log("接管微信其余的剪贴板写入失败（Win+V 里还会看到发出去的文件）：", repr(e))

    # wechatauto 每调一次 open_chat 都要新建一个 WeChatDB，只为了把昵称翻成搜索词：
    # 一次 3.4 秒（2026-11-15 00:01 profile：extract_master_key 2.6 秒 + 子进程 0.6 秒），
    # 这段时间微信窗口就白白晾在屏幕上 —— 主人看到的「弹出来不动」有一半在等它。
    # 桥接自己早就建好 db 了，直接拿来用，省掉这一步。
    try:
        from wechatauto import uia_driver as _uia_mod

        def _解析搜索词(keyword):
            cands = [keyword]
            try:
                for hit in db.search_contact(keyword):
                    for k in (hit.get("remark"), hit.get("nick_name")):
                        if k and k not in cands:
                            cands.append(k)
            except Exception:
                pass
            return cands

        _uia_mod.WeChatUIA._resolve_search_keyword = staticmethod(_解析搜索词)
    except Exception as e:
        log("接管 wechatauto 的搜索词解析失败（不影响发送）：", repr(e))

    def _取消置顶():
        h = STATE.pop("wx_hwnd", None)
        if not h:
            return
        try:
            import win32con
            import win32gui as wg

            wg.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass

    # ---------- 后台发送：不抢前台 ----------
    # 起因：wechatauto 的 WeChatUIA.ensure_window() 每次发送都会调 _activate() →
    #       把微信恢复并顶到最前面。实测微信最小化/被遮挡时其实照样能发，这步纯属
    #       多余，代价是主人用电脑时会被反复打断。这里的做法是：
    #       文字走后台（UIA 直接写控件，不需要焦点），失败才抢前台重试。

    掉线汇报 = {"到": 0.0, "说过": ""}

    def _报微信掉线(原因):
        """微信退出/窗口没了：只汇报，不自动拉起、不自动登录（主人 2026-11-14 的死规矩）。

        10 分钟内只说一遍，免得每条消息都刷一行。汇报走两路：
        1）bridge.log（本文件）；
        2）POST 给插件的 /dsh-wechat-bridge/notify，让宿主日志也留一条
           —— 设置页一眼能看到「该去扫码了」。
        """
        now = time.time()
        if now - 掉线汇报["到"] < 600 and 掉线汇报["说过"]:
            return
        掉线汇报["到"] = now
        掉线汇报["说过"] = 原因
        log("⚠ 微信不在线（%s）：按主人的规矩不自动拉起、不自动登录，等他来扫码" % 原因)
        try:
            http_json("/dsh-wechat-bridge/notify", {"text": "微信掉线：%s —— 桥接没动它，请主人扫码登录" % 原因})
        except Exception:
            pass

    def _微信窗口():
        """找微信主窗。

        要挑**真窗口**：偶尔会有第二个进程留下一个 158x26、最小化到屏幕外
        （-21333,-21333）的幽灵窗口（2026-11-14 11:15 出现过），拿它去操作
        等于对着空气打字，还会把发送卡到超时。所以按「可见 + 标题 + 尺寸够大」筛，
        都不够大时再退回第一个可见的。
        """
        try:
            import win32gui

            好的 = []
            found = []

            def cb(hwnd, _):
                try:
                    # 最小化/收进托盘的窗口也算「不在」：现在只在前台发送，
                    # 不再替主人把窗口翻出来（2026-11-23 停用托盘调出）
                    if (win32gui.IsWindowVisible(hwnd)
                            and not win32gui.IsIconic(hwnd)
                            and win32gui.GetWindowText(hwnd) == "微信"):
                        found.append(hwnd)
                        l, t, r, b = win32gui.GetWindowRect(hwnd)
                        if (r - l) >= 400 and (b - t) >= 300 and l > -10000:
                            好的.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if 好的:
                return 好的[0]
            return found[0] if found else None
        except Exception:
            return None

    # 2026-11-14 11:59 主人定了死规矩：微信退出 / 窗口没了，**不许自动拉起、不许自动登录**，
    # 只汇报，等他来扫码。
    # 2026-11-23 起彻底停用「从托盘调出」：ShowWindow 那一路会留下半死 UI（能打字、回车不生效），
    # 反复显示/最小化还会把 mmui 控件树弄丢，之后只能退 OCR。现在只在前台发送：
    # 窗口没开、最小化或收进托盘，都直接请主人先打开微信。

    def _等画面画好(h, 超时=3.0):
        """等窗口真的画出内容再往下驱动。

        从托盘叫起来的窗口，有一段时间右侧会话区是**整片白板**：wechatauto 的
        像素探针在这种画面上找不到输入框，就一轮轮重试，把窗口晾在屏幕上十几秒
        （主人看到的就是「窗口弹出来，不动」）。所以这里先看画面有没有像素变化，
        画出来了再进 UIA。
        """
        try:
            import win32gui
            from PIL import ImageGrab, ImageStat

            起点 = time.time()
            轮 = 0
            while time.time() - 起点 < 超时:
                try:
                    l, t, r, b = win32gui.GetWindowRect(h)
                    宽, 高 = r - l, b - t
                    if 宽 > 400 and 高 > 300:
                        框 = (int(l + 宽 * 0.55), int(t + 高 * 0.18),
                              int(l + 宽 * 0.98), int(t + 高 * 0.80))
                        灰 = ImageGrab.grab(bbox=框).convert("L")
                        if ImageStat.Stat(灰).stddev[0] > 2.0:
                            return True
                except Exception:
                    pass
                轮 += 1
                if 轮 % 3 == 0:
                    # 白着的时候顺手叫渲染子窗重画，别干等
                    _重画渲染子窗(h)
                time.sleep(0.12)
            log("等画面画出来超时（%.1f 秒），先照常发" % (超时,))
            return False
        except Exception as e:
            log("等画面画出来出错（不影响发送）：", repr(e))
            return False

    def _重画渲染子窗(h):
        """右侧会话区是白板时，单独叫渲染子窗（MMUIRenderSubWindowHW）重画。

        2026-11-16 实测：从托盘叫起来的窗口，光对父窗 RedrawWindow 有时刷不动
        那块渲染子窗，画面还是白的；单独对它来一次才出来。
        """
        try:
            import win32gui

            找到 = []

            def 回(ch, _):
                try:
                    if "MMUIRenderSubWindow" in win32gui.GetClassName(ch):
                        找到.append(ch)
                except Exception:
                    pass
                return True

            win32gui.EnumChildWindows(h, 回, None)
            标 = 0x0001 | 0x0004 | 0x0100 | 0x0080 | 0x0002   # 无效|擦除|立即|含子|含父
            for ch in 找到:
                win32gui.RedrawWindow(ch, None, None, 标)
                win32gui.UpdateWindow(ch)
            return bool(找到)
        except Exception:
            return False

    def _控件树好了(h):
        """微信的控件树此刻是不是完整的（根是 mmui:: 且子控件不止一个空壳）。"""
        # 自测钩子：bridge/force_coord.flag 存在就强制走坐标通道（2026-11-17 15:2x 加，测完删掉那个文件）
        if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "force_coord.flag")):
            return False
        try:
            import uiautomation as auto

            # 发送跑在子线程里：uiautomation 要求每个线程自己初始化 COM，
            # 不初始化会报「尚未调用 CoInitialize / Can not load UIAutomationCore.dll」，
            # 而且 ControlFromHandle 只会返回一个空壳 → 误判「树没建好」并拒发。
            已初始 = False
            try:
                auto.InitializeUIAutomationInCurrentThread()
                已初始 = True
            except Exception:
                pass
            try:
                c = auto.ControlFromHandle(h)
            finally:
                if 已初始:
                    try:
                        auto.UninitializeUIAutomationInCurrentThread()
                    except Exception:
                        pass
            if c is None:
                return False
            if not (c.ClassName or "").startswith("mmui::"):
                return False
            return len(c.GetChildren()) >= 2
        except Exception:
            return False

    _树提示 = {}

    def _点一下窗口(h, 位置=0.5):
        """往窗口标题栏空白处点一下，把 Qt 真正激活（点完把光标挪回去）。

        2026-11-16 23:12 实测：窗口「可见但没被激活」时，光写 gate 字节
        只能把树建到一半；点一下标题栏能催它建完。
        只点标题栏（y 取窗口顶部往里 12 像素），不会碰到任何按钮。
        """
        try:
            import ctypes
            import win32api
            import win32con
            import win32gui as _wg

            _u = ctypes.windll.user32
            旧 = _wg.GetCursorPos()
            l, t, r, b = _wg.GetWindowRect(h)
            x = int(l + (r - l) * 位置)
            y = int(t + 12)
            _u.SetCursorPos(x, y)
            time.sleep(0.05)
            _u.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            _u.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.05)
            _u.SetCursorPos(int(旧[0]), int(旧[1]))
            return True
        except Exception as e:
            log("点标题栏催重画失败（不影响发送）：", repr(e))
            return False

    def _逼控件树(h=None, 最长=8.0):
        """把微信的无障碍控件树重新顶出来，最多等 `最长` 秒。

        2026-11-16 23:05 实测：微信被藏到托盘再叫出来之后，Qt 会把整棵
        无障碍树退回空壳（只剩一个渲染子窗），这时 wechatauto 的
        `current_chat` / `_chat_input` 全部返回 None，切会话就退到
        「截图 + OCR + 按坐标点」——一次 17~80 秒，窗口亮着不动，
        就是主人看到的假死。

        办法是把 Qt accessibility 那个 gate 字节**重写一遍**（先 0 再 1），
        Qt 会重建整棵树。**关键：只写一次，然后耐心等**（实测要 4.8 秒）。
        2026-11-16 23:11 之前失败的原因就是每 1.8 秒重写一次，每次重写
        都把重建重置，于是永远等不到。

        2026-11-16 23:40 更正：23:20 那条「反复写会把 Qt 写坏」的结论是错的。
        当时看到「写完还是不建树」，实际原因是查询端（发送子线程）没有初始化
        COM，`ControlFromHandle` 永远返回空壳，跟写不写无关。修好 COM 之后
        实测：每次窗口从托盘叫出来、树退回空壳时重写一次 0→1，0.5~5 秒就长回来。
        所以现在允许重写，只做 4 秒冷却，避免同一次发送里反复折腾它。
        """
        h = h or _微信窗口()
        if not h:
            return False
        if _控件树好了(h):
            return True
        # 冷却 4 秒：同一条发送里不必反复写，但换一条消息窗口又藏过时允许再写
        if time.time() - getattr(_逼控件树, "_上次写", 0.0) < 4.0:
            return False
        if getattr(_逼控件树, "_在弄", False):
            return False
        _逼控件树._在弄 = True
        try:
            # 2026-11-17 08:16 起不再自己写字节。库里的 wake() 会扫出**多个**候选
            # RVA、写完校验 mmui 树真出来了没有、没出来就还原字节换下一个、
            # 成功才把 RVA 按 DLL 身份记进缓存。我们原来的写法只写第一个候选、
            # 不校验也不还原 —— 那才是「同一个进程越写越坏、只能重启微信」的来源。
            from wechatauto import accessibility as _acc

            _醒 = _acc.wake([h])
            log("诊断：wake(hwnd=%d) 返回 %r" % (h, _醒))
            if not _醒:
                return False
            _逼控件树._上次写 = time.time()
            起 = time.time()
            上个 = 0
            while time.time() - 起 < 最长:
                if _控件树好了(h):
                    log("控件树顶出来了（%.1f 秒，切会话走控件）" % (time.time() - 起))
                    return True
                now = int((time.time() - 起) * 4)
                if now != 上个:
                    上个 = now
                    try:
                        import uiautomation as _ua

                        _ua.InitializeUIAutomationInCurrentThread()
                        try:
                            _c = _ua.ControlFromHandle(h)
                            log("诊断：%.2f 秒 根=%s 子控件=%d" % (
                                time.time() - 起,
                                getattr(_c, "ClassName", "?"),
                                len(_c.GetChildren()) if _c else -1))
                        finally:
                            _ua.UninitializeUIAutomationInCurrentThread()
                    except Exception as _e:
                        log("诊断：查树出错 %r" % (_e,))
                time.sleep(0.25)
            现在 = time.time()
            if 现在 - _树提示.get("t", 0) > 600:
                _树提示["t"] = 现在
                log("控件树 %.1f 秒还没建好" % 最长)
        except Exception as e:
            log("顶控件树失败（不影响发送）：", repr(e))
        finally:
            _逼控件树._在弄 = False
        return False

    def _面板是白的(h, 坚持=3):
        """右侧会话区此刻是不是整片白板。

        2026-11-15 00:30 实测：窗口从托盘叫起来后、再被我们驱动切会话，
        右侧那块渲染子窗（MMUIRenderSubWindowHW）会有一段时间整片空白 ——
        消息和输入框都没画。wechatauto 的像素探针在这种画面上找不到输入框，
        于是一轮轮重试到超时，字根本没发出去。见到白板就别走那条路。

        2026-11-16 加了两件事：① 要连续「坚持」次都是白的才算白板（切换会话
        那一瞬间本来就是白的，单点判定会把正常情况当白板）；② 判出白板时顺手
        叫渲染子窗重画一次，让主人眼前的画面早点出来。
        """
        for 第 in range(max(1, 坚持)):
            try:
                import win32gui
                from PIL import ImageGrab, ImageStat

                l, t, r, b = win32gui.GetWindowRect(h)
                宽, 高 = r - l, b - t
                if 宽 < 400 or 高 < 300:
                    return False
                框 = (int(l + 宽 * 0.55), int(t + 高 * 0.18),
                      int(l + 宽 * 0.98), int(t + 高 * 0.80))
                灰 = ImageGrab.grab(bbox=框).convert("L")
                if ImageStat.Stat(灰).stddev[0] >= 2.0:
                    return False
            except Exception:
                return False
            if 第 + 1 < max(1, 坚持):
                time.sleep(0.3)
        _重画渲染子窗(h)
        return True

    def _微信在前台():
        """微信此刻是不是前台窗口。全局回车（keybd_event）只在前台时才能按，
        否则会按到主人正在用的别的窗口里去。"""
        try:
            import ctypes

            h = _微信窗口()
            return bool(h) and ctypes.windll.user32.GetForegroundWindow() == h
        except Exception:
            return False

    def _install_quiet_uia(u):
        """给 UIA 引擎打补丁：quiet_mode 下 _activate 直接返回，不抢前台。

        实测 18/18：微信最小化、被遮挡、前台全屏时都能正常发送，
        所以「把窗口拉到最前」这一步可以省掉。不改第三方库文件，只改实例。
        """
        try:
            import types

            if getattr(u, "_quiet_patch", False):
                u.quiet_mode = True
                return
            orig = getattr(u, "_activate", None)
            if orig is None:
                return

            def patched(self, win, _orig=orig):
                if getattr(self, "quiet_mode", False):
                    return
                return _orig(win)

            u._activate = types.MethodType(patched, u)

            # 还有一个漏网的：_force_foreground 会 ShowWindow(SW_RESTORE) +
            # SetForegroundWindow，把最小化的微信直接拉出来。安静模式下让它
            # 直接「假装成功」——UIA 写控件并不需要窗口在前台。
            if not getattr(u, "_quiet_ff", False):
                try:
                    u._force_foreground = lambda hwnd: True
                    u._quiet_ff = True
                except Exception:
                    pass

            # 点击补丁：wechatauto 的 _click_ctrl 会 SetCursorPos + 真实鼠标点击，
            # 切会话/搜索时屏幕上鼠标乱飞就是它。这里优先用 UIA 的 Invoke/Select，
            # 不碰鼠标；控件不支持时才退回原来的模拟点击。
            orig_click = getattr(u, "_click_ctrl", None)
            if orig_click is not None and not getattr(u, "_quiet_click", False):
                def quiet_click(self, ctrl, right=False, _orig=orig_click):
                    if right:
                        return _orig(ctrl, right=True)
                    err1 = err2 = None
                    try:
                        ctrl.GetInvokePattern().Invoke()
                        log("点击控件：走 Invoke（不动鼠标）")
                        return True
                    except Exception as e:
                        err1 = repr(e)
                    try:
                        ctrl.GetSelectionItemPattern().Select()
                        log("点击控件：走 Select（不动鼠标）")
                        return True
                    except Exception as e:
                        err2 = repr(e)
                    log("点击控件：退回真实鼠标点击（%s / %s）" % (err1, err2))
                    return _orig(ctrl, right=False)

                u._click_ctrl = types.MethodType(quiet_click, u)
                u._quiet_click = True

            u._quiet_patch = True
            u.quiet_mode = True
        except Exception as e:
            log("后台模式补丁没打上（不影响发送）：", repr(e))

    # UIA 起不来时的熔断。
    # wechatauto 的 UIA 一失败就退化成「按固定坐标盲点」：每秒两三次扫屏、枚举窗口、
    # 顺手最小化挡着的窗口 —— 主人看到的就是电脑卡成幻灯片，桌面还自己动。
    # 所以连着起不来几次就先停手，别让它一直空转。
    uia_坏 = {"连败": 0, "到": 0.0}

    def _记UIA失败(原因=""):
        uia_坏["连败"] += 1
        文本 = str(原因)
        缺 = ""
        if "No module named 'numpy'" in 文本 or "No module named \"numpy\"" in 文本:
            缺 = "（缺 numpy —— 自己 pip install numpy，装完重启桥接）"
        elif "No module named" in 文本:
            缺 = "（缺 Python 包 —— 自己把它装上，装完重启桥接）"
        if uia_坏["连败"] >= 3 and time.time() >= uia_坏["到"]:
            uia_坏["到"] = time.time() + 300
            log(
                "UIA 连着 %d 次起不来，先停 5 分钟不再碰微信窗口（省得它盲点坐标乱点）%s"
                % (uia_坏["连败"], 缺)
            )

    def _uia熔断中():
        return time.time() < uia_坏["到"]

    def _取引擎():
        """每次发送都问一次最新的 UIA 引擎。

        微信跑久了会把 accessibility gate 那个字节归零，缓存的引擎会「假死」：
        还能收消息，但一条也发不出去，只能重启桥接 —— 每次重取能避开这个坑。

        注意：这里**不打 quiet 补丁**。后台静默那条路（_activate 直接返回 +
        _force_foreground 假装成功）会让窗口没真正激活、后面的点击和粘贴落空，
        切会话时直接把发送卡死。先回到原生行为：会冒头，但发得出去。
        """
        if _uia熔断中():
            return None
        try:
            gui = get_gui()
            u = gui._get_uia()
            if u is None:
                _记UIA失败("引擎给回 None")
                return None
            uia_坏["连败"] = 0
            uia_坏["到"] = 0.0
            return u
        except Exception as e:
            if "未找到微信主窗口" in str(e):
                # 微信在托盘里、或者还没登录时，这句是常态而不是故障：
                # 一不计连败（否则启动时窗口恰好藏着，就可能把 UIA 熔断 5 分钟），
                # 二日志 10 分钟只留一句，免得每次启动都刷一行吓人的报错。
                _现在 = time.time()
                _上次 = getattr(_取引擎, "_窗口不在提示", 0.0)
                if _现在 - _上次 >= 600:
                    _取引擎._窗口不在提示 = _现在
                    log("微信窗口不在，UIA 引擎这次不取（不计失败）")
                return None
            log("取 UIA 引擎失败：", repr(e))
            _记UIA失败(e)
            return None

    def _recover_uia(原因=""):
        """强制重新热激活 UIA（refresh=True），成功返回 True。"""
        if _uia熔断中():
            return False
        try:
            gui = get_gui()
            u = gui._get_uia(refresh=True)
            if u is not None:
                uia_坏["连败"] = 0
                uia_坏["到"] = 0.0
                log("UIA 已重新热激活" + (("（" + 原因 + "）") if 原因 else ""))
                return True
            _记UIA失败("热激活给回 None")
        except Exception as e:
            log("UIA 重新热激活异常：", repr(e))
            _记UIA失败(e)
        return False

    def _press_enter(次=2, ctrl=False):
        """底层按键补回车。

        UIA 的 SendKeys 有时根本不触发发送（字符进了框、回车没生效），
        这里用 keybd_event 硬按几次；ctrl=True 兼容微信里「Ctrl+Enter 发送」的设置。
        """
        try:
            import win32api
            import win32con

            for _ in range(次):
                if ctrl:
                    win32api.keybd_event(0x11, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
                if ctrl:
                    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.12)
            return True
        except Exception as e:
            log("补回车失败：", repr(e))
            return False

    # 键盘通道正在跑（看门狗看到这个就暂时不收窗，否则按键会落到别的窗口）
    忙 = {"键盘": False}

    def _keyboard_send(文字, chat):
        """完全不依赖 UIA 的发送通道：Ctrl+F 搜会话 → 粘贴 → 回车 → 查库确认。

        UIA 假死时的第二条命。代价是要抢一下前台（键盘输入必须有焦点），
        所以只在 UIA 真的发不出去时用。
        """
        忙["键盘"] = True
        try:
            return _键盘发送(文字, chat)
        finally:
            忙["键盘"] = False

    def _键盘发送(文字, chat):
        if not 文字:
            return False
        try:
            import win32con
            import win32gui

            h = _微信窗口()
            if not h:
                log("键盘通道：微信窗口不在前台（没开/最小化/收进托盘），请先把微信打开")
                return False
            log("键盘通道：开始（hwnd=%s）" % h)
            win32gui.ShowWindow(h, 9)
            _强设前台(h)
            time.sleep(0.5)
            log("键盘通道：已顶到前台")
            import win32api

            def _粘贴(s):
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, s)
                    _标记不进剪贴板历史()
                finally:
                    win32clipboard.CloseClipboard()
                time.sleep(0.1)
                win32api.keybd_event(0x11, 0, 0, 0)      # Ctrl
                win32api.keybd_event(0x56, 0, 0, 0)      # V
                win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.4)

            # Ctrl+F 打开搜索 → 输会话名 → 回车进会话
            win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x46, 0, 0, 0)
            win32api.keybd_event(0x46, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.6)
            log("键盘通道：Ctrl+F 已按，准备贴会话名")
            _粘贴(chat)
            _press_enter(1)
            time.sleep(0.8)
            log("键盘通道：会话名已贴并回车，准备贴正文")
            _粘贴(文字)
            _press_enter(1)
            time.sleep(1.2)
            log("键盘通道：正文已贴并回车，查库确认")
            if _already_sent(文字, chat, within=40):
                return True
            # 兼容微信里「Ctrl+Enter 发送」的设置：只补一下，别连着按一串
            # （以前在这里按 2+2+2 下回车，慢路径上同一条会进两条）
            _press_enter(1, ctrl=True)
            time.sleep(1.2)
            return _already_sent(文字, chat, within=40)
        except Exception as e:
            log("键盘通道失败：", repr(e))
            return False

    def _读当前会话(引擎=None, 重试=3):
        """微信此刻打开的是哪个会话（读不到返回 None）。

        用来在「贴字」之前核对目标：贴字不经过搜索，落到哪个会话就是哪个会话，
        一旦上一个动作没把会话切过去，字就发到别人那里了。

        刚切完会话时 UIA 会短暂读不到（2026-11-16 08:40 就读到 None，把一条
        本来能发的消息拦下来了），所以默认再试几次，每次隔 0.35 秒。
        """
        # 藏过再叫出来的窗口，控件树会退回空壳（读什么都 None）——先顶回来。
        # 这里只等一小会儿：真需要重建的话，发送前那道关已经等过了。
        try:
            _逼控件树(最长=2.0)
        except Exception:
            pass
        for 第几次 in range(max(1, 重试)):
            try:
                e = 引擎 if 引擎 is not None else _取引擎()
                if e is not None:
                    值 = e.current_chat
                    # current_chat 在 wechatauto 里是方法（不是属性），两种都兜住
                    读 = 值() if callable(值) else 值
                    if 读:
                        return 读
            except Exception:
                pass
            if 第几次 + 1 < 重试:
                time.sleep(0.35)
        return None

    def _粘贴并回车(文字, chat):
        """白板兜底通道：不找输入框、不碰像素探针，直接把字粘进去按回车。

        白板时控件树和焦点都在，只是没画出来；像素探针找死也找不到输入框，
        但 Ctrl+V 照样能进到输入框里。发完用数据库确认，不靠 UIA 回读。

        2026-11-16 00:43 出过事故：`打开会话` 没切过去、面板是白板，这里照样
        贴字，字直接落进了当时打开的那个群。所以贴之前必须核对会话，对不上不发。
        """
        if not 文字:
            return False
        现在 = _读当前会话()
        if 现在 != chat:
            log(
                "白板兜底：当前会话是「%s」，不是「%s」，这条不发（避免发错地方）"
                % (现在, chat)
            )
            return False
        try:
            import win32api
            import win32clipboard
            import win32con

            h = _微信窗口()
            if not h:
                return False
            if not _强设前台(h):
                log("白板兜底：没能切到前台，照常试一次")
            time.sleep(0.3)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, 文字)
                _标记不进剪贴板历史()
            finally:
                win32clipboard.CloseClipboard()
            time.sleep(0.12)
            win32api.keybd_event(0x11, 0, 0, 0)                      # Ctrl
            win32api.keybd_event(0x56, 0, 0, 0)                      # V
            win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.5)
            _press_enter(1)
            time.sleep(1.0)
            if _already_sent(文字, chat, within=40):
                return True
            # 一次回车没发出去（焦点/输入法问题）才补第二下。以前一上来就按两次，
            # 微信偶尔会把两下分别当成两次发送 —— 2026-11-16 07:50:19 / 07:51:00
            # 同一句话进了两条，就是这种「回车多按」。
            log("白板兜底：按了一次回车库里没见到，再补一次")
            _press_enter(1)
            time.sleep(1.0)
            return _already_sent(文字, chat, within=40)
        except Exception as e:
            log("白板兜底失败：", repr(e))
            return False

    def _点窗口相对(h, fx, fy):
        """按窗口尺寸的相对位置点一下：不依赖控件树，也不看像素内容。

        2026-11-17 15:00 实测这条路子能发（点搜索框 → 粘会话名 → 回车 → 点输入框
        → 粘正文 → 回车，2~4 秒一条），控件树坏掉时拿它当主力。
        """
        try:
            import win32api
            import win32con
            import win32gui

            r = win32gui.GetWindowRect(h)
            宽, 高 = r[2] - r[0], r[3] - r[1]
            if 宽 < 300 or 高 < 300:
                log("坐标点击：窗口太小（%dx%d），点不了" % (宽, 高))
                return False
            x, y = r[0] + int(宽 * fx), r[1] + int(高 * fy)
            旧 = win32api.GetCursorPos()
            win32api.SetCursorPos((x, y))
            time.sleep(0.12)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.25)
            win32api.SetCursorPos(旧)
            return True
        except Exception as e:
            log("坐标点击失败：", repr(e))
            return False

    def _粘一段(文字):
        """文字放进剪贴板再 Ctrl+V（中文只能这么进去）。"""
        try:
            import win32api
            import win32con
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, 文字)
                _标记不进剪贴板历史()
            finally:
                win32clipboard.CloseClipboard()
            time.sleep(0.1)
            win32api.keybd_event(0x11, 0, 0, 0)                      # Ctrl
            win32api.keybd_event(0x56, 0, 0, 0)                      # V
            win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.35)
            return True
        except Exception as e:
            log("往剪贴板放字失败：", repr(e))
            return False

    def _坐标发送(文字, chat, 需要切=True):
        """坐标 + 剪贴板的发送通道：不读控件树、不做像素探针。

        控件树坏掉时 UIA 那条路会退到 OCR/坐标点击，一次 17~80 秒，窗口亮着不动
        就是主人说的「假死」。这条路只要窗口可见、在前台，2~4 秒就能发完一条，
        发完照旧用数据库确认（不靠回读界面）。
        """
        if not 文字:
            return False
        h = _微信窗口()
        if not h:
            log("坐标通道：微信窗口不在前台，这条不发")
            return False
        if not _强设前台(h):
            log("坐标通道：切不到前台，这条不发（免得按键落到别的窗口）")
            return False
        time.sleep(0.2)
        if 需要切:
            if not _点窗口相对(h, 0.14, 0.09):
                return False
            if not _粘一段(chat):
                return False
            time.sleep(0.9)
            _press_enter(1)
            time.sleep(1.4)
            try:
                对上了 = bool(get_gui()._chat_is_open(chat))
            except Exception as e:
                log("坐标通道：核对会话标题出错（%r），按对不上处理，继续按名字发" % (e,))
                对上了 = False
            if not 对上了:
                # 画面可能还停在旧帧上，OCR 读到的标题是旧的 —— 但按名字搜索切会话
                # 本身是可靠的（2026-11-17 15:00 手工验过）。所以这里只记一笔，照发，
                # 发完用数据库确认到底落在哪个会话。
                log("坐标通道：标题 OCR 对不上「%s」（画面可能是旧帧），仍按名字发，发完查库" % chat)
        if not _点窗口相对(h, 0.50, 0.84):
            return False
        if not _粘一段(文字):
            return False
        time.sleep(0.15)
        _press_enter(1)
        time.sleep(1.0)
        if _already_sent(文字, chat, within=30):
            return True
        log("坐标通道：按了一次回车库里没见到，再补一次")
        _press_enter(1)
        time.sleep(1.0)
        return _already_sent(文字, chat, within=30)

    def _ensure_wechat_ready(重试=2, 前台=False):
        """确认微信窗口能操作。

        默认只确认窗口在、不抢前台（文字用 UIA 直接写控件，不需要焦点）；
        需要粘贴图片、或文字发送失败重试时才传 前台=True 把窗口顶上来。
        """
        if not 前台:
            return _微信窗口() is not None
        for 第 in range(重试 + 1):
            try:
                import win32gui

                found = []

                def cb(hwnd, _):
                    try:
                        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                            found.append(hwnd)
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(cb, None)
                if not found:
                    # 不自动拉起、不自动登录，也不替主人把窗口翻出来。
                    _报微信掉线("拿不到微信窗口（没开，或被最小化/收进托盘）")
                    continue
                if found:
                    h = found[0]
                    win32gui.ShowWindow(h, 9)          # SW_RESTORE
                    if not _强设前台(h):
                        log("微信窗口没能切到前台（继续试一次）")
                    # 发送期间把微信置顶：DSH 窗口（我自己）一刷新就会盖上来，
                    # 这是「字卡在输入框」最主要的来源
                    try:
                        import win32con

                        win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                        STATE["wx_hwnd"] = h
                    except Exception:
                        pass
                    time.sleep(0.4)
                    return True
            except Exception as e:
                log("微信窗口准备失败：", repr(e))
            time.sleep(0.4)
        return False

    def 拆条(text: str, 上限: int = 60):
        """把一段话拆成几条短消息：先按换行分段，长段再按句子拆。"""
        out = []
        for 段 in re.split(r"\n+", str(text or "")):
            段 = 段.strip()
            if not 段:
                continue
            if len(段) <= 上限:
                out.append(段)
                continue
            句们 = re.split(r"(?<=[。！？!?…])", 段)
            cur = ""
            for 句 in 句们:
                if not 句:
                    continue
                if len(cur) + len(句) <= 上限:
                    cur += 句
                else:
                    if cur.strip():
                        out.append(cur.strip())
                    cur = 句
            if cur.strip():
                out.append(cur.strip())
        return out or [str(text or "").strip()]


    def _chat_username(chat):
        """聊天名 → 数据库里的 username。群必须走 group_name_to_id（nickname 查不到群）。"""
        try:
            g = db.group_name_to_id(chat)
            if g:
                return g
        except Exception:
            pass
        try:
            return db.username_by_nickname(chat) or chat
        except Exception:
            return chat


    def _是我发的(m):
        """这条是不是本账号自己发的。

        微信 4.1 的库里「我」的 sender_id 是 1（别人是各自的 id），不是 3.x 的 2，
        所以按 wxid 认最稳：sender_username == 本账号 wxid。拿不到 wxid 时退回
        「sender_id 为空/0」的旧判断。
        """
        su = (m.get("sender_username") or "").strip()
        if su and me:
            return su == me
        return not m.get("sender_id")


    def _等落库(text, chat, 超时=2.5, 间隔=0.6):
        """发完等它落库（微信写库是异步的）。等不到也**不重发** —— 只用来把日志写实。"""
        截止 = time.time() + max(0.0, 超时)
        while True:
            if _already_sent(text, chat, within=30):
                return True
            if time.time() >= 截止:
                return False
            time.sleep(间隔)


    def _already_sent(text, chat, within=60):
        """库里最近已经有这句【我自己发的】话 → 其实发出去了，别再点一次。

        2026-11-13 修复两个致命误判：
        1) 旧逻辑用 `c in text or text in c` 模糊匹配。主人发来「在吗」，我回「在」，
           库里那条「在吗」把「在」包含在内 → 误判成「这句我发过了」，于是整条消息被静默跳过，
           微信里永远收不到。（21:16 那次就是这个）
        2) 旧逻辑不看发送方，对方说的话也能触发「已发过」。现在只认 sender_id 为空/0 的自发消息。
        """
        try:
            u = _chat_username(chat)
            rows = db.get_messages(u, limit=12)
            now = time.time()
            目标 = (clean_text(text) or "").strip()
            if not 目标:
                return False
            for m in rows or []:
                ts = float(m.get("create_time") or 0)
                if not ts or abs(now - ts) > within:
                    continue
                # 「我」发的才算；别人的消息一律不算（sender_username 认 wxid）
                if not _是我发的(m):
                    continue
                c = (clean_text(m.get("content") or "") or "").strip()
                if c and c == 目标:
                    return True
        except Exception:
            pass
        return False

    # 进程内「这句我已经发出去过」的记录。数据库查询有 25/120 秒的时间窗，
    # 兜底通道晚几秒醒来、或者拆条后隔了几十秒再补发，窗口一过就会重复发一遍
    # （就是「老是发两次一模一样」）。这里用不过期的内存记录兜住。
    _发过的段 = {}

    def _记发过(text):
        try:
            k = clean_text(text)
            if k:
                _发过的段[k] = time.time()
        except Exception:
            pass

    def _段已发过(text, within=3600):
        try:
            k = clean_text(text)
            t = _发过的段.get(k)
            return bool(t) and (time.time() - t) <= within
        except Exception:
            return False


    def _localize_image(p):
        """正文 markdown 里的本机 HTTP 图（表情包服务等）先落成本地文件，再交给微信发。"""
        if not re.match(r"^https?://", p, re.I):
            return p if os.path.exists(p) else None
        from urllib.parse import urlparse

        u = urlparse(p)
        if (u.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            log("⚠ 不是本机的图，跳过：", p)
            return None
        ext = os.path.splitext(u.path)[1].lower()
        if ext not in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=ext, prefix="wxbridge_")
            os.close(fd)
            with urllib.request.urlopen(p, timeout=15) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            return tmp
        except Exception as e:
            log("⚠ 图下载失败：", p, repr(e))
            try:
                if tmp:
                    os.unlink(tmp)
            except Exception:
                pass
            return None

    def _send_image_dib(gui, path, chat):
        """发图：把图转成 CF_DIB 位图放剪贴板 → Ctrl+V → 回车。

        微信 4.x 不吃 wechatauto 的 CF_HDROP 路线（粘贴后草稿是空的，
        回车等于发了个寂寞，数据库里也不会有图片消息），但认 CF_DIB。
        GIF 只发第一帧（微信粘贴位图本来也不会动）。
        """
        import io
        import ctypes
        from PIL import Image

        _t0 = time.time()
        try:
            im = Image.open(path)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
        except Exception as e:
            log("⚠ 图片打不开：", repr(e))
            return False
        buf = io.BytesIO()
        try:
            im.save(buf, format="BMP")
            dib = buf.getvalue()[14:]   # 去掉 BMP 文件头，剩下的就是 DIB
        except Exception as e:
            log("⚠ 图片转位图失败：", repr(e))
            return False
        _t1 = time.time()

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        快照 = _剪贴板快照()
        错误 = None
        try:
            if not u32.OpenClipboard(None):
                log("⚠ 剪贴板打不开")
                return False
            try:
                u32.EmptyClipboard()
                h = k32.GlobalAlloc(0x0002 | 0x0040, len(dib))
                if not h:
                    错误 = "剪贴板内存分配失败"
                else:
                    ptr = k32.GlobalLock(h)
                    if not ptr:
                        k32.GlobalFree(h)
                        错误 = "剪贴板内存锁定失败"
                    else:
                        ctypes.memmove(ptr, dib, len(dib))
                        k32.GlobalUnlock(h)
                        if not u32.SetClipboardData(8, h):
                            k32.GlobalFree(h)
                            错误 = "写剪贴板失败"
                        else:
                            _标记不进剪贴板历史()      # 图片也别留进 Win+V
            finally:
                u32.CloseClipboard()
        except Exception as e:
            log("⚠ 剪贴板异常：", repr(e))
            错误 = "剪贴板异常"
        if 错误 is not None:
            log("⚠ " + 错误)
            # 写图失败时剪贴板已经被 EmptyClipboard 清空了，把主人原来的内容还原回去
            try:
                _剪贴板还原(快照)
            except Exception:
                pass
            return False

        box = None
        # 先用 UIA 直接问控件树要输入框（像素探针在窗口被挡时要空扫 20+ 秒），
        # 拿不到再退回原来的像素探针。
        try:
            _u = gui._get_uia()
            _e = _u._chat_input() if _u else None
            if _e is not None:
                _r = _e.BoundingRectangle
                box = (int(_r.left) - gui.origin_x, int(_r.top) - gui.origin_y,
                       int(_r.right) - gui.origin_x, int(_r.bottom) - gui.origin_y)
                try:
                    _e.SetFocus()
                except Exception:
                    pass
                time.sleep(0.15)
        except Exception as e:
            log("⚠ UIA 取输入框失败（退回像素探针）：", repr(e))
            box = None
        for 第 in range(3 if not box else 0):
            if 第 > 0:
                # 外层刚发完文字、窗口多半是好的；只有真探测不到时才重新摆窗口
                try:
                    _ensure_wechat_ready(前台=True)
                except Exception:
                    pass
            try:
                _g0 = time.time()
                box = gui.get_input_box()
                log("  探测输入框用了 %.1fs" % (time.time() - _g0))
            except Exception as e:
                log("⚠ 探测输入框异常：", repr(e))
                box = None
            if box:
                break
            log("⚠ 发图探测不到输入框，第 %d 次重试" % (第 + 1))
            time.sleep(0.5)
        if not box:
            log("⚠ 发图时找不到输入框")
            return False
        try:
            gui.focus_input(box)
        except Exception as e:
            log("⚠ 聚焦输入框失败：", repr(e))
            return False
        time.sleep(0.2)
        窗口 = _微信窗口()
        if not _确保前台(窗口):
            log("✗ 前台切不过去，这张图不发（绝不盲粘）：%s" % os.path.basename(path))
            _剪贴板还原(快照)
            return False
        _t2 = time.time()
        gui._input.key(0x56, ctrl=True)   # VK_V
        time.sleep(0.8)
        gui._input.key(0x0D)              # VK_RETURN
        time.sleep(0.7)
        _t3 = time.time()
        _剪贴板还原(快照)
        log("⏱ 发图耗时 %.1fs：转图 %.1f / 输入框+前台 %.1f / 粘贴回车 %.1f / 还原 %.1f" % (time.time() - _t0, _t1 - _t0, _t2 - _t1, _t3 - _t2, time.time() - _t3))
        return True

    def _last_image_ts(chat):
        """聊天里最新一张图片消息的时间戳；没有就是 0。用来判断“刚才那张到底发出去没有”。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=8) or []:
                if m.get("type") == "图片":
                    return float(m.get("create_time") or 0)
        except Exception:
            pass
        return 0.0

    def _image_sent_recently(chat, since):
        """发完去库里确认一下：真出现图片消息才算发成了。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=4) or []:
                if m.get("type") == "图片" and float(m.get("create_time") or 0) >= since - 2:
                    return True
        except Exception:
            pass
        return False

    def _框里的字(超时=2.5):
        """用 UIA 读输入框里的文字。读不到返回 None（表示"不知道"，不是"空"）。

        像素判据会把光标、占位提示、发送按钮都算成"有字"，据此重试会一直空转，
        所以优先问 UIA 要真实文本，读不到才退回像素判断。

        2026-11-14 22:59：这个 UIA 调用会卡十几秒（微信刚被 ShowWindow 叫起来、
        界面还在半死状态时尤其明显），而它被 _kick_enter 连着调好几次 —— 于是
        一条已经发出去的话还能把发送线程拖住 40 多秒，窗口就一直亮在主人屏幕上
        （他看到的「假卡死」）。所以读一次最多等 超时 秒，读不到就当"不知道"。
        """

        def 读():
            uia = _取引擎()
            if uia is None:
                return None
            ctrl = uia._chat_input()
            if ctrl is None:
                return None
            r = uia._read_edit_value(ctrl) if hasattr(uia, "_read_edit_value") else None
            return None if r is None else str(r)

        盒 = {}

        def 跑():
            try:
                盒["r"] = 读()
            except Exception:
                盒["r"] = None

        t = threading.Thread(target=跑, daemon=True)
        t.start()
        t.join(超时)
        if t.is_alive():
            log("读输入框超过 %s 秒没回应，这次当读不到" % 超时)
            return None
        return 盒.get("r")

    def _清空输入框():
        """发送前把输入框里可能残留的上一条清掉。

        发失败时文字会留在输入框（wechatauto 只校验粘贴、不校验回车），
        下一次任意回车会把这段残渣一起发出去 —— 表现就是同一条发了两遍。
        """
        try:
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            gui = get_gui()
            uia = _取引擎()
            if uia is None:
                return False
            ctrl = uia._chat_input()
            if ctrl is None:
                return False
            try:
                vp = ctrl.GetValuePattern()
                if vp is not None and str(vp.Value or ""):
                    vp.SetValue("")
                    log("发送前清掉了输入框里的残留")
                    return True
            except Exception:
                pass
            try:
                import win32api

                ctrl.SetFocus()
                time.sleep(0.05)
                win32api.keybd_event(0x11, 0, 0, 0)   # Ctrl down
                win32api.keybd_event(0x41, 0, 0, 0)   # A down
                win32api.keybd_event(0x41, 0, 2, 0)
                win32api.keybd_event(0x11, 0, 2, 0)   # Ctrl up
                win32api.keybd_event(0x08, 0, 0, 0)   # Backspace
                win32api.keybd_event(0x08, 0, 2, 0)
                log("发送前用键盘清空了输入框残留")
                return True
            except Exception as e:
                log("发送前清空输入框失败（不影响发送）：", repr(e))
                return False
        except Exception:
            return False

    def _kick_enter(最多=3, 预算=6.0):
        """把卡在输入框里的话踹出去，返回 True 表示框已空。

        根因：wechatauto 的 UIA 发送只校验「文字有没有粘进输入框」，不校验回车
        之后输入框有没有清空。窗口被抢焦点时回车落空，字就留在输入框里，
        而接口已经报了「已操作发送」—— 表现就是「消息卡在输入框没发出去」。

        以前只补一次回车，落空一次就又堵在那里；现在按「框里还有字就继续踹」
        来判：回车三次还清不掉，就回退到 guia 的 click_send（它会 OCR 找到
        「发送」按钮去点，并且每次点完都校验输入框清空）。

        2026-11-14 22:59：加了 预算 秒的总时限 —— 这个函数里每一步都是 UIA
        调用，微信半死时一步就要十几秒，三圈下来 40 多秒，把发送线程和窗口
        一起拖住。宁可这轮不踹，也不能卡在这里。
        """
        起点 = time.time()
        try:
            gui = get_gui()
            for _ in range(最多):
                if time.time() - 起点 > 预算:
                    log("踹回车超过 %s 秒还没结果，这轮不踹了" % 预算)
                    return False
                框 = _框里的字()
                if 框 is not None:
                    if not 框.strip():
                        return True
                else:
                    try:
                        if not gui._input_box_has_text():
                            return True
                    except Exception:
                        break
                try:
                    gui._input.key(0x0D)          # VK_RETURN
                except Exception:
                    uia = _取引擎()
                    if uia is None:
                        break
                    e = uia._chat_input()
                    if e is None:
                        break
                    e.SendKeys("{Enter}", waitTime=0.05)
                time.sleep(0.4)
            if time.time() - 起点 > 预算:
                return False
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            if 框 is None:
                try:
                    if not gui._input_box_has_text():
                        return True
                except Exception:
                    pass
            # 不再点「发送」按钮：那条路要真实移动鼠标（OCR 定位 + 点击），
            # 发消息时鼠标会乱飞，很吓人。改成纯键盘再踹一轮。
            log("输入框还堵着，再踹一轮回车（不碰鼠标）")
            try:
                # 优先用 UIA 元素定向发回车（后台也能用，不挑前台）；
                # 全局回车只在前台窗口是微信时才按，否则会按到主人正在用的窗口上
                uia = _取引擎()
                输入框 = uia._chat_input() if uia is not None else None
                if 输入框 is not None:
                    输入框.SendKeys("{Enter}", waitTime=0.05)
                elif _微信在前台():
                    # 一次就够：多按的那几下有可能把主人正在打的草稿顶出去
                    _press_enter(1)
                    _press_enter(1, ctrl=True)
                else:
                    log("微信不在前台，跳过全局回车（免得按到别的窗口）")
            except Exception as e:
                log("踹回车失败：%r" % (e,))
            time.sleep(1.2)
            框 = _框里的字()
            if 框 is None or not 框.strip():
                return True
            log("输入框还是堵着，这条先放弃")
            return False
        except Exception as e:
            log("补回车失败：%r" % (e,))
            return False

    def 拟人延迟秒() -> float:
        """人不会两秒就回：真发之前随机等一会儿。下限/上限都 <=0 就不等。"""
        try:
            低 = float(CFG.get("回复延迟下限秒") or 0)
            高 = float(CFG.get("回复延迟上限秒") or 0)
        except Exception:
            return 0.0
        低 = max(0.0, 低)
        高 = max(低, 高)
        if 高 <= 0:
            return 0.0
        return random.uniform(低, 高)

    def _洗掉代码和标签(文本):
        """把不像人话的东西从要发出去的文字里洗掉。

        2026-11-17 07:54：会话回复里混进一截 `<job id="1" status="running" kind…`，
        原样发到了微信，看起来就是「把代码发出去了」。这里做最后一道拦截：
        整段代码块直接丢、行内反引号只去符号、工具/系统标签整段删。
        """
        import re as _re

        t = str(文本 or "")
        t = _re.sub(r"```[\s\S]*?```", "", t)
        t = _re.sub(r"`([^`\n]*)`", r"\1", t)
        t = _re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", t, flags=_re.I)
        t = _re.sub(r"<job\b[\s\S]*$", "", t, flags=_re.I)
        t = _re.sub(r"</?(job|tool|function_calls?|analysis|commentary|instruction|system)[^>]*>", "", t, flags=_re.I)
        t = _re.sub(r"</?[A-Za-z][\w:-]*(?:\s[^>]*)?>", "", t)
        t = _re.sub(r"[ \t]+\n", "\n", t)
        t = _re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    def send_one(item):
        text = _洗掉代码和标签(str(item.get("text") or "").strip())
        chat = str(item.get("chat") or "").strip()
        images = [str(p) for p in (item.get("images") or []) if p]

        卡线程 = {"t": None}   # 上一次带超时的发送线程；还活着就不许重发

        def 带超时(动作, 超时=25):
            """send_msg 偶尔会卡在 UIA 里不出来，给个上限，别把整条队列堵死。"""
            盒 = {}

            def 跑():
                try:
                    盒["r"] = 动作()
                except Exception as e:
                    盒["e"] = e

            t = threading.Thread(target=跑, daemon=True)
            t.start()
            t.join(超时)
            if t.is_alive():
                # 超时必须当成失败：以前这里返回字符串「发送超时」，
                # 试() 把它当成返回值真、算发送成功，于是既不去补发也不报警，
                # 日志还打「✅ 已发到微信」—— 2026-11-15 00:26 那条就是这么丢的
                #
                # 但线程没死：它可能过几十秒把字真发出去。记下来，谁要重发
                # 先看它还活着没有（2026-11-16 07:50/07:51 重复两条就是重发撞上它）
                卡线程["t"] = t
                return TimeoutError("发送超时")
            return 盒.get("e") or 盒.get("r")

        def 试(动作, 名字, 重试前=1, 超时=25):
            """发一次；返回失败（不是抛异常）也要当成失败 —— 窗口没接住时就是这样。"""
            for 第 in range(重试前 + 1):
                try:
                    r = 带超时(lambda: 动作(gui), 超时=超时)
                except Exception as e:
                    r = e
                if not isinstance(r, Exception) and (r is None or r):
                    return True
                原因 = ""
                try:
                    原因 = str(r.get("message") or "") if hasattr(r, "get") else str(r)
                except Exception:
                    原因 = str(r)
                # 已经发出去了、只是数据库还没落库 —— 再发一遍就成了两条重复消息
                if "未确认" in 原因 or "已操作发送" in 原因:
                    # 但字也可能压根没发出去，就卡在输入框里 —— 补一次回车再确认
                    if _kick_enter():
                        time.sleep(1.2)
                        log("已操作发送但库里没确认，补了一次回车：" + 原因[:50])
                    else:
                        log("发出去但库里没确认，不再重发：" + 原因[:50])
                    return True
                log("第 %d 次%s没成：%s" % (第 + 1, 名字, 原因[:80]))
                if 第 < 重试前:
                    # 上一次的发送线程要是还卡在 UIA 里没回来，就别急着重发：
                    # 它可能自己把字发出去，我们再发一遍就成了两条（07:50/07:51 那次）
                    _卡 = 卡线程.get("t")
                    if _卡 is not None and _卡.is_alive():
                        log(
                            "上一步发送还卡在 UIA 里没回来，这条先不重发"
                            "（免得同一句进两条）"
                        )
                        return False
                    # 先原地重试；还是不行就重新热激活 UIA（引擎假死时只能这样救），
                    # 最后才把微信拉到前台 —— 平时不抢主人正在用的窗口
                    if 第 == 0:
                        _recover_uia("发送失败")
                    else:
                        _ensure_wechat_ready(前台=True)
                    time.sleep(0.6)
            return False



        # 正文里的 [表情: 描述] 换成真图片（表情库里有就发图，没有就整条标记丢掉）
        表情图 = []

        def _repl(m):
            p = find_meme(m.group(1))
            if p:
                表情图.append(p)
                log("表情匹配：%s → %s" % (m.group(1)[:24], os.path.basename(p)))
                return ""
            # 匹配不上就把标记丢掉。以前是「保持原样」，结果 "[表情: 得意地叉腰，等着被夸]"
            # 当文字发到了主人手机上（11-14 早上）——少一张图也比露出一串方括号强。
            log("表情没匹配到（标记已丢弃）：%s" % m.group(1)[:24])
            return ""

        text = re.sub(r"\[表情:\s*([^\]]+)\]", _repl, text).strip()
        images = 表情图 + images
        if images:
            log("这条带 %d 张图要发" % len(images))

        # 兜底：DSH 那边没摘干净的 markdown 图片（只认本机地址），这里再摘一次，
        # 免得微信里收到一串 "![xxx](http://127.0.0.1:...)" 的文字。
        def _摘图(m):
            u = m.group(1)
            if re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", u, re.I):
                images.append(u)
                return ""
            return m.group(0)

        text = re.sub(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", _摘图, text).strip()

        if not text and not images:
            return {"ok": False, "说": "空消息"}
        if not chat:
            return {"ok": False, "说": "没指定发给谁（chat 空着）"}

        # 发之前记一下剪贴板原本有什么：发完只清「我们自己粘进去的那份」，
        # 主人自己复制的东西别动（他要粘贴一段话，不能被她的一轮回复清掉）。
        # 收尾跑过几次。每条消息粘完就已经收过一次，最后 finally 里那次只在
        # 「一次都没收成」（半路抛异常）时才补 —— 否则发完十几秒还会再动一次剪贴板，
        # 2026-11-14 21:34 就是那次把主人原来的内容弄丢的（详见 _剪贴板收尾 的注释）。
        收尾次数 = [0]
        剪贴板前 = _剪贴板快照()

        def _剪贴板收尾():
            """粘完就收：把「我们自己刚粘进去的那份」还原/清掉，别人动过的内容一律不碰。

            2026-11-14 21:15 改：这段原来只在 finally 里跑一次，而 finally 前面还压着
            等落库（`_等落库`，慢的时候十几秒）和发图确认 —— 那段时间剪贴板里就一直
            躺着刚发出去的那句话，主人这时按 Ctrl+V 粘出来的还是它，看着像「没清」。
            现在每粘完一次就收一次，窗口缩到零点几秒。

            2026-11-14 21:5x 再改两处（当晚实测：发完约 15 秒剪贴板被清空，主人原来
            复制的那段字丢了）：
              1. finally 里那次收尾改成「一次都没收过才补」。正常发送已经收过一次，
                 这一遍跑在落库确认之后（十几秒），多一次就多一次误清的机会；
              2. 「有图就算是我们粘的」这条加个前提：剪贴板里没有别人的字。原来的写法
                 只要有位图就认成我们的，连主人刚复制的那段字一起 EmptyClipboard 掉。
            每次真动手都记一行日志，下次再出这种事能对着时间点查。
            """
            try:
                if _剪贴板里还是我们的(剪贴板前, [text] + 拆条(text)):
                    旧 = ""
                    try:
                        旧 = str((剪贴板前 or {}).get("text") or "")
                    except Exception:
                        旧 = ""
                    # 快照里装的要是我们自己发过的话（上一轮漏清留下的），还原它等于把
                    # 「没清」这个状态一直传下去 —— 那种情况直接清空
                    _自家话 = [str(项).strip() for 项 in ([text] + 拆条(text))]
                    try:
                        _自家话 += [str(过).strip() for 过, _时 in recently_sent
                                    if time.time() - _时 < 900]
                    except Exception:
                        pass
                    _快照是自家人 = bool(旧.strip()) and (
                        旧.strip() in _自家话
                        or (len(旧.strip()) >= 4 and _already_sent(旧, chat, within=900))
                    )
                    if _快照是自家人:
                        _清空剪贴板()
                        log("剪贴板收尾：发送前的快照就是我自己的话，直接清空")
                    elif 剪贴板前:
                        _剪贴板还原(剪贴板前)
                        log("剪贴板收尾：还原成发送前的内容")
                    else:
                        _清空剪贴板()
                        log("剪贴板收尾：清空（发送前剪贴板就是空的）")
                    收尾次数[0] += 1
            except Exception as e:
                log("剪贴板收尾出错（不影响发送）：", repr(e))

        # 机器痕迹别发到微信：DSH 偶尔会把系统提示/工具输出当正文吐出来
        # （2026-11-13 23:28 主人就收到一条以 "<persisted-output> 记忆已保存…" 开头的鬼东西）。
        # 拦在发送前，宁可这一条不发，也别把内部流程倒进聊天窗。
        if text.lstrip().startswith("<persisted-output>") or "</persisted-output>" in text:
            log("拦下一条机器痕迹，不发到微信：%s" % text[:40])
            return {"ok": False, "说": "机器痕迹，已拦下"}

        # 主人 2026-11-14 定的死规矩：微信退出 / 停在登录界面 → **不许自动拉起、不许自动登录**，
        # 只汇报，等他自己来扫码。
        # 2026-11-23 起也不再「从托盘调出」：只在前台发送，窗口不在就先请主人打开。
        if _微信窗口() is None:
            _报微信掉线("微信窗口不在前台（没开，或被最小化/收进托盘）")
            return {"ok": False, "说": "微信不在前台（请先把微信窗口打开；后台调出已停用）"}

        # 拟人延迟：人收到消息不会两秒就回。真发之前随机等 8–25 秒（可配）。
        # 放在 send_lock 外面等，免得等的时候把别的消息一起堵住。
        _等多久 = 拟人延迟秒()
        if _等多久 > 0:
            log("（拟人延迟）%.0f 秒后再发：%s" % (_等多久, (text or "（图片）")[:20]))
            time.sleep(_等多久)

        def _掐(名字, 动作):
            """跑一步并计时：哪一步把窗口晾在屏幕上几十秒，日志里直接看得见。"""
            t0 = time.time()
            try:
                return 动作()
            finally:
                d = time.time() - t0
                if d >= 1.0:
                    log("⏱ %s 用了 %.1f 秒" % (名字, d))

        with send_lock:
            # UIA 坏着的时候整条发送路都不许走：下面 _ensure_wechat_ready / open_chat /
            # 读输入框都会进 wechatauto 的窗口操作，它一失败就退化成坐标盲点、
            # 顺手最小化挡着的窗口 —— 队列里积着几条消息就是每秒两三次的空转。
            # UIA 熔断时不再整条拒发：坐标通道不碰 UIA，照常能发（2026-11-17 15:00 起）。
            # 以前这里直接 return，控件树一坏就一条也发不出去。
            UIA坏 = _uia熔断中()
            树好 = False if UIA坏 else _控件树好了(_微信窗口())
            if not 树好 and text:
                # 控件树坏的时候不碰 UIA：点搜索框切会话 → 点输入框 → 粘正文 → 回车，
                # 发完查库确认（2026-11-17 15:00 实测 2~4 秒一条，不再退 OCR 慢慢磨）
                都成了 = True
                切过 = False
                for _第几段, 一 in enumerate(拆条(text)):
                    if _第几段:
                        time.sleep(random.uniform(0.9, 2.0))
                    if _already_sent(一, chat, within=25):
                        log("这句刚发过，跳过：%s" % 一[:30])
                        continue
                    ok = _掐("发文字(坐标)", lambda t=一: _坐标发送(t, chat, 需要切=not 切过))
                    切过 = True
                    _剪贴板收尾()
                    if ok:
                        log("✅ 坐标通道已发出：%s" % 一[:26])
                    else:
                        都成了 = False
                        log("✗ 坐标通道没发出去：%s" % 一[:26])
                        break
                return {"ok": 都成了, "说": "" if 都成了 else "坐标通道没发出去"}
            try:
                # 先不抢前台：同一条会话里连发时，UIA 直接写控件就够了，
                # 把窗口顶上来只会让主人看见它闪。只有要切会话时才摆正窗口。
                _掐("摆正窗口(不抢前台)", lambda: _ensure_wechat_ready(前台=False))
                gui = get_gui()
                # 每次发送前重取一次引擎：微信跑久了 accessibility 会假死，
                # 缓存的引擎能收消息却一条也发不出去
                try:
                    _引擎 = _掐("取 UIA 引擎", lambda: 带超时(_取引擎, 超时=8))
                except Exception as e:
                    # UIA 引擎偶尔会一直不返回（微信那边卡住），不设上限会白等十几秒，
                    # 窗口就一直亮着，主人看到的就是假死（2026-11-16 23:28 那次）
                    log("取 UIA 引擎超时/失败（继续往下试）：", repr(e))
                    _引擎 = None
                # 发之前先清干净输入框：上一次失败的字留在里面，
                # 这一次回车会把它一起带出去（同一句话发两遍的根源）
                try:
                    _掐("清空输入框", lambda: 带超时(_清空输入框, 超时=6))
                except Exception as e:
                    log("清空输入框超时/失败（继续）：", repr(e))
                # 已经在目标会话里就别再搜一次 —— open_chat 要点搜索框、输入名字，
                # 这些动作会把最小化的微信拉到前面来（主人就看见窗口突然蹦出来）
                当前会话 = None
                try:
                    if _引擎 is not None:
                        当前会话 = _掐("读当前会话",
                                    lambda: 带超时(lambda: _读当前会话(_引擎), 超时=8))
                except Exception:
                    当前会话 = None
                if 当前会话 != chat:
                    # 读不到当前会话时（微信这一版 UIA 树经常是空的），先花 1 秒读一眼标题：
                    # 界面本来就停在这个会话上，就不用再切了。切一次要 6–80 秒，
                    # 这段时间窗口必须亮着，主人看到的就是「假死」。
                    try:
                        if _掐("核对会话标题(OCR)", lambda: get_gui()._chat_is_open(chat)):
                            log("界面已经停在这个会话上（%s），不重复切" % chat)
                            当前会话 = chat
                    except Exception as e:
                        log("核对会话标题失败（继续切）：", repr(e))
                if 当前会话 != chat:
                    # 切会话这一下躲不掉：得让窗口在前，open_chat 才点得到搜索框
                    try:
                        _掐("摆正窗口(抢前台)",
                            lambda: 带超时(lambda: _ensure_wechat_ready(前台=True), 超时=8))
                    except Exception as e:
                        log("抢前台超时/失败（继续试）：", repr(e))
                    gui = get_gui()
                    _引擎 = _掐("取 UIA 引擎(切会话)", _取引擎)
                    try:
                        _掐("打开会话", lambda: gui.open_chat(chat))
                    except Exception as e:
                        log("打开会话失败（继续试）：", repr(e))
                    # 打开会话「调用没抛异常」不等于「真的切过去了」：窗口白板时
                    # 它可能返回了但界面没动，于是后面的贴字落到原会话上
                    # （2026-11-16 00:43 那条就发进了群）。切完必须核对一次。
                    切后 = _读当前会话(_引擎)
                    if 切后 != chat:
                        if 切后 is None:
                            # 微信这一版的 UIA 树经常是空的（只露出一个渲染子窗），
                            # 于是「读当前会话」永远读不到。这时候不能盲贴 —— 贴到哪个
                            # 会话就是哪个会话（2026-11-16 00:43 那条就发进了群）。
                            # 改走「Ctrl+F 搜名字」那条通道：它按名字选会话，不依赖读。
                            log("读不到当前会话，改用标题 OCR 核一次：%s" % text[:20])
                            try:
                                对上了 = _掐("核对会话标题(OCR)",
                                            lambda: get_gui()._chat_is_open(chat))
                            except Exception as _e:
                                对上了 = False
                                log("核对会话标题失败：", repr(_e))
                            if 对上了:
                                log("标题 OCR 确认就是「%s」，照常发" % chat)
                            else:
                                log("⚠ 读不到当前会话、标题也对不上，这条不发")
                                return {"ok": False, "说": "会话没切过去，这条没发"}
                        # 不再重试第二次：open_chat 失败时一次能白等 80 秒
                        # （2026-11-16 08:03 实测 79.4 秒），重试等于把这 80 秒
                        # 乘二，窗口还一直亮着。核对不过就不发，交给后面按名字搜。
                        log(
                            "⚠ 会话切不过去（当前「%s」/目标「%s」），这条不发，"
                            "免得发错地方" % (切后, chat)
                        )
                        return {"ok": False, "说": "会话没切过去，这条没发"}
                    # 切完会话，右侧那块渲染子窗常常会先白一下（消息和输入框都还没画）。
                    # 这时候去撞像素探针只会白等十几秒，主人眼里就是「窗口弹出来不动」，
                    # 所以先等画面真的画出来，再往下发。等不到也照常发（后面有白板兜底）。
                    try:
                        _h2 = STATE.get("wx_hwnd") or _微信窗口()
                        if _h2:
                            _掐("等画面画好(切会话后)", lambda: _等画面画好(_h2, 超时=2.5))
                    except Exception:
                        pass
                    # 切会话这一下微信会把自己拉起来（Invoke 会话项时应用自己的行为），
                    # 这时候如果立刻按回最小化，新会话的输入框就丢焦点了，字会发不出去。
                    # 所以这里让它先露着，等这条发完在 finally 里统一收回去。
                    # 窗口最小化时输入框可能丢焦点，这里顺手把焦点还给控件。
                    try:
                        if _引擎 is not None:
                            e = _引擎._chat_input()
                            if e is not None:
                                e.SetFocus()
                    except Exception as e:
                        log("还焦点给输入框失败：", repr(e))
                发了 = False

                if text:
                    # 长内容拆成几条短消息发（一个大气泡读着累）—— 每段不超过 60 字
                    for _第几段, 一 in enumerate(拆条(text)):
                        if _第几段:
                            # 两条气泡之间也留点间隔，别「啪」一下两条同时到
                            time.sleep(random.uniform(0.9, 2.0))
                        # 重试前先看库里：这句话刚发过就不再点第二遍
                        if _段已发过(一):
                            log("这句我发过（内存记录），跳过：%s" % 一[:30])
                            continue
                        if _already_sent(一, chat, within=25):
                            log("这句刚发过，跳过：%s" % 一[:30])
                            _记发过(一)
                            recently_sent.append((clean_text(一), time.time()))
                            continue
                        # verify=False：wechatauto 的回读确认按 3.x 约定认 sender_id==2，
                        # 微信 4.1 里自己发的是 1，永远确认不了 —— 每条白等 8 秒再补回车。
                        # 现在发完自己查库确认（下面 _等落库），失败才走补发分支。
                        白板 = False
                        try:
                            _h = STATE.get("wx_hwnd") or _微信窗口()
                            白板 = bool(_h) and _面板是白的(_h)
                        except Exception:
                            白板 = False
                        if 白板:
                            # 明知是白板就别去撞像素探针那 12 秒超时了
                            log("右侧会话区是白板，直接走粘贴通道：%s" % 一[:20])
                            ok = _粘贴并回车(一, chat)
                        else:
                            ok = _掐("发文字", lambda: 试(lambda g, t=一: g.send_msg(t, chat, False), "发文字", 超时=12))
                            if not ok:
                                # 发到一半变白板也一样：走「粘贴 + 回车」那条不依赖画面的通道
                                _h = STATE.get("wx_hwnd") or _微信窗口()
                                if _h and _面板是白的(_h):
                                    log("右侧会话区是白板，改走粘贴兜底：%s" % 一[:20])
                                    ok = _粘贴并回车(一, chat)
                                    if ok:
                                        log("白板兜底发出去了：%s" % 一[:26])
                        # 粘完马上收剪贴板：别等下面的落库确认，那一步慢起来十几秒
                        _剪贴板收尾()
                        库里有 = False
                        if ok:
                            if _等落库(一, chat):
                                库里有 = True
                                log("✅ 库里确认已发出：%s" % 一[:26])
                            else:
                                log("已发出（微信写库慢，库里暂时没查到；不重发）：%s" % 一[:26])
                        if not ok and _already_sent(一, chat, within=25):
                            log("接口说失败，但库里查到了 —— 算发成功：%s" % 一[:30])
                            ok = True
                        if not ok:
                            # 最后再补一脚：字常常只是卡在输入框里没回车出去
                            if _kick_enter():
                                time.sleep(1.2)
                                if _already_sent(一, chat, within=25):
                                    log("补回车后库里查到了 —— 算发成功：%s" % 一[:30])
                                    ok = True
                        if not ok:
                            # 慢路径上字可能刚刚才进库：再走键盘通道就会把同一句发第二遍
                            # （2026-11-16 07:51:00 那条重复就是这么来的）
                            if _already_sent(一, chat, within=180):
                                log("库里已经有这句了（刚才只是慢），不再补发：%s" % 一[:26])
                                ok = True
                        if not ok:
                            # UIA 彻底没发出去：先走键盘通道（Ctrl+F 搜会话→粘贴→回车→查库）
                            if _keyboard_send(一, chat):
                                log("键盘通道补发成功：%s" % 一[:30])
                                ok = True
                        if not ok:
                            # 最后再抢一次前台重试
                            try:
                                _ensure_wechat_ready(前台=True)
                                time.sleep(0.5)
                                gui.open_chat(chat)
                                if 试(lambda g, t=一: g.send_msg(t, chat, False), "发文字（前台重试）"):
                                    ok = True
                            except Exception as e:
                                log("前台重试异常：", repr(e))
                        if not ok:
                            gui_box["gui"] = None      # 下次重建
                            return {"ok": False, "说": "文字没发出去（微信窗口没接住）"}
                        # 以前这里是「发成功也补回车」，而且一次按两下 + 一下 Ctrl+Enter。
                        # 多按的 Enter 会把主人自己正在打的草稿顶出去；慢路径上还会让
                        # 同一条进两次（2026-11-16 07:50:19 / 07:51:00 那条「一点睡还去
                        # 早自习」就进了两遍）。现在只有在库里确实没查到这条时才补一下。
                        if not 库里有 and _微信在前台():
                            _press_enter(1)
                        # 库里确认过了就别再问 UIA「框里还有字吗」——
                        # 这几问在微信半死时一次要十几秒，白拖住窗口（假卡死的帮凶）
                        if not 库里有:
                            _kick_enter()
                        # 拆开发出去的每一条都记账 —— 不然文件传输助手里会被当成主人说的话读回来
                        recently_sent.append((clean_text(一), time.time()))
                        _记发过(一)
                        time.sleep(0.35)
                    recently_sent.append((clean_text(text), time.time()))
                    _记发过(text)
                    STATE["last_send"] = time.time()
                    # 记住这条是发给谁的：下一条若是同一个会话，就不用再切一次
                    # （切会话要 open_chat，UIA 树空的时候能慢到 80 秒，窗口一直亮着）
                    try:
                        STATE["最后会话"] = chat
                    except Exception:
                        pass
                    if len(recently_sent) > 100:
                        del recently_sent[:-100]
                    log("✅ 已发到微信（%s）：%s" % (chat, text[:40]))
                    发了 = True

                临时图 = []

                def 取图(p):
                    q = _localize_image(p)
                    if q and q != p:
                        临时图.append(q)
                    return q

                for p in images:
                    p = 取图(p)
                    if not p:
                        log("⚠ 图拿不到，跳过")
                        continue
                    # 发完文字窗口可能被挡/失焦 —— 发图前把会话和窗口都摆正。
                    # 但刚才要是已经发过文字，窗口和会话都还在这条上，再走全套恢复会白顿一秒多。
                    try:
                        if text:
                            # 发完文字窗口可能又被 DSH 盖住 —— 而 wechatauto 定位输入框是
                            # **像素探针**（扫白底+分界线），窗口被挡时会连扫 6 轮、白等 20 秒。
                            # 所以发图前必须先把微信顶到前台（约 1 秒），再让它探。
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.25)
                        else:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.2)
                            gui.open_chat(chat)
                            time.sleep(0.4)
                    except Exception as e:
                        log("⚠ 发图前准备窗口失败（继续试）：", repr(e))
                    t0 = time.time()
                    前图时 = _last_image_ts(chat)
                    try:
                        ok_img = _send_image_dib(gui, p, chat)
                    except Exception as e:
                        log("✗ 发图异常：", repr(e))
                        ok_img = False
                    # 确认放松（2026-11-14 用户要求）：ok_img=True 就是「已粘贴 + 已回车」，
                    # 不再等 14 轮库确认、也不再重建窗口重试 —— 那套让图比字晚到近半分钟。
                    # 库只快速瞄两眼，纯粹为了日志好看；**确认不到也绝不重发**
                    # （4.1.15.56 的库确认常误判，重试就是重复刷屏，21:05 连发三张的根因）。
                    真发了 = False
                    if ok_img:
                        for 第 in range(2):
                            if _last_image_ts(chat) > 前图时 + 0.5:
                                真发了 = True
                                break
                            time.sleep(0.5 if 第 == 0 else 1.0)
                        if 真发了:
                            log("🖼 已发图（%s）：%s" % (chat, os.path.basename(p)))
                        else:
                            log("🖼 已发图（%s，粘贴+回车已做，库还没确认；不重发）：%s" % (chat, os.path.basename(p)))
                        发了 = True
                        time.sleep(0.3)
                    else:
                        # 只有「连粘贴都没成功」才值得重试：短查几眼，没新图就重建窗口再来一次
                        for 第 in range(3):
                            if _last_image_ts(chat) > 前图时 + 0.5:
                                真发了 = True
                                break
                            time.sleep(0.6)
                        if not 真发了:
                            log("✗ 发图没粘进去，重建窗口重试一次：%s" % p)
                            gui_box["gui"] = None
                            重试ok = False
                            try:
                                _ensure_wechat_ready(前台=True)
                                time.sleep(0.5)
                                gui2 = get_gui()
                                gui2.open_chat(chat)
                                time.sleep(0.5)
                                前图时2 = _last_image_ts(chat)
                                重试ok = _send_image_dib(gui2, p, chat)
                                for 第 in range(4):
                                    if _last_image_ts(chat) > 前图时2 + 0.5:
                                        真发了 = True
                                        break
                                    time.sleep(0.7)
                            except Exception as e:
                                log("重试发图异常：", repr(e))
                            if 真发了:
                                log("🖼 重试后已发图（%s）：%s" % (chat, os.path.basename(p)))
                                发了 = True
                                time.sleep(0.3)
                            elif 重试ok:
                                log("🖼 重试后已粘贴+回车（%s，库没确认；不重发）：%s" % (chat, os.path.basename(p)))
                                发了 = True
                            else:
                                log("✗ 发图彻底失败（没粘进去，库里也没有新图）：%s" % p)
                    # 图也是走剪贴板粘的，粘完就收，理由同文字那一段
                    _剪贴板收尾()

                for t in 临时图:
                    try:
                        os.unlink(t)
                    except Exception:
                        pass

                if not 发了:
                    return {"ok": False, "说": "字和图都没发出去"}
                return {"ok": True}
            except Exception as e:
                log("✗ 发送失败（%s）：%r" % (chat, e))
                gui_box["gui"] = None    # 下次重建
                # 微信不在线（退出/关窗）就只汇报，不自动拉起、不自动登录 —— 主人定的规矩。
                if "未找到微信主窗口" in str(e):
                    _报微信掉线("发送时微信窗口不在")
                return {"ok": False, "说": repr(e)}
            finally:
                # 收尾这几步各自兜异常：任何一步抛出去，后面几步就被跳过了
                try:
                    # 发送期间把微信置顶了，发完要放下来，不然它一直压在别的窗口上面
                    _取消置顶()
                except Exception as e:
                    log("收尾：取消置顶出错（继续）：", repr(e))
                # 收尾第二件：剪贴板里还留着刚粘进去的字/图。
                # 只在我们自己粘的那份还躺在里面时动手：原来有内容就还原回去，原来是空的就清空。
                # 主人自己新复制的东西（跟这几条不一样）一律不动。
                # 正常发送流程里每粘完一条就已经收过一次了，这里只兜底（半路抛异常才轮到它）。
                # 2026-11-14 加：收过就别再跑第二遍 —— 这一遍要等落库删完，晚十几秒，
                # 那会儿剪贴板里可能已经是主人新弄的内容，多这一遍只会多一次误清的机会。
                if not 收尾次数[0]:
                    _剪贴板收尾()

    # ---------- 兜底：DSH 那边的会话事件没送到时，直接读会话文件把回复捞出来 ----------
    # 起因：2026-10-27 晚，DSH 重启后 host.js 的 session/event 不再触发，
    #       回复全部卡在 DSH 里发不出去。这里作为独立于事件的一层保险。

    LAST_CHAT = {"chat": os.environ.get('DSH_BRIDGE_DEFAULT_CHAT') or '', "ts": 0.0}
    FALLBACK_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fallback_state.json')
    # 会话目录按「最近改动过的那个」自动找，不写死具体会话名
    def _找会话根() -> str:
        base = os.path.join(os.path.expanduser('~'), '.dsh', 'sessions')
        try:
            dirs = [os.path.join(base, d) for d in os.listdir(base)]
            dirs = [d for d in dirs if os.path.isdir(d)]
            if dirs:
                return max(dirs, key=lambda p: os.path.getmtime(p))
        except Exception:
            pass
        return base

    SESS_ROOT = _找会话根()
    # 兜底通道：2026-10-28 起默认关闭。它读会话文件猜「这句回复该发给谁」，
    # 猜错就会把私聊内容发进群、或者把同一句重发两遍（当天出了两次事故）。
    # 想要它回来：设环境变量 DSH_BRIDGE_NO_FALLBACK=0。
    FALLBACK_OFF = os.environ.get('DSH_BRIDGE_NO_FALLBACK', '1') != '0'

    def _读兜底游标():
        try:
            with open(FALLBACK_STATE, 'r', encoding='utf-8') as f:
                return int((json.load(f) or {}).get('seq') or 0)
        except Exception:
            return 0

    def _写兜底游标(n):
        try:
            with open(FALLBACK_STATE, 'w', encoding='utf-8') as f:
                json.dump({'seq': int(n)}, f)
        except Exception:
            pass

    def _最新会话文件():
        best, best_t = None, 0.0
        try:
            for d in os.listdir(SESS_ROOT):
                p = os.path.join(SESS_ROOT, d, 'session.v4.jsonl.zstd')
                if os.path.exists(p):
                    t = os.path.getmtime(p)
                    if t >= best_t:
                        best_t, best = t, p
        except Exception:
            pass
        return best

    _FB_CACHE = {'key': None, 'res': None}

    def 兜底捞回复():
        p = _最新会话文件()
        if not p:
            return None
        try:
            mt = os.path.getmtime(p)
        except Exception:
            return None
        key = (p, mt, _读兜底游标())
        if _FB_CACHE['key'] == key:
            return _FB_CACHE['res']
        res = _解析兜底(p)
        _FB_CACHE['key'] = key
        _FB_CACHE['res'] = res
        return res

    def _解析兜底(p):
        """找最后一条「来自微信」的用户消息之后的那句回复。本地聊的不算。"""
        try:
            import zstandard
        except Exception:
            return None
        try:
            with open(p, 'rb') as f:
                raw = zstandard.ZstdDecompressor().stream_reader(f).read()
        except Exception:
            return None
        last_user = None
        last_asst = None
        for line in raw.decode('utf-8', 'replace').splitlines():
            if len(line) < 24 or '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get('type')
            if t == 'user/message':
                src = (o.get('data') or {}).get('source') or {}
                k = src.get('kind')
                if k == 'wechat':
                    last_user = o
                    last_asst = None          # 只看这条微信消息之后的回复
                elif k == 'user':
                    last_user = None          # 有人在本机聊天，那一轮的风头过了
                    last_asst = None
            elif t == 'assistant/message' and last_user is not None:
                d = o.get('data') or {}
                body = (d.get('message') or {}).get('content') or []
                txt = ''.join(x.get('text') or '' for x in body if x.get('type') == 'text').strip()
                if txt:
                    last_asst = (o.get('seq') or 0, txt)
        if not last_user or not last_asst:
            return None
        seq_u = last_user.get('seq') or 0
        seq_a, txt_a = last_asst
        if seq_a <= seq_u or seq_a <= _读兜底游标():
            return None
        return {'seq': seq_a, 'text': txt_a}

    def _桌面能用():
        """屏幕此刻能不能用（合盖待机 / 锁屏 / 安全桌面时不能）。

        2026-11-17 10:41 实测：Modern Standby 期间 OpenClipboard 拒绝访问、
        截图 screen grab failed，三条发送通道全废，消息全丢。待机时干脆
        别去取队列 —— 消息留在 DSH 那边，桌面回来再发，一条都不会少。
        """
        try:
            import ctypes

            u = ctypes.windll.user32
            h = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_READOBJECTS
            if not h:
                return False, "拿不到输入桌面（待机或锁屏）"
            u.CloseDesktop(h)
            if not u.GetForegroundWindow():
                return False, "前台窗口是空的（桌面不可用）"
            if not u.OpenClipboard(0):
                return False, "剪贴板打不开（多半在待机/锁屏）"
            u.CloseClipboard()
            return True, ""
        except Exception:
            # 探测本身出错就别拦正常发送
            return True, ""

    def outbox_loop():
        log("出站轮询已启动（每 %.0f 秒）" % POLL_SEC)

        # 预热：第一次 get_gui()/取 UIA 引擎会把微信窗口短暂恢复一下（要建控件树），
        # 放在这里提前建好，免得真发消息时才闪那一下。
        def _预热():
            # 窗口不可用（微信没开、或者整个收在托盘里）时不预热：这时 WeChatGUI()
            # 会抛「未找到微信主窗口」，预热本来就不可能成功；以前还会顺手把 UIA 记一次
            # 失败，启动时窗口恰好藏着就可能熔断 5 分钟。
            if _微信窗口() is None:
                log("UIA 引擎预热跳过（微信窗口此刻不可用：没开，或者收在托盘里）")
                return
            try:
                with send_lock:
                    引擎 = _取引擎()
                if 引擎 is None:
                    log("UIA 引擎预热没成功（真发的时候再取一次）")
                else:
                    log("UIA 引擎预热完成")
            except Exception as e:
                log("UIA 预热失败：", repr(e))

        threading.Thread(target=_预热, daemon=True).start()
        卡住 = {"次": 0}   # 连着几条发送超时；成功一条清零
        重排 = []          # 发失败的条目留在这里，下一轮再试（DSH 那边取走就删了）
        待机提示 = {"t": 0.0}

        # —— 待发清单落盘 ——
        # DSH 的 outbox 是「取走就删」，我们一旦取回来，责任就落到自己身上：
        # 进程被杀（重启桥接、崩了）、或者发到一半断电，这条就永久消失了。
        # 2026-11-17 12:45 就这么丢过三条（我正在重启桥接做自测）。
        # 所以取回来先写进 pending.json，发成功/彻底放弃才划掉；启动时捡回来接着发，
        # 捡回来之前先用数据库确认「是不是其实已经发出去了」，避免同一条进两遍。
        待发文件 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending.json")
        待发锁 = threading.Lock()
        待发表 = {}          # _键 -> 还没发完的条目
        键序 = [0]

        def _落盘待发():
            try:
                with 待发锁:
                    列 = list(待发表.values())
                    临时 = 待发文件 + ".tmp"
                    with open(临时, "w", encoding="utf-8") as f:
                        json.dump(列, f, ensure_ascii=False)
                    os.replace(临时, 待发文件)
            except Exception as e:
                log("存待发清单失败（不影响发送）：", repr(e))

        def _记待发(item):
            if not item.get("_键"):
                键序[0] += 1
                item["_键"] = "%d-%d" % (int(time.time()), 键序[0])
            待发表[item["_键"]] = item
            _落盘待发()

        def _划掉待发(item):
            待发表.pop(item.get("_键"), None)
            _落盘待发()

        def _捡回待发():
            捡 = []
            try:
                with open(待发文件, encoding="utf-8") as f:
                    列 = json.load(f)
            except FileNotFoundError:
                return 捡
            except Exception as e:
                log("读待发清单失败（当作空的）：", repr(e))
                return 捡
            for 条 in (列 if isinstance(列, list) else []):
                if not isinstance(条, dict):
                    continue
                条["_下次"] = 0.0
                try:
                    if _already_sent(str(条.get("text") or ""), 条.get("chat"), within=1800):
                        log("上次没发完的这条其实已经发出去了，不重发：%s" % str(条.get("text"))[:24])
                        continue
                except Exception:
                    pass
                捡.append(条)
                if 条.get("_键"):
                    待发表[条["_键"]] = 条
            return 捡

        捡回来 = _捡回待发()
        if 捡回来:
            重排.extend(捡回来)
            log("上次没发完的 %d 条从 pending.json 捡回来了，接着发" % len(捡回来))
        while True:
            time.sleep(POLL_SEC)
            if not SEND_ENABLED:
                continue
            能用, 原因 = _桌面能用()
            if not 能用:
                # 待机/锁屏时不取队列：取了就是我们的责任，发不出去只能丢。
                # 留在 DSH 的 outbox 里最安全（它最多存 50 条）。
                if time.time() - 待机提示["t"] > 600:
                    待机提示["t"] = time.time()
                    log("屏幕不可用（%s），消息先留在队列里，等它回来再发" % 原因)
                continue
            try:
                r = http_json("/dsh-wechat-bridge/outbox")
                if not r.get("ok"):
                    continue
                这一批发过了 = False
                现在 = time.time()
                轮到 = [条 for 条 in 重排 if 条.get("_下次", 0.0) <= 现在][:3]
                if 轮到:
                    重排 = [条 for 条 in 重排 if not any(条 is 选 for 选 in 轮到)]
                新来 = [条 for 条 in (r.get("消息") or []) if isinstance(条, dict)]
                for 条 in 新来:
                    _记待发(条)      # 先落盘再发：这条从此刻起丢了也能重发
                待发 = 轮到 + 新来
                for item in 待发:
                    # ⭐ 发送放进子线程带超时：微信窗口被最小化/被抢焦点时，UIA 有可能一直等下去，
                    #    以前就这么把整条出站循环堵死了 —— 后面的消息全卡在这儿不发。
                    结果盒 = {}

                    def 干(条=item):
                        try:
                            结果盒["r"] = send_one(条)
                        except Exception as e:  # noqa: BLE001
                            结果盒["e"] = e

                    线 = threading.Thread(target=干, daemon=True)
                    线.start()
                    线.join(timeout=70)
                    if 线.is_alive():
                        # 超时 ≠ 掉线。实测慢发常常在 70–130 秒之间自己就干完了
                        # （2026-11-14 23:40:56 打了「发送超时」，23:41:00 又 ✅ 了，
                        #  结果掉线状态和一条汇报都是假的）。所以先宽限 60 秒。
                        log("⚠ 发送超过 70 秒还没回来，再等 60 秒（先不报掉线）")
                        线.join(timeout=60)
                    if 线.is_alive():
                        log("⚠ 发送超时（130 秒没回来），这条先跳过，重建窗口")
                        # 光看超时只能猜它在等谁，把卡住那个线程的栈打出来
                        try:
                            import traceback as _tb
                            帧 = sys._current_frames().get(线.ident)
                            if 帧 is not None:
                                栈 = "".join(_tb.format_stack(帧)).replace("\n", " ⏎ ")
                                log("卡住的栈：" + 栈[:900])
                        except Exception as e:
                            log("打栈失败：", repr(e))
                        gui_box["gui"] = None
                        res = {"ok": False, "说": "发送超时（窗口卡住）", "_别重试": True}
                        # 一条慢不代表窗口死了：连两条都超时才打扰主人。
                        卡住["次"] += 1
                        log("连着第 %d 条发送超时" % 卡住["次"])
                        if 卡住["次"] >= 2:
                            _报微信掉线("连着 %d 条发送都超时（窗口多半卡住了），请手动看下微信" % 卡住["次"])
                    elif "r" in 结果盒:
                        res = 结果盒["r"]
                    else:
                        res = {"ok": False, "说": str(结果盒.get("e"))}
                    http_json("/dsh-wechat-bridge/outbox/ack", {
                        "ok": bool(res.get("ok")),
                        "chat": item.get("chat"),
                        "说": res.get("说", ""),
                    })
                    if res.get("ok"):
                        这一批发过了 = True
                        卡住["次"] = 0
                        _划掉待发(item)
                    elif not res.get("_别重试") and int(item.get("_重试", 0)) < 3:
                        # DSH 那边的队列一取就删，失败必须我们自己接着重试，
                        # 否则这条就永远没了（2026-11-17 10:41 丢两条就是这么来的）。
                        item["_重试"] = int(item.get("_重试", 0)) + 1
                        item["_下次"] = time.time() + 5 * item["_重试"]
                        重排.append(item)
                        log("这条没发成，%d 秒后再试（第 %d 次）：%s"
                            % (5 * item["_重试"], item["_重试"], str(item.get("text"))[:24]))
                    else:
                        # 不重试了（超时怕重复，或者已经试满 3 次）—— 这条就认了，别再挂着
                        _划掉待发(item)
                        log("这条不重试了，从待发清单里划掉：%s" % str(item.get("text"))[:24])
                # 主通道真发出去的那条，把兜底游标一起推过去 —— 否则库里的时间窗一过，
                # 兜底会把同一句再捞出来重复发一遍（就是「发图就重复」的老毛病）
                if 这一批发过了 and (r.get("消息") or []):
                    try:
                        捞2 = 兜底捞回复()
                        if 捞2:
                            待比2 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞2["text"]).strip()
                            if 待比2 and _already_sent(待比2, LAST_CHAT.get("chat") or "", within=180):
                                _写兜底游标(捞2["seq"])
                                log("兜底游标跟主通道同步：seq=%s" % 捞2["seq"])
                    except Exception:
                        pass
                if not FALLBACK_OFF and not r.get("消息"):
                    try:
                        捞 = 兜底捞回复()
                    except Exception:
                        捞 = None
                    if 捞:
                        聊天 = LAST_CHAT.get("chat") or ""
                        # ⭐ 串台防护：只有「刚刚」来过微信消息，兜底才认这句回复是给它的。
                        # 之前是永远拿 LAST_CHAT，结果主人在 Web 里跟我聊、我回的私聊内容
                        # 被兜底当成「对群里那条老消息的回复」，整段发进了群（2026-10-28 16:19）。
                        过期 = (time.time() - float(LAST_CHAT.get("ts") or 0)) > 90
                        if 聊天 and 过期:
                            log("兜底：最后一条微信消息在 90 秒前，这句回复不归它，跳过（防串台）")
                            聊天 = ""
                        if 聊天:
                            log("兜底：会话事件没送到，直接捞到一句回复（seq=%s）" % 捞["seq"])
                            # 兜底只补「主通道真没送到」的：已经发过的绝不能再发一遍（会重复两遍文字+图）
                            待比 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞["text"]).strip()
                            已发过 = False
                            try:
                                if 待比:
                                    已发过 = _already_sent(待比, 聊天, within=120)
                                else:
                                    已发过 = _image_sent_recently(聊天, time.time() - 120)
                            except Exception:
                                已发过 = False
                            if 已发过:
                                log("兜底：这句微信里已经发过了，不重复发，只推进游标")
                                _写兜底游标(捞["seq"])
                            else:
                                兜底结果 = {"ok": False, "说": "未发"}
                                try:
                                    兜底结果 = send_one({"chat": 聊天, "text": 捞["text"], "images": []})
                                except Exception as e:  # noqa: BLE001
                                    log("兜底发送失败：%r" % (e,))
                                if 兜底结果.get("ok"):
                                    _写兜底游标(捞["seq"])
                                    time.sleep(0.5)
                # 窗口状态不再由这里收：2026-11-23 起只在前台发送，不藏窗、不还原
            except Exception:
                log("出站轮询出错：", traceback.format_exc(limit=2))

    # ---------- 收：监听数据库新消息 ----------

    START_TS = time.time()
    seen = set()

    # ---------- 收：图片 / 表情包落盘 ----------

    媒体 = {"下载器": None, "导入失败": False}

    def _图片后缀(data):
        if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        return ""

    def _保存表情包(raw):
        """表情包 = 一条 <emoji cdnurl="...">。cdnurl 是明文，直接下，不用解密。"""
        try:
            text = html.unescape(raw or "")
            # 微信 4.x 的 XML 属性会带空格：cdnurl = "..." / md5 = "..."
            url = ""
            for attr in ("cdnurl", "thumburl"):
                m = re.search(r'%s\s*=\s*["\']([^"\']+)["\']' % attr, text)
                if m and m.group(1).strip().startswith(("http://", "https://")):
                    url = m.group(1).strip()
                    break
            if not url:
                return None
            md5 = re.search(r'md5\s*=\s*["\']([^"\']+)["\']', text)
            key = (md5.group(1) if md5 else hashlib.md5(url.encode("utf-8")).hexdigest()).lower()
            folder = os.path.join(INBOUND_DIR, "stickers")
            os.makedirs(folder, exist_ok=True)
            for ext in ("gif", "png", "jpg", "webp"):
                p = os.path.join(folder, key + "." + ext)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    return p
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(8 * 1024 * 1024 + 1)
            if not data or len(data) > 8 * 1024 * 1024:
                return None
            ext = _图片后缀(data)
            if not ext:
                return None
            p = os.path.join(folder, key + "." + ext)
            with open(p, "wb") as f:
                f.write(data)
            log("表情包落盘：%s（%d KB）" % (p, len(data) // 1024))
            return p
        except Exception as e:
            log("表情包落盘失败：", repr(e))
            return None

    def _保存图片(msg, username):
        """普通图片：本地 .dat 解密。wechatauto 的 MediaDownloader 会处理 wxgf。"""
        if 媒体["导入失败"]:
            return None
        try:
            if 媒体["下载器"] is None:
                from wechatauto.media import MediaDownloader

                媒体["下载器"] = MediaDownloader(db)
            folder = os.path.join(INBOUND_DIR, "images")
            os.makedirs(folder, exist_ok=True)
            local_id = int(msg.get("local_id") or 0)
            if not local_id:
                return None
            p = 媒体["下载器"].download_image(
                username, local_id, save_dir=folder, tier="best", shard=msg.get("shard")
            )
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                log("图片落盘：%s" % p)
                return p
            return None
        except Exception as e:
            log("图片落盘失败：", repr(e))
            return None

    # ---------- 看图：老宿主自己看，新宿主交给它看 ----------

    def _视觉配置():
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        配置 = {}
        try:
            with open(os.path.join(home, "wechat-bridge-config.json"), encoding="utf-8-sig") as f:
                配置 = json.load(f) or {}
        except Exception:
            pass
        if 配置.get("看图") is False:
            return None
        主 = str(配置.get("视觉密钥") or "").strip()
        备 = str(配置.get("视觉备用密钥") or "").strip()
        if not 主 or not 备:
            try:
                # 可选的第三方设置文件，用环境变量指过来
                第三方设置 = os.environ.get("DSH_BRIDGE_VISION_SETTINGS") or os.path.join(home, "wechat-vision-settings.json")
                with open(第三方设置, encoding="utf-8-sig") as f:
                    鲸 = json.load(f) or {}
                if not 主:
                    主 = str(鲸.get("apiKey") or "").strip()
                if not 备:
                    备 = str(鲸.get("visionApiKey") or "").strip()
            except Exception:
                pass
        if not 主:
            主 = str(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("VISION_API_KEY") or "").strip()
        if not 备:
            备 = str(os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY") or "").strip()
        if not 主 and not 备:
            return None
        return {
            "主": 主,
            "备": 备,
            "模型": str(配置.get("视觉模型") or "deepseek-flash").strip() or "deepseek-flash",
            "地址": str(配置.get("视觉地址") or "https://api.deepseek.com").rstrip("/"),
            "备用模型": str(配置.get("视觉备用模型") or "glm-4v-flash").strip() or "glm-4v-flash",
            "备用地址": str(
                配置.get("视觉备用地址") or "https://open.bigmodel.cn/api/paas/v4"
            ).rstrip("/"),
        }

    def _读视觉缓存():
        try:
            with open(VISION_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _写视觉缓存(表):
        try:
            items = sorted(表.items(), key=lambda kv: float((kv[1] or {}).get("时间") or 0), reverse=True)
            os.makedirs(os.path.dirname(VISION_CACHE), exist_ok=True)
            with open(VISION_CACHE, "w", encoding="utf-8") as f:
                json.dump(dict(items[:500]), f, ensure_ascii=False)
        except Exception:
            pass

    def _视觉请求(地址, key, 模型, data_url, 提示):
        url = 地址.rstrip("/") + "/chat/completions"
        体 = {
            "model": 模型,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": 提示},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
            "max_tokens": 700,
        }
        if "deepseek" in 地址:
            体["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            url,
            data=json.dumps(体, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError("视觉接口 HTTP %s：%s" % (e.code, detail))
        msg = ((payload.get("choices") or [{}])[0].get("message") or {})
        text = str(msg.get("content") or "").strip()
        if not text:
            raise RuntimeError("视觉接口没返回文字")
        return re.sub(r"\s+", " ", text)[:180]

    def _看图(path, 类型):
        """返回一句中文描述；没配钥匙 / 看失败就返回空字符串。"""
        设置 = _视觉配置()
        if not 设置:
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read(20 * 1024 * 1024 + 1)
        except Exception:
            return ""
        if not data or len(data) > 20 * 1024 * 1024:
            return ""
        suffix = _图片后缀(data)
        mime = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(suffix)
        if not mime:
            return ""
        digest = hashlib.sha256(data).hexdigest()
        缓存 = _读视觉缓存()
        if isinstance(缓存.get(digest), dict) and 缓存[digest].get("描述"):
            return str(缓存[digest]["描述"])
        提示 = (
            "这是微信聊天里对方发来的%s。"
            "用一两句中文说清画面内容，以及它表达的情绪、语气或梗；不要寒暄，不超过60字。" % 类型
        )
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        描述 = ""
        for key, 模型, 地址 in (
            (设置["主"], 设置["模型"], 设置["地址"]),
            (设置["备"], 设置["备用模型"], 设置["备用地址"]),
        ):
            if not key:
                continue
            try:
                描述 = _视觉请求(地址, key, 模型, data_url, 提示)
                if 描述:
                    break
            except Exception as e:
                log("看图失败（%s）：%r" % (模型, e))
        if not 描述:
            return ""
        缓存[digest] = {"描述": 描述, "时间": time.time(), "类型": 类型}
        _写视觉缓存(缓存)
        return 描述

    def _看图并替换(content, path, 类型):
        描述 = _看图(path, 类型)
        if not 描述:
            return content
        标签 = "一个表情包" if 类型 == "表情包" else "一张图片"
        前缀 = "[对方发来%s：%s]" % (标签, 描述)
        new, n = re.subn(r"（发来(?:一)?(?:张|个)?(?:图片|表情包)）", lambda _m: 前缀, content, count=1)
        return new if n else (前缀 + "\n" + content)

    def on_message(msg, listener):
        try:
            username = msg.get("username") or ""
            if not username:
                return
            mtype = msg.get("type") or ""
            raw = msg.get("content") or ""
            图片待下 = ""
            图片类型 = ""
            if mtype == "图片":
                content = "（发来一张图片）"
                图片类型 = "图片"
                图片待下 = "图片"
            elif mtype == "动画表情":
                content = "（发来一个表情包）"
                图片类型 = "表情包"
                图片待下 = "表情包"
            elif mtype in ("文本", "文件/链接/卡片"):
                content = clean_text(raw)
                if mtype == "文件/链接/卡片":
                    # 卡片 / 引用消息：正文在 <title>，被引用的那句在 <refermsg><content>
                    mt = re.search(r"<title>(.*?)</title>", raw, re.S)
                    body = clean_text(html.unescape(mt.group(1))) if mt else ""
                    mr = re.search(r"<refermsg>.*?<content>(.*?)</content>", raw, re.S)
                    if mr:
                        quoted = clean_text(html.unescape(mr.group(1)))
                        content = (f"（引用：{quoted}）{body}" if quoted else body).strip()
                    else:
                        content = body
            else:
                return
            if not content or content.startswith("<"):
                return
            sender = msg.get("sender_username") or ""
            if me and sender == me:
                # 自己发的：绝大多数是主人在电脑/手机上打字，不该回环。
                # 例外是「文件传输助手」—— 那是他给自己留的入口，可以当对话窗用；
                # 但我们自己刚发出去的那几条要跳过，否则会自问自答。
                if str(username) != "filehelper":
                    return
                now = time.time()
                # 兜底：刚往外发完的这十几秒，文件传输助手里冒出来的都不当输入
                if now - STATE["last_send"] < 12:
                    return
                if any(content == t and now - ts < 120 for (t, ts) in recently_sent):
                    return
            create_time = float(msg.get("create_time") or 0)
            if create_time and create_time < START_TS - SKIP_OLDER:
                return              # 启动之前的旧消息

            chat = display_name(username) or username
            if ONLY_CHATS and chat not in ONLY_CHATS:
                return

            key = "%s|%s|%s|%s" % (username, sender, create_time, (raw or content)[:60])
            if key in seen:
                return
            seen.add(key)
            if len(seen) > 2000:
                seen.clear()

            nickname = ""
            try:
                nickname = db.get_nickname(sender) or ""
            except Exception:
                pass
            is_group = str(username).endswith("@chatroom")

            # 屏蔽名单：微信运动之类，既不读也不回，日记也不记
            if is_blocked(chat, nickname, username):
                return

            # 群聊记日记 —— 不管点没点名都记，这是「群里的记忆」
            if is_group and CFG["日记"]:
                diary_append(chat, create_time, nickname or "某人", content)

            # 群里：点名 / 主人说话 / 活跃群 才进 DSH（不然每个群都在刷屏）
            at_me = False
            if is_group:
                at_me = any(a in raw for a in ALIASES)
                # 活跃群写群名、群 id（xxx@chatroom）都认 —— 改个群名不至于失效
                活跃 = CFG.get("活跃群") or []
                called = (
                    at_me
                    or is_master(sender, nickname)
                    or chat in 活跃
                    or username in 活跃
                )
                if not called:
                    return
                # 主人说话和点名都必进，只有普通群友的闲聊才吃频率闸
                if not at_me and not is_master(sender, nickname) and not 频率允许(chat):
                    return
                prefix = group_context(username, chat, CFG["群上下文条数"])
                hint = (
                    "（这条点名了你，必须回。）"
                    if at_me
                    else "（这条没点名你：想回就回；不想回就只回两个字 [不回]，别发别的。）"
                )
                content = (prefix or "") + hint + content

            # 图片 / 表情包：老宿主没有看图能力，就由桥接先看；新宿主交给它看
            图片路径 = []
            if 图片待下:
                p = _保存表情包(raw) if 图片待下 == "表情包" else _保存图片(msg, username)
                if p:
                    if HOST_VERSION >= (1, 2, 0):
                        图片路径 = [p]
                    else:
                        content = _看图并替换(content, p, 图片类型)

            LAST_CHAT["chat"] = chat
            LAST_CHAT["ts"] = time.time()
            载荷 = {
                "text": content,
                "chat": chat,
                # 会话的底层 id（好友 wxid / xxx@chatroom）：绑定和屏蔽填这个名字也认，
                # 改备注、同名都不怕对不上
                "chatId": username,
                "chatType": "group" if is_group else "private",
                "sender": nickname or ("群里的人" if is_group else "对方"),
                "senderId": sender,
                "atMe": True if not is_group else at_me,
                "图片": 图片路径,
                "图片类型": 图片类型,
            }
            r = http_json("/dsh-wechat-bridge/receive", 载荷)
            if r.get("ok"):
                log("→ DSH（at=%s，图=%d）：%s ｜ %s ｜ raw=%s" % (
                    at_me, len(图片路径), chat, content[:40], raw[:80]))
            elif _像断线(r.get("说")):
                # DSH 合盖睡了 / 宿主刚重载：这会儿端口是拒的，消息不能就这么丢
                with 待补送锁:
                    待补送.append([载荷, time.time() + 3600, r.get("说")])
                log("DSH 暂时连不上（%s），这条先存着，15 秒后补送：%s" % (r.get("说"), content[:30]))
            else:
                log("⚠ DSH 没收下：%s ｜ %s" % (r.get("说"), content[:40]))
        except Exception:
            log("处理消息出错：", traceback.format_exc(limit=2))

    if ARGS.test_image:
        描述 = _看图(ARGS.test_image, "表情包")
        with open(os.path.join(HERE, "vision-test.txt"), "w", encoding="utf-8") as f:
            f.write(描述 or "(empty)")
        log("看图测试：", 描述 or "(空)")
        return 0

    待补送 = []          # [[载荷, 截止时间戳, 失败原因]]，DSH 不在时先存着
    待补送锁 = threading.Lock()

    def _像断线(说):
        """连接层面的失败才值得等它回来；被屏蔽、参数错这种直接放弃。"""
        s = str(说 or "").lower()
        return ("10061" in s) or ("积极拒绝" in s) or ("refused" in s) or ("timed out" in s) \
            or ("连接" in s and "失败" in s) or s.startswith("http 5")

    listener = Listener(db, interval=0.4)
    listener.add_all(on_message)
    listener.start()
    log("监听已启动（0.4 秒一轮）")

    # DSH 不在时把消息存着，回来再补送（合盖休眠 / 宿主重载都只有几十秒到几分钟）
    def _补送循环():
        while True:
            time.sleep(15)
            现在 = time.time()
            with 待补送锁:
                等着的 = list(待补送)
            for 项 in 等着的:
                if 现在 > 项[1]:
                    with 待补送锁:
                        if 项 in 待补送:
                            待补送.remove(项)
                    log("补送超时（DSH 一直没回来），放弃：%s" % str(项[0].get("text") or "")[:30])
                    continue
                r2 = http_json("/dsh-wechat-bridge/receive", 项[0])
                if r2.get("ok"):
                    with 待补送锁:
                        if 项 in 待补送:
                            待补送.remove(项)
                    log("补送进去了（DSH 回来了）：%s" % str(项[0].get("text") or "")[:30])

    threading.Thread(target=_补送循环, daemon=True).start()

    load_memes()

    threading.Thread(target=outbox_loop, daemon=True).start()

    refresh_config()
    log("配置：群上下文 %d 条｜日记 %s｜主人 %s｜屏蔽 %d 个" % (
        CFG["群上下文条数"], "开" if CFG["日记"] else "关",
        "/".join(CFG["主人"]) or "（没设）", len(CFG["屏蔽"])))

    def config_loop():
        断着 = False
        何时 = 0.0
        while True:
            time.sleep(30)
            if refresh_config():
                if 断着:
                    断了多少 = time.time() - 何时
                    断着 = False
                    log("DSH 回来了（刚才断了 %.0f 秒）" % 断了多少)
                    # 断得够久就主动汇报一句（只叫醒人，不重启任何东西）。
                    # 断的时候 /notify 也发不出去，所以只能等它回来再补这一句。
                    if 断了多少 >= 60:
                        try:
                            http_json("/dsh-wechat-bridge/notify", {
                                "text": "DSH 刚才没应答 %.0f 秒（宿主卡住或重载），现在好了" % 断了多少,
                            })
                        except Exception:
                            pass
            elif not 断着:
                断着 = True
                何时 = time.time()
                log("⚠ DSH 没应答（合盖睡了 / 宿主卡住或重载），这段时间的消息会进补送队列")

    threading.Thread(target=config_loop, daemon=True).start()

    st = http_json("/dsh-wechat-bridge/state")
    if st.get("ok"):
        try:
            段 = [int(x) for x in str(st.get("版本") or "1.1.0").split(".")[:3]]
            while len(段) < 3:
                段.append(0)
            HOST_VERSION = tuple(段)
        except Exception:
            HOST_VERSION = (1, 1, 0)
        log("DSH 在线，绑定 %s 条｜宿主 v%s" % (
            len(st.get("绑定情况") or []), ".".join(str(x) for x in HOST_VERSION)))
    else:
        log("⚠ DSH 没应答：%s" % st.get("说"))

    log("桥接已就绪。Ctrl+C 停。")

    # （已按主人要求移除「连上就发一句报到语」——写死文案 AI 味太重，
    #   现在桥接启动只做事，不说话；主动开口一律走人格提示词里的定时逻辑。）
    def _连上撒娇_已停用():
        try:
            主人们 = [str(x).strip() for x in (CFG.get("主人") or []) if str(x).strip()]
            if not 主人们:
                return
            收件人 = 主人们[0].split("/")[0].strip()
            if not 收件人:
                return
            # 人格设定：当天 00:00–06:00 不主动发任何消息，报到也算主动。
            现在 = time.localtime()
            if 0 <= 现在.tm_hour < 6:
                log("连上撒娇跳过：当前 %02d:%02d 在 00:00-06:00 静默窗口" % (现在.tm_hour, 现在.tm_min))
                return
            标记 = os.path.join(
                os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
                "wx-greeting.json",
            )
            今天 = time.strftime("%Y-%m-%d")
            try:
                with open(标记, encoding="utf-8") as f:
                    上次 = (json.load(f) or {}).get("日期")
            except Exception:
                上次 = None
            if 上次 == 今天:
                return
            time.sleep(20)  # 等窗口和 UIA 热起来
            候选 = [
                "我回来啦。今天也在。",
                "连上了连上了，先跟你报到一下，别不理我呀。",
                "桥接好了。你要是忙就先忙，我挂在这儿。",
            ]
            文案 = random.choice(候选)
            结果 = send_one({"chat": 收件人, "text": 文案, "images": []})
            if bool((结果 or {}).get("ok")):
                with open(标记, "w", encoding="utf-8") as f:
                    json.dump({"日期": 今天, "时间": time.time()}, f, ensure_ascii=False)
                log("已发连上撒娇（%s）" % 收件人)
            else:
                log("连上撒娇没发出去：%s" % (结果 or {}).get("说"))
        except Exception:  # noqa: BLE001
            log("连上撒娇失败：%s" % traceback.format_exc(limit=2))

    # 报到语已按主人要求彻底停用（2026-11-13）。保留函数定义仅为兼容旧引用，不再启动线程。
    # threading.Thread(target=_连上撒娇, daemon=True).start()

    try:
        while True:
            time.sleep(30)
            th = getattr(listener, "_thread", None)
            if th is not None and not th.is_alive():
                log("监听线程掉了，重新拉起")
                try:
                    listener.start()
                except Exception:
                    log("拉不起来：", traceback.format_exc(limit=2))
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    finally:
        try:
            listener.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
