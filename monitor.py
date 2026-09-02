#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站 UP 主动态监控脚本(免费云版 / GitHub Actions 专用,也可本地运行)

功能:每隔几分钟抓一次指定 UP 的最新动态(含纯图文/文字动态,不依赖B站官方推送),
      发现新动态就推送到手机(微信 PushPlus / Bark / Server酱 / 邮箱,任选其一即可)。

用法:
  1. UID 与 UP名 已填在下方"基础配置"(也可用仓库变量 UP_UID/UP_NAME 覆盖)
  2. 推送渠道二选一或多个,填 key 即启用(推荐微信 PushPlus)
  3. 本文件与 .github/workflows/monitor.yml 一起放到 GitHub 仓库,自动每 5 分钟运行

仅用 Python 标准库,无需 pip 安装任何依赖。
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
import xml.etree.ElementTree as ET
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ============================================================
# 一、基础配置(已代填,一般不用改)
# ============================================================
UP_UID_DEFAULT = 3546610447419885   # UP 的数字 UID
UP_NAME_DEFAULT = "股市里的猩猩"      # UP 名字(用于推送标题)
RSSHUB_INSTANCES = []               # 备用数据源(可选,公共RSSHub不稳定,默认不用)

# ============================================================
# 二、推送渠道(推荐微信 PushPlus;多填会全部推送;都不填=只打印不推送)
# ============================================================
PUSH = {
    # 微信 PushPlus(推荐):https://www.pushplus.plus 微信扫码登录后复制 token
    "PUSHPLUS_TOKEN": "",
    # iPhone Bark:手机装 Bark,给一个 https://api.day.app/XXXXXX,填 XXXXXX
    "BARK_KEY": "",
    # 微信 Server酱:https://sct.ftqq.com 微信扫码登录后复制 SendKey(免费额度每天很少)
    "SERVERCHAN_KEY": "",
    # 邮箱(QQ/163 等):SMTP_USER 填邮箱,SMTP_PASS 填"授权码",SMTP_TO 填收件邮箱
    "SMTP_HOST": "",
    "SMTP_PORT": "465",
    "SMTP_USER": "",
    "SMTP_PASS": "",
    "SMTP_TO": "",
}

# ============================================================
# 通用小工具
# ============================================================

def env(name):
    """环境变量优先(GitHub Actions 注入),否则用 PUSH 默认值。"""
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    return str(PUSH.get(name, ""))


def trunc(text, n=260):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[:n] + "…"


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": [], "baseline": False}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return s


def make_opener():
    """先访问一次B站首页拿匿名 cookie(buvid3),可大幅降低 412 风控概率。"""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        op.open(urllib.request.Request("https://www.bilibili.com/",
                                       headers={"User-Agent": UA}), timeout=20)
    except Exception:
        pass
    return op


# ============================================================
# 动态类型名(两种接口类型编码都覆盖)
# ============================================================

POLYMER_TYPE_NAMES = {
    "DYNAMIC_TYPE_AV": "投稿了视频",
    "DYNAMIC_TYPE_DRAW": "发布了图文动态",
    "DYNAMIC_TYPE_WORD": "发布了一条文字动态",
    "DYNAMIC_TYPE_FORWARD": "转发了一条动态",
    "DYNAMIC_TYPE_ARTICLE": "发布了一篇文章",
    "DYNAMIC_TYPE_NOTE": "发布了一篇笔记",
    "DYNAMIC_TYPE_LIVE_RCMD": "发布了直播动态",
    "DYNAMIC_TYPE_MUSIC": "分享了音乐",
    "DYNAMIC_TYPE_PGC": "发布了剧集动态",
}
LEGACY_TYPE_NAMES = {
    8: "投稿了视频", 64: "发布了图文动态", 4: "发布了图文动态",
    256: "发布了一条文字动态", 1: "转发了一条动态", 2: "发布了一条动态",
}


def type_name(t):
    if isinstance(t, int):
        return LEGACY_TYPE_NAMES.get(t, "发布了一条新动态")
    s = str(t).upper()
    return POLYMER_TYPE_NAMES.get(s, "发布了一条新动态")


# ============================================================
# 抓取动态
#   源1(主):官方新接口 feed/space —— 实测无需登录即可用,含纯图文/文字动态
#   源2(备):官方旧接口 space_history —— 兜底
#   源3(可选):RSSHub 公共实例
# ============================================================

def parse_polymer(data, uid):
    """解析官方新接口返回,输出统一格式的列表(新→旧)。"""
    items = []
    for it in (data.get("data", {}).get("items") or []):
        did = str(it.get("id_str") or "")
        if not did:
            continue
        dtype = str(it.get("type") or "")
        mods = it.get("modules") or {}
        author = mods.get("module_author") or {}
        try:
            ts = int(author.get("pub_ts") or time.time())
        except Exception:
            ts = int(time.time())
        dyn = mods.get("module_dynamic") or {}
        desc_obj = dyn.get("desc")
        desc_text = desc_obj.get("text") if isinstance(desc_obj, dict) else ""
        major = dyn.get("major") or {}
        mtype = str(major.get("type") or "")
        archive = major.get("archive") or {}
        bvid = archive.get("bvid") or ""
        title = archive.get("title") or ""
        opus_obj = major.get("opus")
        opus_text = ""
        if isinstance(opus_obj, dict):
            summary = opus_obj.get("summary") or {}
            if isinstance(summary, dict):
                opus_text = summary.get("text") or ""
        if dtype == "DYNAMIC_TYPE_AV":
            text = desc_text or title or "投稿了视频"
        elif dtype == "DYNAMIC_TYPE_DRAW":
            text = desc_text or "(图文动态,无文字描述)"
        else:
            text = (desc_text or opus_text or "").strip() or "(动态内容见链接)"
        if dtype == "DYNAMIC_TYPE_AV" and bvid:
            link = "https://www.bilibili.com/video/%s" % bvid
        else:
            link = "https://www.bilibili.com/opus/%s" % did
        items.append({
            "id": did, "ts": ts, "type": dtype,
            "type_name": type_name(dtype), "text": trunc(text, 260), "link": link,
        })
    items.sort(key=lambda x: int(x["id"]), reverse=True)
    return items


def fetch_polymer(uid, op):
    """官方新动态接口(主数据源)。"""
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=%d" % uid
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://space.bilibili.com/%d/dynamic" % uid,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    raw = op.open(req, timeout=25).read().decode("utf-8", "ignore")
    data = json.loads(raw)
    if data.get("code") != 0:
        raise RuntimeError("新接口返回 code=%s: %s" % (data.get("code"), data.get("message")))
    return parse_polymer(data, uid)


def _pick(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_legacy(data):
    """解析官方旧接口(space_history)返回,兜底用。"""
    items = []
    for c in (data.get("data", {}).get("cards") or []):
        desc = c.get("desc") or {}
        did = str(desc.get("dynamic_id") or c.get("id_str") or "")
        if not did:
            continue
        typ = int(desc.get("type") or 0)
        card_str = c.get("card") or ""
        text = ""
        try:
            obj = json.loads(card_str)
            if isinstance(obj, dict):
                if typ == 8:
                    text = _pick(obj, "title", "desc") or "投稿了视频"
                else:
                    it = obj.get("item")
                    if isinstance(it, dict):
                        text = _pick(it, "content", "description")
                    text = text or _pick(obj, "content", "description", "desc", "title")
        except Exception:
            text = ""
        items.append({
            "id": did, "ts": desc.get("timestamp") or int(time.time()), "type": typ,
            "type_name": type_name(typ), "text": trunc(text, 260),
            "link": "https://t.bilibili.com/" + did,
        })
    items.sort(key=lambda x: int(x["id"]), reverse=True)
    return items


def fetch_legacy(uid, op):
    """官方旧动态接口(备用数据源)。"""
    url = ("https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history"
           "?host_uid=%d&offset_dynamic_id=0" % uid)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://space.bilibili.com/%d/dynamic" % uid,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    raw = op.open(req, timeout=25).read().decode("utf-8", "ignore")
    data = json.loads(raw)
    if data.get("code") != 0:
        raise RuntimeError("旧接口返回 code=%s: %s" % (data.get("code"), data.get("message")))
    return parse_legacy(data)


def fetch_rss(uid, instances):
    """RSSHub 公共实例(可选备用)。"""
    items = []
    for base in instances:
        try:
            url = base.rstrip("/") + "/bilibili/user/dynamic/" + str(uid)
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/xml"})
            raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            root = ET.fromstring(raw)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                desc = strip_html(item.findtext("description") or "")
                m = re.search(r"(opus|dynamic)/(\d+)", link)
                did = m.group(2) if m else link
                if not did:
                    continue
                kw = re.search(r"(视频|图文|动态|转发|文章)", title)
                items.append({
                    "id": did, "ts": int(time.time()), "type": 0,
                    "type_name": (kw.group(1) if kw else "新") + "动态",
                    "text": trunc(desc or title, 260), "link": link,
                })
            if items:
                return items
        except Exception as e:
            print("  RSS源 %s 失败:%s" % (base, e))
    return []


def fetch_dynamics(uid, op):
    errors = []
    for fn in (fetch_polymer, fetch_legacy):
        try:
            items = fn(uid, op)
            if items:
                return items
        except Exception as e:
            errors.append("%s: %s" % (fn.__name__, e))
    if RSSHUB_INSTANCES:
        items = fetch_rss(uid, RSSHUB_INSTANCES)
        if items:
            return items
    raise RuntimeError("所有数据源均不可用 -> " + " | ".join(errors))


# ============================================================
# 推送(微信 PushPlus / Bark / Server酱 / 邮箱)
# ============================================================

def mask(key):
    k = str(key)
    return k[:2] + "***" + k[-3:] if len(k) > 5 else "***"


def push_pushplus(token, title, body):
    payload = json.dumps({"token": token, "title": title, "content": body, "template": "txt"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=payload,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def push_bark(key, title, body):
    url = "https://api.day.app/%s/%s/%s" % (key, urllib.parse.quote(title), urllib.parse.quote(body))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
        r.read()


def push_serverchan(key, title, body):
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % key, data=data,
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def push_mail(cfg, title, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = (str(Header("B站动态监控", "utf-8")) + " <%s>" % cfg["user"])
    msg["To"] = cfg["to"]
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"] or 465), timeout=20, context=ctx) as s:
        s.login(cfg["user"], cfg["pass"])
        s.sendmail(cfg["user"], [cfg["to"]], msg.as_string())


def build_channels():
    ch = []
    if env("PUSHPLUS_TOKEN"):
        ch.append(("PushPlus(微信)", lambda t, b: push_pushplus(env("PUSHPLUS_TOKEN"), t, b), env("PUSHPLUS_TOKEN")))
    if env("BARK_KEY"):
        ch.append(("Bark(iPhone)", lambda t, b: push_bark(env("BARK_KEY"), t, b), env("BARK_KEY")))
    if env("SERVERCHAN_KEY"):
        ch.append(("Server酱(微信)", lambda t, b: push_serverchan(env("SERVERCHAN_KEY"), t, b), env("SERVERCHAN_KEY")))
    if env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASS") and env("SMTP_TO"):
        cfg = {"host": env("SMTP_HOST"), "port": env("SMTP_PORT"), "user": env("SMTP_USER"),
               "pass": env("SMTP_PASS"), "to": env("SMTP_TO")}
        ch.append(("邮箱", lambda t, b, c=cfg: push_mail(c, t, b), env("SMTP_USER")))
    return ch


def send_notify(title, body):
    """返回是否至少一条渠道推送成功。"""
    channels = build_channels()
    if not channels:
        print("[提示] 未配置任何推送渠道,本次只打印不推送。")
        return True
    ok_any = False
    for name, fn, key in channels:
        try:
            fn(title, body)
            print("  ✓ %s 推送成功(%s)" % (name, mask(key)))
            ok_any = True
        except Exception as e:
            print("  ✗ %s 推送失败(%s):%s" % (name, mask(key), e))
    return ok_any


# ============================================================
# 主流程
# ============================================================

def main():
    uid_raw = env("UP_UID") or str(UP_UID_DEFAULT)
    try:
        uid = int(uid_raw)
    except Exception:
        uid = 0
    if uid <= 0:
        print("尚未配置 UP UID!请在 monitor.py 顶部填 UP_UID_DEFAULT,或配置仓库变量 UP_UID。")
        sys.exit(1)

    name = env("UP_NAME") or UP_NAME_DEFAULT
    print("监控UP: %s (UID=%d)  时间: %s" % (name, uid, fmt_ts(time.time())))

    op = make_opener()
    try:
        items = fetch_dynamics(uid, op)
    except Exception as e:
        print("本次抓取失败:%s(可能被临时风控,下次运行自动重试)" % e)
        return

    if not items:
        print("该UP当前没有可读取的动态(可能近期无动态,正常)。")
        return

    state = load_state()
    seen = set(state.get("seen") or [])

    if not state.get("baseline"):
        top = items[0]
        title = "✅ 开始监控%s" % name
        body = "以后 TA 一发新动态就会提醒你\n\n【当前最新】%s\n%s %s\n%s" % (
            top["text"], top["type_name"], fmt_ts(top["ts"]), top["link"])
        ok = send_notify(title, body)
        state["baseline"] = True
        state["seen"] = list(seen | {x["id"] for x in items})
        save_state(state)
        print("首次运行:已建立基线(最新动态ID=%s),基线消息%s" % (top["id"], "推送成功" if ok else "推送失败(请检查密钥)"))
        return

    news = [x for x in items if x["id"] not in seen]
    if not news:
        print("无新动态(共 %d 条,最新ID=%s)" % (len(items), items[0]["id"]))
        return

    print("发现 %d 条新动态,开始推送…" % len(news))
    all_ok = True
    for it in news:
        title = "%s %s" % (name, it["type_name"])
        body = "%s\n%s\n\n🔗 %s" % (fmt_ts(it["ts"]), it["text"], it["link"])
        ok = send_notify(title, trunc(body, 600))
        print("  - [%s] %s %s" % (it["type_name"], fmt_ts(it["ts"]), it["link"]))
        if not ok:
            all_ok = False

    if all_ok:
        state["seen"] = list(seen | {x["id"] for x in news})
        save_state(state)
        print("推送全部成功,状态已更新。")
    else:
        print("有推送失败,状态暂不更新,5分钟后自动重试,避免漏报。")


if __name__ == "__main__":
    main()
