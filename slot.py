#!/usr/bin/env python3
"""claude-slot — Claude Code usage counter disguised as a pachislot machine.

One spin per interaction: the daemon polls agent states and starts spinning
when a Claude pane enters "working" (= you sent a message); the reels land
the moment the answer finishes. Spin count = your Claude Code usage for the
day (resets at 10:00 JST, like a pachinko parlor opening).

Odds scale with your Claude session usage (use Claude more, win more):
<=50% -> 1/99, then -10 per 10% band, >=90% -> 1/10.
Of jackpots: 777 = 1%, odd triple = 49%, even = 50%.

Rendering uses the same pseudo-agent trick as claude-flex: a mini-workspace
pane is reported as agent "slot", and pane tokens carry the cabinet rows
into the sidebar layout.

Commands:
  ensure  start the background daemon if not already running
  daemon  the daemon loop itself (spawned by ensure)
  fix     one-shot: reposition to bottom + repaint idle display
  spin    run one spin directly (manual test / instant flex)
  stop    stop the daemon and close the mini-workspace
"""
import json
import os
import queue
import random
import signal
import socket as sk
import subprocess
import sys
import threading
import time

PLUGIN_ID = "miko.claude-slot"
SOURCE = f"plugin:{PLUGIN_ID}"
AGENT_ID = "slot"
WS_LABEL = "🎰 Claude"  # claude-usage と同居する共有ミニワークスペース（スペース1枠に統合）
AGENT_DISPLAY = "🎰 くろスロ"  # agents パネルでの見出し（display_agent 経由）
# 大当たり確率はセッション使用率に連動（Claudeをいっぱい使うほど甘くなる仕様）
# 〜50%: 1/99 → 以降10%ごとに分母が10ずつ減り、90%以上で 1/10
BASE_DENOM = 99
HOT_DENOM = 10      # 使用率90%以上の激甘モード
P_777 = 0.01        # 大当たりの内訳: 777 = 1%
P_ODD = 0.49        # 〃 奇数揃い(1,3,5,9) = 49%（残り50%が偶数揃い 0,2,4,6,8）
MAINT_INTERVAL_S = 60

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
# Fixed path on purpose: manual runs and herdr-hook runs must share one pidfile.
STATE_DIR = os.path.expanduser("~/.local/state/herdr/plugins/miko.claude-slot")
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH", "") or os.path.expanduser("~/.config/herdr/herdr.sock")
PIDFILE = os.path.join(STATE_DIR, "daemon.pid")
WS_ID_FILE = os.path.join(STATE_DIR, "workspace.id")
PANE_ID_FILE = os.path.join(STATE_DIR, "pane.id")
STATS_FILE = os.path.join(STATE_DIR, "stats.json")
FEVER_FILE = os.path.join(STATE_DIR, "fever.json")  # popup 演出との連絡用
SQUID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "squid.txt")
FEVER777_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fever777.txt")
# 777の効果音は同梱しない: 好きな音声を 777.mp3 という名前でプラグインフォルダに置くと鳴る
SOUND_777 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "777.mp3")


# ---------- herdr plumbing ----------

def herdr_cli(*args):
    return subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=10)


def list_workspaces():
    out = herdr_cli("workspace", "list")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "workspace list failed")
    return json.loads(out.stdout)["result"]["workspaces"]


def socket_call(method, params, timeout=2):
    """One request over the socket; returns the parsed result dict or None."""
    req = {"id": f"plugin:{PLUGIN_ID}:{time.time_ns()}", "method": method, "params": params}
    try:
        client = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(SOCKET_PATH)
        client.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        client.close()
        return json.loads(buf.split(b"\n", 1)[0]).get("result")
    except Exception:
        return None


def socket_request(method, params):
    socket_call(method, params)


def stored_workspace_id():
    try:
        return open(WS_ID_FILE).read().strip() or None
    except OSError:
        return None


def ensure_slot_workspace(workspaces):
    ids = [w["workspace_id"] for w in workspaces]
    target = stored_workspace_id()
    if target not in ids:
        target = next((w["workspace_id"] for w in workspaces
                       if (w.get("label") or "").startswith("🎰")), None)
        if target:
            with open(WS_ID_FILE, "w") as f:
                f.write(target)
    if target is None:
        out = herdr_cli("workspace", "create", "--label", WS_LABEL, "--cwd", STATE_DIR, "--no-focus")
        if out.returncode != 0:
            return None
        target = json.loads(out.stdout)["result"]["workspace"]["workspace_id"]
        with open(WS_ID_FILE, "w") as f:
            f.write(target)
    return target


def list_panes(workspace_id):
    out = herdr_cli("pane", "list")
    if out.returncode != 0:
        return []
    panes = json.loads(out.stdout)["result"]["panes"]
    return [p["pane_id"] for p in panes if p["workspace_id"] == workspace_id]


def agent_panes():
    """疑似エージェント登録済みの pane_id → agent 名のマップ。"""
    out = herdr_cli("agent", "list")
    try:
        return {a["pane_id"]: a.get("agent") for a in json.loads(out.stdout)["result"]["agents"]}
    except Exception:
        return {}


def my_pane(workspace_id):
    """共有ワークスペース内で自分(slot)が使うペインを確保する。
    記憶済み → 空きペイン（エージェント未登録） → 分割で新規、の順。"""
    panes = list_panes(workspace_id)
    try:
        stored = open(PANE_ID_FILE).read().strip()
    except OSError:
        stored = None
    if stored in panes:
        return stored
    taken = agent_panes()
    pane = next((p for p in panes if taken.get(p) in (None, AGENT_ID)), None)
    if pane is None and panes:
        out = herdr_cli("pane", "split", panes[0], "--direction", "down")
        try:
            pane = json.loads(out.stdout)["result"]["pane"]["pane_id"]
        except Exception:
            return None
    if pane:
        with open(PANE_ID_FILE, "w") as f:
            f.write(pane)
    return pane


def pin_bottom(workspaces, target):
    """The shared mini-space sits at the absolute bottom of the sidebar."""
    ids = [w["workspace_id"] for w in workspaces]
    if ids and ids[-1] != target and target in ids:
        # insert_index semantics: "insert before the element at this pre-removal
        # index"; the full list length appends at the very bottom.
        socket_request("workspace.move", {"workspace_id": target, "insert_index": len(ids)})
    current = next((w.get("label") for w in workspaces if w["workspace_id"] == target), None)
    if current != WS_LABEL:
        herdr_cli("workspace", "rename", target, WS_LABEL)


# ---------- stats ----------

def pachi_day():
    """パチ屋の営業日番号: 日本時間の朝10時が日付の境目（開店リセット用）。"""
    return int((time.time() + 9 * 3600 - 10 * 3600) // 86400)


def stats_read():
    try:
        with open(STATS_FILE) as f:
            stats = json.load(f)
    except Exception:
        stats = {"spins": 0, "hits": 0, "last": [7, 7, 7]}
    today = pachi_day()
    if stats.get("day") != today:
        # 開店リセット: 回転数・揃い数は毎日 JST 10:00 にゼロから
        stats = {"spins": 0, "hits": 0, "last": stats.get("last", [7, 7, 7]), "day": today}
        stats_write(stats)
    return stats


def stats_write(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f)
    except OSError:
        pass


# ---------- rendering ----------

import unicodedata

INNER = 15  # 筐体の内側の表示幅（桁数）
FW_DIGITS = "０１２３４５６７８９"

C_TOP = "╔" + "═" * INNER + "╗"
C_SEP1 = "╠" + "═" * INNER + "╣"
C_SEP2 = "╟" + "─" * INNER + "╢"
C_BOT = "╚" + "═" * INNER + "╝"

def dwidth(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def boxed(s):
    """筐体の内側にセンタリングして左右の枠で閉じる。"""
    pad = max(0, INNER - dwidth(s))
    left = pad // 2
    return "║" + " " * left + s + " " * (pad - left) + "║"


REEL_TOP = boxed(" ".join(["┌──┐"] * 3))
REEL_BOT = boxed(" ".join(["└──┘"] * 3))

# 色バリアントの仕組み: 色は config 側でトークンごとに固定なので、
# 「同じ行に色違いトークンを並べて、使う色にだけ値を入れる」ことで動的に見せる。
# 注意: 同じ行に複数トークンが同時に値を持つと「・」区切りで連結されてズレるので、
# 各行とも常に1トークンだけに値を入れる（リールも行単位で1色）
PALETTE = ["b", "r", "y", "c", "m"]  # 青 / ピンク赤 / 黄 / 水色 / 紫
SCR_PALETTE = ["w"] + PALETTE
RAINBOW_CYCLE = ["y", "c", "m", "r", "b"]  # 777 演出の色サイクル


def row_color(digits):
    """リール行の色: 3桁の偶奇多数決（偶数優勢=青 / 奇数優勢=ピンク赤）"""
    evens = sum(1 for d in digits if d % 2 == 0)
    return "b" if evens >= 2 else "r"


def gap_row(lever):
    # 右サイドにレバー: 待機は水平アーム、引いた瞬間はアームが下に折れる
    return "║" + " " * INNER + ("╠═╗" if lever == "pulled" else "╠══○")


def button_row(locked, lever):
    # リールのセル(4桁幅+区切り1)と同じピッチで、各リールの真下にボタンを置く
    row = boxed(" ".join(f" {'●' if l else '○'}  " for l in locked).rstrip())
    return row + (" ●" if lever == "pulled" else "")


def report_tokens(pane, ops):
    """1回の report は最大16トークン操作までなので、超えたら分割して送る。
    ops: ("t", name, value) で設定 / ("c", name) でクリア"""
    for i in range(0, len(ops), 16):
        args = []
        for op in ops[i:i + 16]:
            if op[0] == "t":
                args += ["--token", f"{op[1]}={op[2]}"]
            else:
                args += ["--clear-token", op[1]]
        herdr_cli("pane", "report-metadata", pane, "--source", SOURCE, *args)


# 前フレームでどの色トークンを使ったか（差分クリア用）
_prev = {"s1": None, "s2": None, "rl": None}


def paint_statics(pane):
    """固定枠の描画 + 全色バリアントの掃除（起動/メンテ時に1回）。"""
    ops = [("t", "s_top", C_TOP), ("t", "s_sep1", C_SEP1), ("t", "s_sep2", C_SEP2),
           ("t", "s_rl_t", REEL_TOP), ("t", "s_rl_b", REEL_BOT), ("t", "s_bot", C_BOT)]
    for name in ("s1", "s2"):
        ops += [("c", f"{name}_{ck}") for ck in SCR_PALETTE]
    ops += [("c", f"rl_{ck}") for ck in PALETTE]
    report_tokens(pane, ops)
    _prev["s1"] = _prev["s2"] = _prev["rl"] = None


def render(pane, reels, scr1, scr2, stats, locked=(True, True, True), lever="rest",
           reel_col=None, scr_col="w"):
    """reel_col: リール行の色キー（省略時は偶奇多数決）。
    scr_col: モニター2行の色キー。色はトークンの出し分けで実現する。"""
    if reel_col is None:
        reel_col = row_color(reels)
    marquee = f"{stats['hits']}揃い/{stats['spins']}回転"
    ops = [("t", "s_marq", boxed(marquee)),
           ("t", "s_gap", gap_row(lever)),
           ("t", "s_btn", button_row(locked, lever))]
    # モニター2行 + リール行: 使う色に値を入れ、色が変わったときだけ旧色をクリア
    reel_text = boxed(" ".join(f"│{FW_DIGITS[d]}│" for d in reels))
    for name, text, col in (("s1", boxed(scr1), scr_col),
                            ("s2", boxed(scr2), scr_col),
                            ("rl", reel_text, reel_col)):
        if _prev[name] is not None and _prev[name] != col:
            ops.append(("c", f"{name}_{_prev[name]}"))
        ops.append(("t", f"{name}_{col}", text))
        _prev[name] = col
    report_tokens(pane, ops)


def setup_block(workspaces=None):
    """Make sure workspace + pane + pseudo-agent exist; returns pane id."""
    if workspaces is None:
        workspaces = list_workspaces()
    target = ensure_slot_workspace(workspaces)
    if not target:
        return None
    pin_bottom(workspaces, target)
    pane = my_pane(target)
    if pane:
        herdr_cli("pane", "report-agent", pane, "--source", SOURCE,
                  "--agent", AGENT_ID, "--state", "idle")
        herdr_cli("pane", "report-metadata", pane, "--source", SOURCE,
                  "--display-agent", AGENT_DISPLAY)
        paint_statics(pane)
    return pane


# ---------- the game ----------

FORCE_FILE = os.path.join(STATE_DIR, "force.json")

# ---------- セッション使用率（大当たり確率の変動用） ----------
# 確率変動は姉妹プラグイン claude-usage(miko.claude-flex) が入っている場合だけ有効。
# その5分毎キャッシュを読むだけで、無ければ常に基本確率 1/99 で動く。
FLEX_CACHE = os.path.expanduser("~/.local/state/herdr/plugins/miko.claude-flex/usage.json")
USAGE_MAX_AGE_S = 600


def session_pct():
    """セッション使用率(%)。姉妹プラグインのキャッシュ限定、無ければ None。"""
    try:
        if time.time() - os.path.getmtime(FLEX_CACHE) > USAGE_MAX_AGE_S:
            return None
        with open(FLEX_CACHE, encoding="utf-8") as f:
            return json.load(f)["session"]["pct"]
    except Exception:
        return None


def jackpot_denom(pct):
    """使用率→確率分母: 〜50%=99, 以降10%毎に-10, 90%以上=10"""
    if pct is None or pct < 50:
        return BASE_DENOM
    if pct >= 90:
        return HOT_DENOM
    return BASE_DENOM - 10 * ((int(pct) - 40) // 10)


def pick_outcome():
    # 仕込み: force.json に [7,7,7] などを置くと次の1回だけその出目で着地する
    try:
        with open(FORCE_FILE) as f:
            forced = json.load(f)
        os.remove(FORCE_FILE)
        if (isinstance(forced, list) and len(forced) == 3
                and all(isinstance(d, int) and 0 <= d <= 9 for d in forced)):
            return forced
    except Exception:
        pass
    if random.random() < 1 / jackpot_denom(session_pct()):
        r = random.random()
        if r < P_777:
            d = 7
        elif r < P_777 + P_ODD:
            d = random.choice([1, 3, 5, 9])
        else:
            d = random.choice([0, 2, 4, 6, 8])
        return [d, d, d]
    while True:
        reels = [random.randint(0, 9) for _ in range(3)]
        if not reels[0] == reels[1] == reels[2]:
            return reels


SPIN_WAVE = ["≫　　　　", "　≫　　　", "　　≫　　", "　　　≫　", "　　　　≫"]
REACH_S1 = ["　リーチ！！", "＞リーチ！＜", "▼リーチ！▼", "＞リーチ！＜"]
REACH_S2 = ["ドキドキ…", "くるかにぇ？", "ドキドキ…", "！！！？"]


def triple_colors(d):
    """揃い目の色: 777=金(演出中は虹サイクル), 偶数=青, 奇数=ピンク赤"""
    if d == 7:
        return "y", "m"
    ck = "b" if d % 2 == 0 else "r"
    return ck, ck


def celebrate(pane, final, stats):
    d3 = FW_DIGITS[final[0]] * 3
    if final[0] == 7:
        seq = [
            (f"＼{d3}／", "☆★☆★☆"),
            (f"★{d3}★", "エリート！！"),
            (f"／{d3}＼", "★☆★☆★"),
            ("大当り！！", "＼(^o^)／"),
            (f"★{d3}★", "にぇ〜！！"),
            ("ＪＡＣＫＰＯＴ", f"＼{d3}／"),
        ] * 2
        # 虹演出: リールとモニターの色を毎フレームずらしながらサイクルさせる
        for f, (s1, s2) in enumerate(seq):
            render(pane, final, s1, s2, stats,
                   reel_col=RAINBOW_CYCLE[f % 5], scr_col=RAINBOW_CYCLE[(f + 2) % 5])
            time.sleep(0.35)
    else:
        seq = random.choice([
            [
                ("ピカーン！", f"＼{d3}／"),
                (f"＊{d3}＊", "＊　＊　＊"),
                (f"＊{d3}＊", "　＊　＊　"),
                ("大当り！！", "＼(^o^)／"),
                (f"＼{d3}／", "やったにぇ！"),
            ],
            [
                (f"＞＞{d3}＜＜", "♪　♪　♪"),
                (f"＼{d3}／", "　♪　♪　"),
                ("大当り！！", "＼(^o^)／"),
                (f"＊{d3}＊", "えらい！！"),
            ],
        ] * 2)
        cols, sc = triple_colors(final[0])
        for s1, s2 in seq:
            render(pane, final, s1, s2, stats, reel_col=cols, scr_col=sc)
            time.sleep(0.35)
    # 最後の静止画は描かない: ここから先は ambient_frame が RUSH/FEVER/ぼーなす表示で回し続ける


def lever_pull(pane, stats):
    cur = [random.randint(0, 9) for _ in range(3)]
    render(pane, cur, "ガコッ！", "", stats, [False] * 3, lever="pulled")
    time.sleep(0.4)


def spin_frame(pane, stats, f):
    """回答待ちのあいだ回り続けるフレーム（ゆっくりめ）。"""
    cur = [random.randint(0, 9) for _ in range(3)]
    render(pane, cur, "かんがえ中…", SPIN_WAVE[f % len(SPIN_WAVE)], stats, [False] * 3)


def fever_write(stage, final):
    try:
        with open(FEVER_FILE, "w") as f:
            json.dump({"stage": stage, "final": final, "ts": time.time()}, f)
    except OSError:
        pass


def land_spin(pane):
    """回答完了（=ユーザー待ち）の瞬間に呼ばれて、リールを順に止める。"""
    stats = stats_read()
    final = pick_outcome()
    reach = final[0] == final[1]
    if final == [7, 7, 7]:
        # 777確定のときだけ: リーチが走るこのスピンの冒頭で FEVER ポップアップを開く
        fever_write("reach", final)
        herdr_cli("plugin", "pane", "open",
                  "--plugin", PLUGIN_ID, "--entrypoint", "fever")
    locks = [2, 5, 10 if reach else 7]  # 待ち中に散々回ったので停止は短め
    f = 0
    while f <= locks[-1]:
        locked = [f >= lk for lk in locks]
        cur = [final[i] if locked[i] else random.randint(0, 9) for i in range(3)]
        in_reach = reach and locked[1] and not locked[2]
        if in_reach:
            s1 = REACH_S1[f % len(REACH_S1)]
            s2 = REACH_S2[(f // 2) % len(REACH_S2)]
            sc = "r"  # リーチは赤ピンクで煽る
            delay = 0.3
        else:
            s1 = "とまるにぇ！"
            s2 = SPIN_WAVE[f % len(SPIN_WAVE)]
            sc = "w"
            delay = 0.15
        render(pane, cur, s1, s2, stats, locked, scr_col=sc)
        time.sleep(delay)
        f += 1
    stats["spins"] += 1
    hit = final[0] == final[1] == final[2]
    if hit:
        stats["hits"] += 1
    stats["last"] = final
    stats_write(stats)
    if hit:
        # popup が虹色演出に切り替わり、10秒後に自走終了→ポップアップも閉じる
        fever_write("win", final)
        if final == [7, 7, 7] and os.path.exists(SOUND_777):
            # 揃ったと同時に確定音（777.mp3 が置いてあれば非ブロッキング再生）
            subprocess.Popen(["afplay", SOUND_777],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        celebrate(pane, final, stats)
    elif reach:
        render(pane, final, "おしい…！！", "つぎこそにぇ", stats)
        time.sleep(2.0)  # 余韻を見せてから ambient に引き継ぐ
    else:
        render(pane, final, "ざんねん…", "つぎいくにぇ", stats)
        time.sleep(2.0)


def ambient_frame(pane, stats, f):
    """待機中も次のスピンまで動き続けるアンビエント演出（0.7s/フレーム）。"""
    last = stats.get("last", [7, 7, 7])
    hit = last[0] == last[1] == last[2]
    if hit:
        d = last[0]
        d3 = FW_DIGITS[d] * 3
        s2 = [f"＼{d3}／", f"＊{d3}＊"][f % 2]
        if d == 7:
            # 3000 FEVER: 虹サイクルで文字もリールも回り続ける
            render(pane, last, "3000 FEVER", s2, stats,
                   reel_col=RAINBOW_CYCLE[f % 5], scr_col=RAINBOW_CYCLE[(f + 2) % 5])
        elif d % 2 == 1:
            # 1500 RUSH: 赤⇔黄でギラギラ点滅
            render(pane, last, "1500 RUSH突入！", s2, stats,
                   reel_col="r", scr_col=["r", "y"][f % 2])
        else:
            # 300ぼーなす: 青⇔水色でゆらゆら
            render(pane, last, "300ぼーなすにゃ", s2, stats,
                   reel_col="b", scr_col=["b", "c"][f % 2])
    else:
        s2 = ["めざせ７７７", SPIN_WAVE[f % len(SPIN_WAVE)]][f % 2]
        render(pane, last, "＊くろスロ＊", s2, stats)


def idle_paint(pane):
    ambient_frame(pane, stats_read(), 0)


# ---------- interaction watcher ----------

def watch_agents(evq):
    """Poll agent.list over the socket (no subprocess). Emits:
      ("start", pane_id)  claude pane entered 'working'  (= user sent a message)
      ("land",  pane_id)  claude pane left 'working'     (= answer done, user's turn)
    report-agent driven changes don't show up on any globally-subscribable
    event, so polling it is."""
    last = {}
    first = True
    while os.path.exists(SOCKET_PATH):
        result = socket_call("agent.list", {})
        agents = (result or {}).get("agents")
        if agents is not None:
            seen = set()
            for a in agents:
                if a.get("agent") != "claude":
                    continue
                pid, status = a["pane_id"], a.get("agent_status")
                seen.add(pid)
                prev = last.get(pid)
                # seed on the first pass so already-working agents don't fire
                if not first:
                    if status == "working" and prev != "working":
                        evq.put(("start", pid))
                    elif status != "working" and prev == "working":
                        evq.put(("land", pid))
                last[pid] = status
            for pid in [p for p in last if p not in seen]:
                if last[pid] == "working" and not first:
                    evq.put(("land", pid))  # 回答中にペインごと消えた
                del last[pid]
            first = False
        time.sleep(1)


# ---------- commands ----------

def running_pid():
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def cmd_ensure():
    if running_pid():
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def cmd_daemon():
    os.makedirs(STATE_DIR, exist_ok=True)
    other = running_pid()
    if other and other != os.getpid():
        return
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    time.sleep(1)  # settle the spawn race: last writer wins, everyone else bows out
    try:
        if int(open(PIDFILE).read().strip()) != os.getpid():
            return
    except Exception:
        return
    pane = setup_block()
    if pane:
        idle_paint(pane)
    evq = queue.Queue()
    threading.Thread(target=watch_agents, args=(evq,), daemon=True).start()
    active = set()  # いま回答生成中の claude ペイン
    f = 0   # スピンフレーム
    af = 0  # アンビエントフレーム
    last_maint = time.time()
    while os.path.exists(SOCKET_PATH):
        try:
            kind, pid = evq.get(timeout=0.35 if active else 0.7)
            if kind == "start":
                if pane and not active:
                    lever_pull(pane, stats_read())
                active.add(pid)
            elif kind == "land":
                active.discard(pid)
                if pane:
                    land_spin(pane)  # 復帰直後の land (start無し) も1回転扱い
        except queue.Empty:
            pass
        if pane:
            if active:
                spin_frame(pane, stats_read(), f)
                f += 1
            else:
                # 待機中も次のスピンまで演出を回し続ける
                ambient_frame(pane, stats_read(), af)
                af += 1
        if time.time() - last_maint > MAINT_INTERVAL_S:
            try:
                pane = setup_block()  # paint_statics 後も次のフレームですぐ再描画される
            except Exception:
                pane = None
            last_maint = time.time()
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def cmd_fix():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        pane = setup_block()
        if pane:
            idle_paint(pane)
    except Exception:
        pass
    cmd_ensure()


def cmd_spin():
    os.makedirs(STATE_DIR, exist_ok=True)
    pane = setup_block()
    if pane:
        lever_pull(pane, stats_read())
        for f in range(6):
            spin_frame(pane, stats_read(), f)
            time.sleep(0.2)
        land_spin(pane)


# ---------- FEVER popup ----------

ANSI_RAINBOW = [196, 208, 220, 46, 51, 33, 129, 201]  # 赤橙黄緑水青紫桃


def fever_read():
    try:
        with open(FEVER_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def load_art(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().rstrip("\n").split("\n")
    except OSError:
        return [fallback]


def cmd_fever():
    """777確定演出ポップアップ（64x32セル）。reach 中は王冠がグレーでせり上がり、
    win で虹色の王冠3000。win 表示から10秒で自動終了（手動でも閉じられる）。"""
    art = ["  " + l for l in load_art(FEVER777_FILE, "3000 FEVER")]
    out = sys.stdout
    out.write("\033[?25l")  # カーソル隠す
    start = time.time()
    f = 0
    try:
        # --- リーチ（確定）フェーズ: シルエットがせり上がる + バナー点滅 ---
        while True:
            st = fever_read()
            if st and st.get("stage") == "win":
                break
            if time.time() - start > 90:  # 安全弁: 置き去りにされたら勝手に終わる
                return
            out.write("\033[2J\033[H")
            banner = "＞＞＞　リ　ー　チ　！！！　＜＜＜"
            color = [196, 226][f % 2]
            out.write(f"\033[1m\033[38;5;{color}m{'　' * 6}{banner}\033[0m\n")
            if f % 4 >= 2:
                out.write(f"\033[38;5;213m{'　' * 9}＊＊＊　確定！？　＊＊＊\033[0m\n")
            else:
                out.write("\n")
            reveal = min(len(art), (f + 1) * 3)  # 3行ずつ出現
            for line in art[-reveal:]:
                out.write(f"\033[38;5;240m{line}\033[0m\n")
            out.flush()
            time.sleep(0.25)
            f += 1
        # --- 大当りフェーズ: 虹色の王冠3000 ---
        t_win = time.time()
        f = 0
        while time.time() - t_win < 10:
            out.write("\033[2J\033[H")
            head_c = ANSI_RAINBOW[f % len(ANSI_RAINBOW)]
            out.write(f"\033[1m\033[38;5;{head_c}m{'　' * 5}"
                      f"★☆★　大　当　り　７７７　★☆★\033[0m\n")
            for i, line in enumerate(art):
                c = ANSI_RAINBOW[(i + f) % len(ANSI_RAINBOW)]
                out.write(f"\033[38;5;{c}m{line}\033[0m\n")
            tail_c = ANSI_RAINBOW[(f + 4) % len(ANSI_RAINBOW)]
            remain = 10 - int(time.time() - t_win)
            out.write(f"\033[1m\033[38;5;{tail_c}m{'　' * 7}"
                      f"｜｜　３０００ ＦＥＶＥＲ　｜｜\033[0m")
            out.write(f"\033[2m ({remain}s)\033[0m\n")
            out.flush()
            time.sleep(0.18)
            f += 1
    finally:
        out.write("\033[?25h\033[0m")
        out.flush()


def cmd_stop():
    pid = running_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        workspaces = list_workspaces()
        mine = stored_workspace_id()
        if mine and any(w["workspace_id"] == mine for w in workspaces):
            # 共有ワークスペースなので、他の疑似エージェント(usage等)が居なければ閉じる
            others = [p for p, a in agent_panes().items()
                      if p.startswith(mine + ":") and a != AGENT_ID]
            if others:
                try:
                    pane = open(PANE_ID_FILE).read().strip()
                    herdr_cli("pane", "release-agent", pane, "--source", SOURCE)
                    herdr_cli("pane", "close", pane)
                except OSError:
                    pass
            else:
                herdr_cli("workspace", "close", mine)
        os.remove(WS_ID_FILE)
        os.remove(PANE_ID_FILE)
    except Exception:
        pass
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    {"ensure": cmd_ensure, "daemon": cmd_daemon, "fix": cmd_fix,
     "spin": cmd_spin, "fever": cmd_fever, "stop": cmd_stop}.get(cmd, cmd_ensure)()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
