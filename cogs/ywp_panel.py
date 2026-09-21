import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import asyncio
import json
import os
import re
import time
import unicodedata
import random
from datetime import datetime, timezone, timedelta, time as dtime

from cogs.ywp_auto import Client, login_email, RC, YPOINT_ITEM_ID, parse_item_rows, parse_user_data

# ============================================================
# JST
# ============================================================
JST = timezone(timedelta(hours=9))

# ============================================================
# データ
# ============================================================
DATA_DIR = "data"
SESSION_FILE = f"{DATA_DIR}/sessions.json"
LOOP_FILE = f"{DATA_DIR}/loops.json"
PANEL_FILE = f"{DATA_DIR}/panel.json"
BENCH_FILE = f"{DATA_DIR}/bench.json"
DAILY_FILE = f"{DATA_DIR}/daily.json"
DEBUG_BUY_FILE = f"{DATA_DIR}/debug_buyhitodama.json"
DEBUG_GAMEEND_FILE = f"{DATA_DIR}/debug_gameend.json"

# ============================================================
# 設定
# ============================================================
MAX_CONCURRENT_LOOPS = 30

HITODAMA_THRESHOLD = 5
HITODAMA_RESUME_MIN = 5

AUTO_BUY_HITODAMA = True
AUTO_BUY_GOODS_ID = 1001
AUTO_BUY_GOODS_IDS = [1001]
AUTO_BUY_COST_YM = 1000
AUTO_BUY_MAX_PER_LOOP = 9999
AUTO_BUY_KEEP_YMONEY = 5000
AUTO_BUY_HITODAMA_GAIN = 2

BENCH_MAX_STAGES = 8
BENCH_MAX_SAMPLES = 10

DAILY_REPORT_ENABLED = True
DAILY_REPORT_HOUR = 23
DAILY_REPORT_MINUTE = 55

REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 3.0
COOLDOWN_MIN = 2.5
COOLDOWN_MAX = 3.0

ODD_HOUR_REST_ENABLED = True
ODD_HOUR_REST_MIN = 29
ODD_HOUR_REST_DURATION = 300

RETRY_CODES = (4, 5, 32)
RETRY_WAITS = [4, 5, 5]

EVENT_SKIP_CODES = (1303, 101, 102, 1701)

LOCKED_RC = 5
LOCKED_STAGE_LIMIT = 3

MAX_CONSECUTIVE_ERRORS = 4

_session_lock = asyncio.Lock()
_loop_lock = asyncio.Lock()

_panel_hitodama_cache = {}


# ============================================================
# JSON キャッシュ
# ============================================================
_json_cache = {}


def ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default=None):
    ensure_dir()
    if not os.path.exists(path):
        return default or {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return default or {}
    cached = _json_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return default or {}
    _json_cache[path] = (mtime, data)
    return data


def save_json(path, data):
    ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        _json_cache[path] = (os.path.getmtime(path), data)
    except OSError:
        _json_cache.pop(path, None)


def load_loops() -> dict:
    return load_json(LOOP_FILE, {})


def save_loops(data):
    save_json(LOOP_FILE, data)


async def save_loops_async(data):
    async with _loop_lock:
        save_loops(data)


# ============================================================
# 複数アカウント管理
# ============================================================
def _migrate_session(sdata: dict) -> dict:
    if "accounts" in sdata:
        return sdata
    if "udkey" not in sdata:
        return {"accounts": [], "active_index": 0}
    acc = {
        "email": sdata.get("email", ""),
        "password": sdata.get("password", ""),
        "player_id": str(sdata.get("userId", "")),
        "player_name": sdata.get("playerName", "不明"),
        "udkey": sdata["udkey"],
        "gdkey": sdata["gdkey"],
        "userId": sdata["userId"],
        "token": sdata["token"],
        "mst": sdata.get("mst", 16897),
        "saved_at": sdata.get("saved_at", ""),
    }
    return {"accounts": [acc], "active_index": 0}


def load_sessions() -> dict:
    return load_json(SESSION_FILE, {})


def save_sessions(data):
    save_json(SESSION_FILE, data)


async def save_sessions_async(data):
    async with _session_lock:
        save_sessions(data)


def get_user_session(user_id: int) -> dict:
    sessions = load_sessions()
    sdata = sessions.get(str(user_id))
    if not sdata:
        return {"accounts": [], "active_index": 0}
    return _migrate_session(sdata)


def get_user_accounts(user_id: int) -> list:
    return get_user_session(user_id).get("accounts", [])


def get_active_account(user_id: int) -> dict | None:
    sess = get_user_session(user_id)
    accounts = sess.get("accounts", [])
    if not accounts:
        return None
    idx = sess.get("active_index", 0)
    if idx >= len(accounts):
        idx = 0
    return accounts[idx]


def get_active_id(user_id):
    sess = get_user_session(user_id)
    accounts = sess.get("accounts", [])
    if not accounts:
        return None
    idx = sess.get("active_index", 0)
    if idx >= len(accounts):
        idx = 0
    acc = accounts[idx]
    return str(acc.get("player_id") or acc.get("userId") or "")


def get_session(user_id, account_id=None):
    if account_id is None:
        return get_active_account(user_id)
    accounts = get_user_accounts(user_id)
    for acc in accounts:
        if str(acc.get("player_id")) == str(account_id) or str(acc.get("userId")) == str(account_id):
            return acc
    return None


def get_accounts(user_id) -> dict:
    out = {}
    for acc in get_user_accounts(user_id):
        gid = str(acc.get("player_id") or acc.get("userId") or "")
        if gid:
            out[gid] = acc
    return out


def set_active_index(user_id: int, index: int):
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    sess["active_index"] = index
    sessions[uid] = sess
    save_sessions(sessions)


async def add_account(user_id, client, player_name: str):
    store = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(store.get(uid, {}))
    accounts = sess.get("accounts", [])
    gid = str(client.userId)
    new_acc = {
        "email": "",
        "password": "",
        "player_id": gid,
        "player_name": player_name,
        "udkey": client.udkey,
        "gdkey": client.gdkey,
        "userId": client.userId,
        "token": client.token,
        "mst": client.mst,
        "saved_at": datetime.now(JST).isoformat(),
    }
    found = None
    for i, acc in enumerate(accounts):
        if str(acc.get("player_id")) == gid:
            found = i
            break
    if found is not None:
        accounts[found] = new_acc
        sess["active_index"] = found
    else:
        accounts.append(new_acc)
        sess["active_index"] = len(accounts) - 1
    sess["accounts"] = accounts
    store[uid] = sess
    await save_sessions_async(store)
    return gid


async def set_active(user_id, account_id) -> bool:
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    accounts = sess.get("accounts", [])
    for i, acc in enumerate(accounts):
        if str(acc.get("player_id")) == str(account_id) or str(acc.get("userId")) == str(account_id):
            sess["active_index"] = i
            sessions[uid] = sess
            await save_sessions_async(sessions)
            return True
    return False


async def remove_account(user_id, account_id) -> bool:
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    accounts = sess.get("accounts", [])
    new_accounts = [
        acc for acc in accounts
        if str(acc.get("player_id")) != str(account_id)
        and str(acc.get("userId")) != str(account_id)
    ]
    if len(new_accounts) == len(accounts):
        return False
    if not new_accounts:
        sessions.pop(uid, None)
    else:
        idx = sess.get("active_index", 0)
        if idx >= len(new_accounts):
            idx = 0
        sess["accounts"] = new_accounts
        sess["active_index"] = idx
        sessions[uid] = sess
    await save_sessions_async(sessions)
    return True


async def update_token(user_id, account_id, client):
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    accounts = sess.get("accounts", [])
    for acc in accounts:
        if str(acc.get("player_id")) == str(account_id) or str(acc.get("userId")) == str(account_id):
            acc["token"] = client.token
            acc["mst"] = client.mst
            break
    sess["accounts"] = accounts
    sessions[uid] = sess
    await save_sessions_async(sessions)


def upsert_account(user_id, email, password, client):
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    accounts = sess.get("accounts", [])
    player_id = str(client.userId)
    info = client.info()
    player_name = info.get("playerName", "不明")
    new_acc = {
        "email": email,
        "password": password,
        "player_id": player_id,
        "player_name": player_name,
        "udkey": client.udkey,
        "gdkey": client.gdkey,
        "userId": client.userId,
        "token": client.token,
        "mst": client.mst,
        "saved_at": datetime.now(JST).isoformat(),
    }
    found_idx = None
    for i, acc in enumerate(accounts):
        if acc.get("email") == email and str(acc.get("player_id")) == player_id:
            found_idx = i
            break
    if found_idx is not None:
        accounts[found_idx] = new_acc
        sess["active_index"] = found_idx
    else:
        accounts.append(new_acc)
        sess["active_index"] = len(accounts) - 1
    sess["accounts"] = accounts
    sessions[uid] = sess
    save_sessions(sessions)
    return new_acc


def build_client_from_account(acc: dict) -> Client:
    client = Client(acc["udkey"])
    client.gdkey = acc["gdkey"]
    client.userId = acc["userId"]
    client.token = acc["token"]
    client.mst = acc.get("mst", 16897)
    return client


def make_client(sdata: dict) -> Client:
    return build_client_from_account(sdata)


def update_account_tokens(user_id: int, client: Client):
    sessions = load_sessions()
    uid = str(user_id)
    sess = _migrate_session(sessions.get(uid, {}))
    accounts = sess.get("accounts", [])
    idx = sess.get("active_index", 0)
    if 0 <= idx < len(accounts):
        accounts[idx]["token"] = client.token
        accounts[idx]["mst"] = client.mst
        sess["accounts"] = accounts
        sessions[uid] = sess
        save_sessions(sessions)


def account_busy(account_id) -> bool:
    for data in load_loops().values():
        if data.get("status") != "running":
            continue
        if str(data.get("account_id") or "") == str(account_id):
            return True
    return False


def load_panel() -> dict:
    return load_json(PANEL_FILE, {})


def save_panel(data):
    save_json(PANEL_FILE, data)


# ============================================================
# ランダム生成
# ============================================================
def rand_request_delay() -> float:
    return random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)


def rand_cooldown() -> float:
    return random.uniform(COOLDOWN_MIN, COOLDOWN_MAX)


# ============================================================
# 奇数時間休憩
# ============================================================
async def check_odd_hour_rest(loop_key=None):
    if not ODD_HOUR_REST_ENABLED:
        return False
    now = datetime.now(JST)
    if now.hour % 2 == 1 and now.minute == ODD_HOUR_REST_MIN:
        print(f">>> 奇数時間休憩開始 ({now.hour:02d}:{now.minute:02d} JST) → {ODD_HOUR_REST_DURATION}秒停止")
        await asyncio.sleep(ODD_HOUR_REST_DURATION)
        print(f">>> 奇数時間休憩終了 → 周回再開")
        return True
    return False


# ============================================================
# 日時パーサ
# ============================================================
def parse_datetime(s: str):
    s = s.strip()
    formats = [
        "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


# ============================================================
# 3秒対策
# ============================================================
async def safe_defer(interaction, ephemeral=True, thinking=True):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except (discord.NotFound, discord.HTTPException):
        return False


async def safe_reply(interaction, **kwargs):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        else:
            return await interaction.response.send_message(**kwargs)
    except (discord.NotFound, discord.HTTPException):
        pass


# ============================================================
# ユーザーの古いジョブを整理
# ============================================================
async def cleanup_user_loops(user_id: int):
    loops = load_loops()
    keys_to_delete = []
    for key, data in loops.items():
        if data.get("user_id") != user_id:
            continue
        if data.get("status") in ("stopped", "done", "fatal", "error"):
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del loops[key]
    if keys_to_delete:
        await save_loops_async(loops)


# ============================================================
# ステージパーサ
# ============================================================
def parse_stages(stage_raw: str) -> dict:
    result = {}
    if not stage_raw:
        return result
    for row in str(stage_raw).split("*"):
        if not row:
            continue
        cols = row.split("|")
        if not cols:
            continue
        try:
            sid = int(cols[0])
            if not (1 <= sid <= 99999999):
                continue
            cleared = len(cols) >= 2 and cols[1] == "1"
            result[sid] = {"cols": cols, "cleared": cleared}
        except (ValueError, IndexError):
            pass
    return result


# ============================================================
# ステージ分類
# ============================================================
STAGE_CATEGORIES = [
    {"name": "📖 メインステージ", "pattern": lambda sid: str(sid).startswith(("1001", "1002", "1003"))},
    {"name": "📖 メイン（2章以降）", "pattern": lambda sid: str(sid).startswith("1901")},
    {"name": "🎯 サブステージ", "pattern": lambda sid: str(sid).startswith(("5001", "5002", "5003", "5004",
                                                                            "5005", "5006", "5007", "5008",
                                                                            "5009", "5010", "5011"))},
    {"name": "🎪 イベントステージ", "pattern": lambda sid: str(sid).startswith(("3167", "2970"))},
    {"name": "💪 強敵ステージ", "pattern": lambda sid: str(sid).startswith(("2880", "2890"))},
    {"name": "🌙 夜叉ステージ", "pattern": lambda sid: str(sid).startswith("2900")},
    {"name": "❓ その他", "pattern": lambda sid: True},
]


def categorize_stages(stage_ids: list) -> dict:
    result = {}
    assigned = set()
    for cat in STAGE_CATEGORIES:
        name = cat["name"]
        bucket = []
        for sid in stage_ids:
            if sid in assigned:
                continue
            try:
                if cat["pattern"](sid):
                    bucket.append(sid)
                    assigned.add(sid)
            except Exception:
                pass
        if bucket:
            result[name] = bucket
    return result


# ============================================================
# 同時実行
# ============================================================
def get_running_count() -> int:
    loops = load_loops()
    return sum(1 for data in loops.values() if data.get("status") == "running")


def can_start_loop() -> tuple:
    current = get_running_count()
    return current < MAX_CONCURRENT_LOOPS, current


# ============================================================
# 人魂
# ============================================================
def get_hitodama_detail(client: Client) -> dict:
    try:
        data = parse_user_data(client.save.get("ywp_user_data"))
        paid = int(data.get("hitodama", 0))
        free = int(data.get("freeHitodama", 0))
        return {"paid": paid, "free": free, "total": paid + free,
                "recover_sec": int(data.get("hitodamaRecoverSec", 0))}
    except Exception:
        return {"paid": 999, "free": 0, "total": 999, "recover_sec": 0}


def get_hitodama(client: Client) -> int:
    return get_hitodama_detail(client)["total"]


def get_ymoney(client: Client) -> int:
    try:
        data = client.save.get("ywp_user_data", {})
        if isinstance(data, str):
            data = json.loads(data)
        return int(data.get("ymoney", 0))
    except Exception:
        return 0


def get_items(client: Client) -> dict:
    try:
        return parse_item_rows(client.save.get("ywp_user_item"))
    except Exception:
        return {}


def get_ypoint(client: Client):
    items = get_items(client)
    if not items:
        return None
    return items.get(YPOINT_ITEM_ID, 0)


def fmt_gain(value) -> str:
    if value is None:
        return "?"
    return f"{value:+,}" if value else "0"


def event_point_name(client: Client) -> str:
    try:
        for e in (client.save.get("ywp_mst_event") or []):
            for part in str(e.get("generalStringParam12") or "").split(","):
                if part.startswith("spPointName:"):
                    name = part.split(":", 1)[1].strip()
                    if name:
                        return name
    except Exception:
        pass
    return "イベントP"


class GainTracker:
    def __init__(self, client: Client):
        self.client = client
        self.items_prev = get_items(client)
        self.money_start = get_ymoney(client)
        self.point_name = event_point_name(client)
        self.money_gain = 0
        self.exp_gain = 0
        self.event_point = 0
        self.event_sub_point = 0
        self.score_total = 0
        self.item_gain = {}
        self.last_money = None
        self.counted = 0
        self.live = None

    def _apply_items(self):
        items = get_items(self.client)
        if not items:
            return
        for iid, cnt in items.items():
            diff = cnt - self.items_prev.get(iid, 0)
            if diff:
                self.item_gain[iid] = self.item_gain.get(iid, 0) + diff
        self.items_prev = items

    async def after_battle(self, result, user_id=None):
        if not isinstance(result, dict):
            return
        game = result.get("userGameResultData") or {}
        if self.live is None:
            self.live = bool(game)
            if GAIN_DEBUG_DUMP:
                dump_gameend_debug(result)
        money = game.get("money")
        if isinstance(money, (int, float)):
            self.money_gain += int(money)
            self.last_money = int(money)
            self.counted += 1
        exp = game.get("exp")
        if isinstance(exp, (int, float)):
            self.exp_gain += int(exp)
        score = game.get("score")
        if isinstance(score, (int, float)):
            self.score_total += int(score)
        for key, attr in (("eventPoint", "event_point"), ("eventSubPoint", "event_sub_point")):
            v = result.get(key)
            if isinstance(v, (int, float)) and v:
                setattr(self, attr, getattr(self, attr) + int(v))
        self._apply_items()

    async def refresh(self, batches=None):
        try:
            await asyncio.to_thread(self.client.login, self.client.userId)
        except Exception:
            return
        self._apply_items()

    def per_stage(self):
        if not self.counted:
            return None
        return self.money_gain / self.counted

    def to_dict(self):
        return {
            "money_gain": self.money_gain,
            "money_last": self.last_money,
            "money_counted": self.counted,
            "exp_gain": self.exp_gain,
            "event_point": self.event_point,
            "point_name": self.point_name,
            "event_sub_point": self.event_sub_point,
            "score_total": self.score_total,
            "item_gain": {str(k): v for k, v in self.item_gain.items() if v},
        }


def dump_gameend_debug(result):
    try:
        if os.path.exists(DEBUG_GAMEEND_FILE):
            return
        if not isinstance(result, dict):
            return
        info = {
            "saved_at": datetime.now(JST).isoformat(),
            "top_level_keys": sorted(result.keys()),
            "ywp_user_item": str(result.get("ywp_user_item"))[:2000],
            "ywp_user_data": str(result.get("ywp_user_data"))[:2000],
        }
        save_json(DEBUG_GAMEEND_FILE, info)
    except Exception:
        pass


def apply_gain_to_loop(loop_data: dict, tracker):
    if tracker is not None:
        loop_data.update(tracker.to_dict())


def gain_fields(embed: discord.Embed, data: dict, done: bool = False):
    buy_count = data.get("buy_count")
    if buy_count:
        limit = AUTO_BUY_MAX_PER_LOOP or "∞"
        embed.add_field(
            name="💠 人魂購入",
            value=f"**{buy_count} / {limit}** 回\n-{data.get('buy_ym', 0):,} YM",
            inline=True
        )
    gain = data.get("money_gain")
    if gain is None:
        return embed
    counted = data.get("money_counted") or 0
    last = data.get("money_last")
    avg = (gain / counted) if counted else None
    value = f"**{fmt_gain(gain)}**"
    if isinstance(last, int):
        value += f"\n直近: {last:+,}"
    embed.add_field(name="💰 稼いだマネー", value=value, inline=True)
    embed.add_field(
        name="📐 1ステあたり",
        value=(f"**{avg:,.1f}** マネー" if avg is not None else "計測中..."),
        inline=True
    )
    ep = data.get("event_point") or 0
    if ep:
        sub = data.get("event_sub_point") or 0
        v = f"**{ep:,}**" + (f"\nサブ: {sub:,}" if sub else "")
        name = data.get("point_name") or "イベントP"
        embed.add_field(name=f"🎪 {name}", value=v, inline=True)
    exp = data.get("exp_gain") or 0
    if exp and done:
        embed.add_field(name="⭐ 経験値", value=f"{exp:,}", inline=True)
    drops = {k: v for k, v in (data.get("item_gain") or {}).items() if v}
    if drops and done:
        top = sorted(drops.items(), key=lambda kv: -abs(kv[1]))[:6]
        embed.add_field(
            name="🎁 アイテム増減",
            value=" / ".join(f"`{k}`: {v:+,}" for k, v in top),
            inline=False
        )
    return embed


async def auto_buy_hitodama(client: Client, goods_id: int = None):
    candidates = [goods_id] if goods_id is not None else list(AUTO_BUY_GOODS_IDS)
    attempts = []
    for gid in candidates:
        try:
            rc, result = await asyncio.to_thread(
                client.call, "buyHitodama.nhn", {"goodsId": gid}
            )
            if not isinstance(result, dict):
                result = {}
            rc_code = result.get("resultCode")
            msg = result.get("dialogMsg") or result.get("_error") or result.get("_raw") or ""
            attempts.append({"goodsId": gid, "http": rc, "resultCode": rc_code,
                             "dialogTitle": result.get("dialogTitle", ""),
                             "dialogMsg": str(msg)[:300]})
            if rc_code == 0:
                try:
                    await asyncio.to_thread(client.login, client.userId)
                except Exception:
                    pass
                return True, {"attempts": attempts}
        except Exception as e:
            attempts.append({"goodsId": gid, "error": str(e)[:200]})
    info = {
        "attempts": attempts,
        "saved_at": datetime.now(JST).isoformat(),
        "ymoney": get_ymoney(client),
        "hitodama": get_hitodama_detail(client),
        "hitodamaShopSaleList": client.save.get("hitodamaShopSaleList"),
        "ymoneyShopSaleList": client.save.get("ymoneyShopSaleList"),
        "monthlyPurchasableLeft": client.save.get("monthlyPurchasableLeft"),
    }
    try:
        save_json(DEBUG_BUY_FILE, info)
    except Exception:
        pass
    return False, info


async def try_auto_buy(client: Client, buy_count: int):
    if not AUTO_BUY_HITODAMA:
        return False, {"skipped": "自動購入が OFF"}, buy_count
    if AUTO_BUY_MAX_PER_LOOP and buy_count >= AUTO_BUY_MAX_PER_LOOP:
        return False, {"skipped": f"この周回の購入上限 {AUTO_BUY_MAX_PER_LOOP}回に到達"}, buy_count
    ym = get_ymoney(client)
    if ym - AUTO_BUY_COST_YM < AUTO_BUY_KEEP_YMONEY:
        return False, {"skipped": f"YM残 {ym:,} — 温存ライン {AUTO_BUY_KEEP_YMONEY:,} を割るため見送り"}, buy_count
    ok, info = await auto_buy_hitodama(client)
    if ok:
        buy_count += 1
    return ok, info, buy_count


def describe_buy_failure(info) -> str:
    if info and info.get("skipped"):
        return f"・購入を見送りました: {info['skipped']}"
    if not info or not info.get("attempts"):
        return "不明（レスポンスなし）"
    lines = []
    for a in info["attempts"][:4]:
        if "error" in a:
            lines.append(f"・goodsId `{a['goodsId']}` → 例外: {a['error'][:80]}")
            continue
        rc_code = a.get("resultCode")
        desc = RC.get(rc_code, "未知のコード")
        msg = a.get("dialogMsg") or ""
        line = f"・goodsId `{a['goodsId']}` → rc=`{rc_code}` ({desc})"
        if msg:
            line += f"\n　{msg[:120]}"
        lines.append(line)
    return "\n".join(lines)


# ============================================================
# イベントID遷移
# ============================================================
def next_block_start(current_id: int) -> int:
    s = str(current_id)
    if len(s) < 7:
        return current_id + 1
    block = int(s[-4])
    prefix = s[:-4]
    next_block = block + 1
    return int(f"{prefix}{next_block}001")


# ============================================================
# DM通知View
# ============================================================
class ResumeView(ui.View):
    def __init__(self, user_id, loop_key, stage_id, count, current,
                 request_delay, cooldown, end_at=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.loop_key = loop_key
        self.stage_id = stage_id
        self.count = count
        self.current = current
        self.request_delay = request_delay
        self.cooldown = cooldown
        self.end_at = end_at

    @ui.button(label="▶️ 周回再開", style=discord.ButtonStyle.success, emoji="▶️", custom_id="hitodama_resume:resume")
    async def resume(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ あなたの通知ではありません。", ephemeral=True)
        if not await safe_defer(interaction):
            return
        acc = get_active_account(self.user_id)
        if not acc:
            return await safe_reply(interaction, content="❌ セッションがありません。", ephemeral=True)
        client = build_client_from_account(acc)
        try:
            await asyncio.to_thread(client.login, acc["userId"])
            update_account_tokens(self.user_id, client)
        except Exception as e:
            return await safe_reply(interaction, content=f"❌ ログイン失敗: `{str(e)[:200]}`", ephemeral=True)
        current_hitodama = get_hitodama(client)
        if current_hitodama < HITODAMA_RESUME_MIN:
            return await safe_reply(
                interaction,
                content=f"❌ 人魂がまだ足りません。\n**現在**: {current_hitodama} / 必要: {HITODAMA_RESUME_MIN}",
                ephemeral=True
            )
        loops = load_loops()
        if self.loop_key in loops:
            loops[self.loop_key]["status"] = "running"
            await save_loops_async(loops)
        remaining = self.count - self.current
        if remaining <= 0:
            return await safe_reply(interaction, content="✅ 周回は既に完了しています。", ephemeral=True)
        embed = discord.Embed(
            title="▶️ 周回を再開します",
            description=f"**人魂**: {current_hitodama}\n**残り回数**: {remaining}回\n**ステージ**: `{self.stage_id}`",
            color=0x00ff88
        )
        await safe_reply(interaction, embed=embed, ephemeral=False)
        asyncio.create_task(run_farm_dm(
            interaction.client, interaction, self.user_id,
            self.stage_id, self.count, self.current + 1,
            self.request_delay, self.cooldown, self.end_at
        ))

    @ui.button(label="🛑 完全停止", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="hitodama_resume:stop")
    async def stop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ あなたの通知ではありません。", ephemeral=True)
        if not await safe_defer(interaction):
            return
        loops = load_loops()
        if self.loop_key in loops:
            del loops[self.loop_key]
            await save_loops_async(loops)
        await safe_reply(interaction, content="🛑 周回を完全停止しました。", ephemeral=True)


# ============================================================
# DM通知
# ============================================================
async def send_locked_dm(bot, user_id, stage_id, player_name=None, streak=0):
    try:
        user = await bot.fetch_user(user_id)
    except Exception:
        return
    embed = discord.Embed(
        title="⛔ ステージに入れないため停止しました",
        description=(
            f"**ステージ**: `{stage_id}`" + chr(10)
            + f"**アカウント**: {player_name or '不明'}" + chr(10)
            + f"**症状**: `rc=5`（条件未達）が {streak} 周連続" + chr(10) + chr(10)
            + "【よくある原因】" + chr(10)
            + "・**操作中の垢が違う**（👥 アカウント で切替）" + chr(10)
            + "・そのステージの解放条件を満たしていない" + chr(10)
            + "・イベントが終了している" + chr(10)
        ),
        color=0xff4444,
        timestamp=datetime.now(JST)
    )
    try:
        await user.send(embed=embed)
    except Exception:
        pass


async def send_hitodama_dm(bot, user_id, loop_key, stage_id, count, current,
                           request_delay, cooldown, current_hitodama, end_at=None,
                           buy_info=None):
    try:
        user = await bot.fetch_user(user_id)
    except Exception:
        return
    acc = get_active_account(user_id)
    ymoney = "?"
    detail = None
    if acc:
        try:
            client = build_client_from_account(acc)
            await asyncio.to_thread(client.login, acc["userId"])
            ymoney = get_ymoney(client)
            detail = get_hitodama_detail(client)
        except Exception:
            pass
    if detail:
        hitodama_line = f"{detail['total']}（購入分 {detail['paid']} / 無料 {detail['free']}）"
    else:
        hitodama_line = str(current_hitodama)
    embed = discord.Embed(
        title="⚠️ 人魂自動購入失敗",
        description=(
            f"**現在の人魂**: {hitodama_line}\n"
            f"**YM**: {ymoney}\n"
            f"**ステージ**: `{stage_id}`\n"
            f"**進捗**: {current} / {count if count < 999999 else '日時まで'}\n\n"
            f"【サーバーの応答】\n{describe_buy_failure(buy_info)}\n\n"
            f"【対応】\nぷにぷに側で人魂を補充して「▶️ 周回再開」を押してください"
        ),
        color=0xff4444,
        timestamp=datetime.now(JST)
    )
    view = ResumeView(
        user_id=user_id, loop_key=loop_key, stage_id=stage_id,
        count=count, current=current,
        request_delay=request_delay, cooldown=cooldown, end_at=end_at
    )
    try:
        await user.send(embed=embed, view=view)
    except discord.Forbidden:
        pass
    except Exception:
        pass


# ============================================================
# モーダル群
# ============================================================
class LoginModal(ui.Modal, title="ぷにぷに ログイン"):
    email_input = ui.TextInput(label="メールアドレス", placeholder="example@mail.com", required=True, max_length=200)
    password_input = ui.TextInput(label="パスワード", placeholder="パスワードを入力", required=True, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        await safe_reply(interaction, content="🔐 ログイン処理中...\n（検出した全垢を自動保存します／10〜30秒）", ephemeral=True)
        email = self.email_input.value.strip()
        password = self.password_input.value
        try:
            c = await asyncio.to_thread(login_email, email, password)
        except Exception as e:
            return await safe_reply(interaction, content=f"❌ ログイン失敗: `{str(e)[:300]}`", ephemeral=True)
        info = c.info()
        player_name = info.get("playerName", "不明")
        await add_account(interaction.user.id, c, player_name)
        self.password_input = None
        accounts = get_user_accounts(interaction.user.id)
        embed = discord.Embed(title="✅ ログイン成功", color=0x00ff88, timestamp=datetime.now(JST))
        embed.add_field(name="👤 プレイヤー名", value=player_name, inline=True)
        embed.add_field(name="🆔 ユーザーID", value=str(c.userId), inline=True)
        embed.add_field(name="👥 登録済み", value=f"{len(accounts)} 垢（この垢を選択中）", inline=True)
        await safe_reply(interaction, embed=embed, ephemeral=True)


class FarmModal(ui.Modal, title="ぷにぷに 自動周回"):
    stage_input = ui.TextInput(label="ステージID", placeholder="例: 1001001", required=True, max_length=20)
    count_input = ui.TextInput(label="周回回数（任意）", placeholder="日時指定なら空欄OK", required=False, max_length=6)
    end_at_input = ui.TextInput(label="終了日時（任意）", placeholder="例: 2026-09-20 21:00", required=False, max_length=20)
    request_delay_input = ui.TextInput(label="リクエスト前待機(秒)", placeholder="空欄=4.5〜6.0ランダム", default="", required=False, max_length=10)
    cooldown_input = ui.TextInput(label="クールダウン(秒)", placeholder="空欄=4.5〜6.0ランダム", default="", required=False, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        await cleanup_user_loops(interaction.user.id)

        acc = get_active_account(interaction.user.id)
        if not acc:
            return await safe_reply(interaction, content="❌ ログインしていません。", ephemeral=True)

        try:
            stage_id = int(self.stage_input.value.strip())
        except ValueError:
            return await safe_reply(interaction, content="❌ ステージIDが不正です。", ephemeral=True)

        count = None
        raw_count = self.count_input.value.strip() if self.count_input.value else ""
        if raw_count:
            try:
                count = int(raw_count)
                if count < 1 or count > 999999:
                    raise ValueError
            except ValueError:
                return await safe_reply(interaction, content="❌ 回数は 1〜999999 の整数で指定してください。", ephemeral=True)

        end_at = None
        end_at_str = ""
        raw_end = self.end_at_input.value.strip() if self.end_at_input.value else ""
        if raw_end:
            end_at = parse_datetime(raw_end)
            if end_at is None:
                return await safe_reply(interaction, content="❌ 終了日時の形式が不正です。\n**形式**: `YYYY-MM-DD HH:MM`", ephemeral=True)
            if end_at.timestamp() <= time.time():
                return await safe_reply(interaction, content="❌ 終了日時は未来の時刻を指定してください。", ephemeral=True)
            end_at_str = end_at.strftime("%Y-%m-%d %H:%M")

        if count is None and end_at is None:
            return await safe_reply(interaction, content="❌ **「周回回数」or「終了日時」のどちらか一方を入力してください。**", ephemeral=True)
        if count is None:
            count = 999999

        rd_raw = self.request_delay_input.value.strip() if self.request_delay_input.value else ""
        if rd_raw:
            try:
                request_delay = float(rd_raw)
                if request_delay < 0 or request_delay > 60:
                    raise ValueError
                use_random_rd = False
            except ValueError:
                return await safe_reply(interaction, content="❌ リクエスト前待機は 0〜60 秒で指定してください。", ephemeral=True)
        else:
            request_delay = 0
            use_random_rd = True

        cd_raw = self.cooldown_input.value.strip() if self.cooldown_input.value else ""
        if cd_raw:
            try:
                cooldown = float(cd_raw)
                if cooldown < 0 or cooldown > 60:
                    raise ValueError
                use_random_cd = False
            except ValueError:
                return await safe_reply(interaction, content="❌ クールダウンは 0〜60 秒で指定してください。", ephemeral=True)
        else:
            cooldown = 0
            use_random_cd = True

        ok, current = can_start_loop()
        if not ok:
            return await safe_reply(interaction, content=f"❌ **同時実行上限に達しています** ({current}/{MAX_CONCURRENT_LOOPS})", ephemeral=True)

        active_id = get_active_id(interaction.user.id)
        if account_busy(active_id):
            sd_busy = get_session(interaction.user.id, active_id)
            return await safe_reply(
                interaction,
                content=(f"❌ **{(sd_busy or {}).get('player_name', '不明')}** は既に周回中です。"
                         + chr(10) + "別の垢で回すなら 👥 アカウント で切り替えてください。"),
                ephemeral=True)

        count_str = f"{count}回" if count < 999999 else "日時まで"
        end_str = end_at_str if end_at_str else "なし"

        desc = (
            f"**垢**: {acc.get('player_name', '不明')} (`{acc.get('player_id')}`)\n"
            f"**ステージ**: `{stage_id}`\n"
            f"**回数**: {count_str}\n"
            f"**終了日時**: {end_str}\n"
            f"**同時実行**: {current + 1}/{MAX_CONCURRENT_LOOPS}"
        )
        embed = discord.Embed(title="🚀 周回を開始します", description=desc, color=0x5865F2)
        await safe_reply(interaction, embed=embed, ephemeral=True)

        asyncio.create_task(run_farm(
            interaction.client, interaction, interaction.guild_id, interaction.user.id,
            stage_id, count, request_delay, cooldown,
            end_at.timestamp() if end_at else None,
            use_random_rd, use_random_cd,
            get_active_id(interaction.user.id)
        ))


class ProgressModal(ui.Modal, title="ステージ進行"):
    start_input = ui.TextInput(label="開始ステージID", placeholder="例: 1001001", required=True, max_length=20)
    end_input = ui.TextInput(label="終了ステージID", placeholder="例: 1001100", required=True, max_length=20)
    end_at_input = ui.TextInput(label="終了日時（任意）", placeholder="例: 2026-09-20 21:00", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        await cleanup_user_loops(interaction.user.id)

        acc = get_active_account(interaction.user.id)
        if not acc:
            return await safe_reply(interaction, content="❌ ログインしていません。", ephemeral=True)

        try:
            start_id = int(self.start_input.value.strip())
        except ValueError:
            return await safe_reply(interaction, content="❌ 開始ステージIDが不正です。", ephemeral=True)
        try:
            end_id = int(self.end_input.value.strip())
        except ValueError:
            return await safe_reply(interaction, content="❌ 終了ステージIDが不正です。", ephemeral=True)

        if end_id < start_id:
            return await safe_reply(interaction, content="❌ 終了IDは開始ID以上にしてください。", ephemeral=True)
        if end_id - start_id > 1000:
            return await safe_reply(interaction, content="❌ 範囲が広すぎます（最大1000ステージ）", ephemeral=True)

        end_at = None
        end_at_str = ""
        raw_end = self.end_at_input.value.strip() if self.end_at_input.value else ""
        if raw_end:
            end_at = parse_datetime(raw_end)
            if end_at is None:
                return await safe_reply(interaction, content="❌ 終了日時の形式が不正です。", ephemeral=True)
            if end_at.timestamp() <= time.time():
                return await safe_reply(interaction, content="❌ 終了日時は未来の時刻を指定してください。", ephemeral=True)
            end_at_str = end_at.strftime("%Y-%m-%d %H:%M")

        ok, current = can_start_loop()
        if not ok:
            return await safe_reply(interaction, content=f"❌ **同時実行上限** ({current}/{MAX_CONCURRENT_LOOPS})", ephemeral=True)

        active_id = get_active_id(interaction.user.id)
        if account_busy(active_id):
            sd_busy = get_session(interaction.user.id, active_id)
            return await safe_reply(
                interaction,
                content=(f"❌ **{(sd_busy or {}).get('player_name', '不明')}** は既に周回中です。"
                         + chr(10) + "別の垢で回すなら 👥 アカウント で切り替えてください。"),
                ephemeral=True)

        desc = (
            f"**垢**: {acc.get('player_name', '不明')} (`{acc.get('player_id')}`)\n"
            f"**開始**: `{start_id}`\n"
            f"**終了**: `{end_id}`\n"
            f"**終了日時**: {end_at_str if end_at_str else 'なし'}\n"
            f"**同時実行**: {current + 1}/{MAX_CONCURRENT_LOOPS}"
        )
        embed = discord.Embed(title="🎯 ステージ進行を開始します", description=desc, color=0x5865F2)
        await safe_reply(interaction, embed=embed, ephemeral=True)
        asyncio.create_task(run_progress(
            interaction.client, interaction, interaction.guild_id, interaction.user.id,
            start_id, end_id,
            end_at.timestamp() if end_at else None,
            get_active_id(interaction.user.id)
        ))


class EventProgressModal(ui.Modal, title="イベント自動進行"):
    start_input = ui.TextInput(label="開始ID", placeholder="例: 29701001", required=True, max_length=20)
    end_at_input = ui.TextInput(label="終了日時（任意）", placeholder="例: 2026-09-20 21:00", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        await cleanup_user_loops(interaction.user.id)

        acc = get_active_account(interaction.user.id)
        if not acc:
            return await safe_reply(interaction, content="❌ ログインしていません。", ephemeral=True)

        try:
            start_id = int(self.start_input.value.strip())
        except ValueError:
            return await safe_reply(interaction, content="❌ 開始IDが不正です。", ephemeral=True)

        end_at = None
        end_at_str = ""
        raw_end = self.end_at_input.value.strip() if self.end_at_input.value else ""
        if raw_end:
            end_at = parse_datetime(raw_end)
            if end_at is None:
                return await safe_reply(interaction, content="❌ 終了日時の形式が不正です。", ephemeral=True)
            if end_at.timestamp() <= time.time():
                return await safe_reply(interaction, content="❌ 終了日時は未来の時刻を指定してください。", ephemeral=True)
            end_at_str = end_at.strftime("%Y-%m-%d %H:%M")

        ok, current = can_start_loop()
        if not ok:
            return await safe_reply(interaction, content=f"❌ **同時実行上限** ({current}/{MAX_CONCURRENT_LOOPS})", ephemeral=True)

        active_id = get_active_id(interaction.user.id)
        if account_busy(active_id):
            sd_busy = get_session(interaction.user.id, active_id)
            return await safe_reply(
                interaction,
                content=(f"❌ **{(sd_busy or {}).get('player_name', '不明')}** は既に周回中です。"
                         + chr(10) + "別の垢で回すなら 👥 アカウント で切り替えてください。"),
                ephemeral=True)

        desc = (
            f"**垢**: {acc.get('player_name', '不明')} (`{acc.get('player_id')}`)\n"
            f"**開始ID**: `{start_id}`\n"
            f"**終了日時**: {end_at_str if end_at_str else 'なし'}\n"
            f"**動作**: エラーで次ブロック001へ自動ジャンプ\n"
            f"**同時実行**: {current + 1}/{MAX_CONCURRENT_LOOPS}"
        )
        embed = discord.Embed(title="🎪 イベント自動進行を開始します", description=desc, color=0xE67E22)
        await safe_reply(interaction, embed=embed, ephemeral=True)
        asyncio.create_task(run_event_progress(
            interaction.client, interaction, interaction.guild_id, interaction.user.id,
            start_id, end_at.timestamp() if end_at else None,
            get_active_id(interaction.user.id)
        ))


class BenchmarkModal(ui.Modal, title="効率計測"):
    stages_input = ui.TextInput(
        label="ステージID（複数可）",
        placeholder="例: 1001015 1001020 / 1001015-1001018",
        required=True, max_length=200
    )
    samples_input = ui.TextInput(
        label="1ステージあたりの回数",
        placeholder=f"1〜{BENCH_MAX_SAMPLES}（既定 3）",
        default="3", required=False, max_length=3
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        await cleanup_user_loops(interaction.user.id)

        acc = get_active_account(interaction.user.id)
        if not acc:
            return await safe_reply(interaction, content="❌ ログインしていません。", ephemeral=True)

        try:
            stage_ids = parse_stage_list(self.stages_input.value)
        except ValueError as e:
            return await safe_reply(interaction, content=f"❌ ステージIDが不正です（{e}）", ephemeral=True)
        if not stage_ids:
            return await safe_reply(interaction, content="❌ ステージIDを入力してください。", ephemeral=True)
        if len(stage_ids) > BENCH_MAX_STAGES:
            return await safe_reply(interaction, content=f"❌ ステージが多すぎます（最大{BENCH_MAX_STAGES}件）", ephemeral=True)

        raw = (self.samples_input.value or "3").strip()
        try:
            samples = int(raw) if raw else 3
        except ValueError:
            return await safe_reply(interaction, content="❌ 回数は数字で入力してください。", ephemeral=True)
        if not (1 <= samples <= BENCH_MAX_SAMPLES):
            return await safe_reply(interaction, content=f"❌ 回数は1〜{BENCH_MAX_SAMPLES}にしてください。", ephemeral=True)

        ok, current = can_start_loop()
        if not ok:
            return await safe_reply(interaction, content=f"❌ **同時実行上限** ({current}/{MAX_CONCURRENT_LOOPS})", ephemeral=True)

        active_id = get_active_id(interaction.user.id)
        if account_busy(active_id):
            sd_busy = get_session(interaction.user.id, active_id)
            return await safe_reply(
                interaction,
                content=(f"❌ **{(sd_busy or {}).get('player_name', '不明')}** は既に周回中です。"
                         + chr(10) + "別の垢で回すなら 👥 アカウント で切り替えてください。"),
                ephemeral=True)

        total = len(stage_ids) * samples
        embed = discord.Embed(
            title="📐 効率計測を開始します",
            description=(
                f"**ステージ**: {' '.join(f'`{s}`' for s in stage_ids)}\n"
                f"**回数**: 各 {samples} 回（合計 {total} 周）\n"
                f"**消費人魂**: 最大 {total} 個程度\n"
                f"**目安時間**: 約 {total * 20 // 60 + 1} 分"
            ),
            color=0x1ABC9C
        )
        await safe_reply(interaction, embed=embed, ephemeral=True)
        asyncio.create_task(run_benchmark(
            interaction.client, interaction, interaction.guild_id,
            interaction.user.id, stage_ids, samples,
            get_active_id(interaction.user.id)
        ))


# ============================================================
# 周回タスク
# ============================================================
async def run_farm(bot, interaction, guild_id, user_id, stage_id, count,
                   request_delay, cooldown, end_at=None,
                   use_random_rd=True, use_random_cd=True, account_id=None):
    loop_key = f"{user_id}_{stage_id}_{int(time.time())}"
    try:
        sdata = get_session(user_id, account_id)
        account_id = str((sdata or {}).get("userId") or account_id or "")
        if not sdata:
            await safe_reply(interaction, content="❌ セッションなし", ephemeral=True)
            return
        client = build_client_from_account(sdata)
        try:
            await asyncio.to_thread(client.login, sdata["userId"])
            await update_token(user_id, account_id, client)
        except Exception as e:
            await safe_reply(interaction, content=f"❌ 再ログイン失敗: `{str(e)[:300]}`", ephemeral=True)
            return

        tracker = GainTracker(client)
        buy_count = 0
        rc5_stage, rc5_streak = None, 0

        loops = load_loops()
        loops[loop_key] = {
            "type": "farm", "user_id": user_id, "stage_id": stage_id,
            "count": count, "current": 0, "success": 0, "fail": 0,
            "account_id": account_id, "player_name": (sdata or {}).get("player_name"),
            "status": "running", "request_delay": request_delay,
            "cooldown": cooldown, "end_at": end_at,
            "buy_count": 0, "buy_ym": 0,
            "started_at": datetime.now(JST).isoformat()
        }
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        embed = build_progress_embed(loop_key, loops[loop_key], "🔄 周回中...")
        try:
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception:
            pass

        start_time = time.time()
        last_update = 0
        end_reason = None
        consecutive_errors = 0

        for i in range(1, count + 1):
            loops = load_loops()
            if loop_key not in loops or loops.get(loop_key, {}).get("status") == "stopped":
                end_reason = "ユーザー停止"; break
            if end_at and time.time() >= end_at:
                end_reason = "指定日時到達"; break

            await check_odd_hour_rest(loop_key)

            current_hitodama = get_hitodama(client)
            if AUTO_BUY_HITODAMA and current_hitodama < HITODAMA_THRESHOLD:
                bought, buy_info, buy_count = await try_auto_buy(client, buy_count)
                if bought:
                    loops[loop_key]["buy_count"] = buy_count
                    loops[loop_key]["buy_ym"] = buy_count * AUTO_BUY_COST_YM
                    new_hitodama = get_hitodama(client)
                    embed = build_progress_embed(loop_key, loops[loop_key], f"💠 人魂補充 ({current_hitodama} → {new_hitodama})")
                    try:
                        await interaction.edit_original_response(embed=embed)
                    except Exception:
                        pass
                else:
                    await tracker.refresh()
                    apply_gain_to_loop(loops[loop_key], tracker)
                    loops[loop_key]["status"] = "paused_hitodama"
                    await save_loops_async(loops)
                    await send_hitodama_dm(bot, user_id, loop_key, stage_id, count, i - 1,
                                           request_delay, cooldown, current_hitodama, end_at, buy_info=buy_info)
                    embed = discord.Embed(
                        title="⚠️ 人魂不足で周回停止（自動購入失敗）",
                        description=f"**現在の人魂**: {current_hitodama}\n**進捗**: {i - 1} / {count}\n\nDMを確認",
                        color=0xff4444
                    )
                    try:
                        await interaction.edit_original_response(content=None, embed=embed)
                    except Exception:
                        pass
                    return

            rd = rand_request_delay() if use_random_rd else request_delay
            print(f">>> [{i}/{count}] 待機 {rd:.2f}秒")
            await asyncio.sleep(rd)

            try:
                for retry_idx in range(len(RETRY_WAITS) + 1):
                    rc, result = await asyncio.to_thread(client.battle, stage_id)
                    result_code = result.get("resultCode")
                    print(f">>> battle rc={rc} result_code={result_code} (retry={retry_idx})")
                    if result_code == 0:
                        loops[loop_key]["success"] += 1
                        consecutive_errors = 0
                        await tracker.after_battle(result)
                        apply_gain_to_loop(loops[loop_key], tracker)
                        break
                    if result_code == 32:
                        try:
                            await asyncio.to_thread(client.login, sdata["userId"])
                            await update_token(user_id, account_id, client)
                            continue
                        except Exception:
                            pass
                    if (result_code == LOCKED_RC and rc5_streak >= 1):
                        pass
                    elif result_code in RETRY_CODES and retry_idx < len(RETRY_WAITS):
                        await asyncio.sleep(RETRY_WAITS[retry_idx])
                        continue

                    loops[loop_key]["fail"] += 1
                    if result_code == LOCKED_RC:
                        cur_stage = loops[loop_key].get("current_stage") or loops[loop_key].get("stage_id")
                        if rc5_stage == cur_stage:
                            rc5_streak += 1
                        else:
                            rc5_stage, rc5_streak = cur_stage, 1
                        if rc5_streak >= LOCKED_STAGE_LIMIT:
                            loops[loop_key]["status"] = "fatal"
                            await save_loops_async(loops)
                            end_reason = f"ステージ `{cur_stage}` に入れません（rc=5 が{rc5_streak}周連続）"
                            await send_locked_dm(bot, user_id, cur_stage, loops[loop_key].get("player_name"), rc5_streak)
                            break
                    else:
                        rc5_streak = 0
                    if result_code in (202, 30):
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"致命的エラー (rc={result_code})"
                        break
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回 (最後のrc={result_code})"
                        break
                    break
                if end_reason:
                    break
            except Exception as e:
                loops[loop_key]["fail"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    loops[loop_key]["status"] = "fatal"
                    await save_loops_async(loops)
                    end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回（例外）"
                    break

            loops[loop_key]["current"] = i
            await save_loops_async(loops)

            now = time.time()
            if now - last_update >= 3 or i == count:
                embed = build_progress_embed(loop_key, loops[loop_key], "🔄 周回中...")
                try:
                    await interaction.edit_original_response(embed=embed)
                except Exception:
                    pass
                last_update = now

            if i < count:
                cd = rand_cooldown() if use_random_cd else cooldown
                print(f">>> [{i}/{count}] クールダウン {cd:.2f}秒")
                await asyncio.sleep(cd)

        await tracker.refresh()
        loops = load_loops()
        loops[loop_key]["status"] = "done"
        loops[loop_key]["end_reason"] = end_reason or "全回数完了"
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        elapsed = time.time() - start_time
        embed = build_progress_embed(loop_key, loops[loop_key], "✅ 完了", elapsed)
        embed.add_field(name="🏁 終了理由", value=end_reason or "全回数完了", inline=False)
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass
    except Exception as e:
        print(f">>> run_farm 致命的: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await finalize_loop(loop_key)


async def run_progress(bot, interaction, guild_id, user_id, start_id, end_id, end_at=None, account_id=None):
    loop_key = f"{user_id}_progress_{int(time.time())}"
    try:
        sdata = get_session(user_id, account_id)
        account_id = str((sdata or {}).get("userId") or account_id or "")
        if not sdata:
            await safe_reply(interaction, content="❌ セッションなし", ephemeral=True)
            return
        client = build_client_from_account(sdata)
        try:
            await asyncio.to_thread(client.login, sdata["userId"])
            await update_token(user_id, account_id, client)
        except Exception as e:
            await safe_reply(interaction, content=f"❌ 再ログイン失敗: `{str(e)[:300]}`", ephemeral=True)
            return
        target_ids = [sid for sid in range(start_id, end_id + 1)]
        if not target_ids:
            return await safe_reply(interaction, content="❌ 範囲内にステージがありません。", ephemeral=True)
        total = len(target_ids)
        tracker = GainTracker(client)
        buy_count = 0
        rc5_stage, rc5_streak = None, 0

        loops = load_loops()
        loops[loop_key] = {
            "type": "progress", "user_id": user_id,
            "start_id": start_id, "end_id": end_id, "total": total,
            "current": 0, "success": 0, "fail": 0,
            "account_id": account_id, "player_name": (sdata or {}).get("player_name"),
            "status": "running", "end_at": end_at,
            "current_stage": target_ids[0],
            "buy_count": 0, "buy_ym": 0,
            "started_at": datetime.now(JST).isoformat()
        }
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        embed = build_progress_embed(loop_key, loops[loop_key], "🎯 ステージ進行中...")
        try:
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception:
            pass

        start_time = time.time()
        last_update = 0
        end_reason = None
        consecutive_errors = 0

        for idx, stage_id in enumerate(target_ids, 1):
            loops = load_loops()
            if loop_key not in loops or loops.get(loop_key, {}).get("status") == "stopped":
                end_reason = "ユーザー停止"; break
            if end_at and time.time() >= end_at:
                end_reason = "指定日時到達"; break

            await check_odd_hour_rest(loop_key)

            current_hitodama = get_hitodama(client)
            if AUTO_BUY_HITODAMA and current_hitodama < HITODAMA_THRESHOLD:
                bought, buy_info, buy_count = await try_auto_buy(client, buy_count)
                if bought:
                    loops[loop_key]["buy_count"] = buy_count
                    loops[loop_key]["buy_ym"] = buy_count * AUTO_BUY_COST_YM
                    new_hitodama = get_hitodama(client)
                    embed = build_progress_embed(loop_key, loops[loop_key], f"💠 人魂補充 ({current_hitodama} → {new_hitodama})")
                    try:
                        await interaction.edit_original_response(embed=embed)
                    except Exception:
                        pass
                else:
                    await tracker.refresh()
                    apply_gain_to_loop(loops[loop_key], tracker)
                    loops[loop_key]["status"] = "paused_hitodama"
                    await save_loops_async(loops)
                    await send_hitodama_dm(bot, user_id, loop_key, stage_id, total, idx - 1,
                                           0.5, 0.5, current_hitodama, end_at, buy_info=buy_info)
                    embed = discord.Embed(
                        title="⚠️ 人魂不足で進行停止（自動購入失敗）",
                        description=f"**現在の人魂**: {current_hitodama}\n**進捗**: {idx - 1} / {total}\n\nDMを確認",
                        color=0xff4444
                    )
                    try:
                        await interaction.edit_original_response(content=None, embed=embed)
                    except Exception:
                        pass
                    return

            rd = rand_request_delay()
            print(f">>> [{idx}/{total}] ステージ {stage_id} / 待機 {rd:.2f}秒")
            await asyncio.sleep(rd)

            try:
                for retry_idx in range(len(RETRY_WAITS) + 1):
                    rc, result = await asyncio.to_thread(client.battle, stage_id)
                    result_code = result.get("resultCode")
                    if result_code == 0:
                        loops[loop_key]["success"] += 1
                        consecutive_errors = 0
                        await tracker.after_battle(result)
                        apply_gain_to_loop(loops[loop_key], tracker)
                        break
                    if result_code == 32:
                        try:
                            await asyncio.to_thread(client.login, sdata["userId"])
                            await update_token(user_id, account_id, client)
                            continue
                        except Exception:
                            pass
                    if (result_code == LOCKED_RC and rc5_streak >= 1):
                        pass
                    elif result_code in RETRY_CODES and retry_idx < len(RETRY_WAITS):
                        await asyncio.sleep(RETRY_WAITS[retry_idx])
                        continue

                    loops[loop_key]["fail"] += 1
                    if result_code == LOCKED_RC:
                        cur_stage = loops[loop_key].get("current_stage") or loops[loop_key].get("stage_id")
                        if rc5_stage == cur_stage:
                            rc5_streak += 1
                        else:
                            rc5_stage, rc5_streak = cur_stage, 1
                        if rc5_streak >= LOCKED_STAGE_LIMIT:
                            loops[loop_key]["status"] = "fatal"
                            await save_loops_async(loops)
                            end_reason = f"ステージ `{cur_stage}` に入れません（rc=5 が{rc5_streak}周連続）"
                            await send_locked_dm(bot, user_id, cur_stage, loops[loop_key].get("player_name"), rc5_streak)
                            break
                    else:
                        rc5_streak = 0
                    if result_code in (202, 30):
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"致命的エラー (rc={result_code}, stage={stage_id})"
                        break
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回 (最後のstage={stage_id}, rc={result_code})"
                        break
                    break
                if end_reason:
                    break
            except Exception as e:
                loops[loop_key]["fail"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    loops[loop_key]["status"] = "fatal"
                    await save_loops_async(loops)
                    end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回（例外）"
                    break

            loops[loop_key]["current"] = idx
            loops[loop_key]["current_stage"] = stage_id
            await save_loops_async(loops)

            now = time.time()
            if now - last_update >= 3 or idx == total:
                embed = build_progress_embed(loop_key, loops[loop_key], "🎯 ステージ進行中...")
                try:
                    await interaction.edit_original_response(embed=embed)
                except Exception:
                    pass
                last_update = now

            if idx < total:
                cd = rand_cooldown()
                print(f">>> [{idx}/{total}] クールダウン {cd:.2f}秒")
                await asyncio.sleep(cd)

        await tracker.refresh()
        loops = load_loops()
        loops[loop_key]["status"] = "done"
        loops[loop_key]["end_reason"] = end_reason or "全ステージ完了"
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        elapsed = time.time() - start_time
        embed = build_progress_embed(loop_key, loops[loop_key], "✅ 完了", elapsed)
        embed.add_field(name="🏁 終了理由", value=end_reason or "全ステージ完了", inline=False)
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass
    except Exception as e:
        print(f">>> run_progress 致命的: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await finalize_loop(loop_key)


async def run_event_progress(bot, interaction, guild_id, user_id, start_id, end_at=None, account_id=None):
    loop_key = f"{user_id}_event_{int(time.time())}"
    try:
        sdata = get_session(user_id, account_id)
        account_id = str((sdata or {}).get("userId") or account_id or "")
        if not sdata:
            await safe_reply(interaction, content="❌ セッションなし", ephemeral=True)
            return
        client = build_client_from_account(sdata)
        try:
            await asyncio.to_thread(client.login, sdata["userId"])
            await update_token(user_id, account_id, client)
        except Exception as e:
            await safe_reply(interaction, content=f"❌ 再ログイン失敗: `{str(e)[:300]}`", ephemeral=True)
            return
        tracker = GainTracker(client)
        buy_count = 0
        rc5_stage, rc5_streak = None, 0

        loops = load_loops()
        loops[loop_key] = {
            "type": "event", "user_id": user_id,
            "start_id": start_id, "total": 0,
            "current": 0, "success": 0, "fail": 0,
            "account_id": account_id, "player_name": (sdata or {}).get("player_name"),
            "status": "running", "end_at": end_at,
            "current_stage": start_id,
            "buy_count": 0, "buy_ym": 0,
            "started_at": datetime.now(JST).isoformat()
        }
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        embed = build_progress_embed(loop_key, loops[loop_key], "🎪 イベント自動進行中...")
        try:
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception:
            pass

        start_time = time.time()
        last_update = 0
        end_reason = None
        consecutive_errors = 0
        current_id = start_id
        processed = 0

        while True:
            loops = load_loops()
            if loop_key not in loops or loops.get(loop_key, {}).get("status") == "stopped":
                end_reason = "ユーザー停止"; break
            if end_at and time.time() >= end_at:
                end_reason = "指定日時到達"; break

            await check_odd_hour_rest(loop_key)

            current_hitodama = get_hitodama(client)
            if AUTO_BUY_HITODAMA and current_hitodama < HITODAMA_THRESHOLD:
                bought, buy_info, buy_count = await try_auto_buy(client, buy_count)
                if bought:
                    loops[loop_key]["buy_count"] = buy_count
                    loops[loop_key]["buy_ym"] = buy_count * AUTO_BUY_COST_YM
                    new_hitodama = get_hitodama(client)
                    embed = build_progress_embed(loop_key, loops[loop_key], f"💠 人魂補充 ({current_hitodama} → {new_hitodama})")
                    try:
                        await interaction.edit_original_response(embed=embed)
                    except Exception:
                        pass
                else:
                    await tracker.refresh()
                    apply_gain_to_loop(loops[loop_key], tracker)
                    loops[loop_key]["status"] = "paused_hitodama"
                    await save_loops_async(loops)
                    await send_hitodama_dm(bot, user_id, loop_key, current_id, 999999, processed,
                                           rand_request_delay(), rand_cooldown(), current_hitodama, end_at, buy_info=buy_info)
                    embed = discord.Embed(
                        title="⚠️ 人魂不足で進行停止（自動購入失敗）",
                        description=f"**現在の人魂**: {current_hitodama}\n**処理済み**: {processed}\n\nDMを確認",
                        color=0xff4444
                    )
                    try:
                        await interaction.edit_original_response(content=None, embed=embed)
                    except Exception:
                        pass
                    return

            rd = rand_request_delay()
            print(f">>> [{processed + 1}] stage={current_id} / 待機 {rd:.2f}秒")
            await asyncio.sleep(rd)

            result_code = None
            try:
                for retry_idx in range(len(RETRY_WAITS) + 1):
                    rc, result = await asyncio.to_thread(client.battle, current_id)
                    result_code = result.get("resultCode")
                    print(f">>> event battle stage={current_id} rc={rc} result_code={result_code} (retry={retry_idx})")
                    if result_code == 0:
                        loops[loop_key]["success"] += 1
                        await tracker.after_battle(result)
                        apply_gain_to_loop(loops[loop_key], tracker)
                        break
                    if result_code == 32:
                        try:
                            await asyncio.to_thread(client.login, sdata["userId"])
                            await update_token(user_id, account_id, client)
                            continue
                        except Exception:
                            pass
                    if (result_code == LOCKED_RC and rc5_streak >= 1):
                        pass
                    elif result_code in RETRY_CODES and retry_idx < len(RETRY_WAITS):
                        await asyncio.sleep(RETRY_WAITS[retry_idx])
                        continue
                    break
            except Exception:
                result_code = -1

            processed += 1
            loops[loop_key]["current"] = processed
            loops[loop_key]["current_stage"] = current_id
            await save_loops_async(loops)

            now = time.time()
            if now - last_update >= 3:
                embed = build_progress_embed(loop_key, loops[loop_key], "🎪 イベント自動進行中...")
                try:
                    await interaction.edit_original_response(embed=embed)
                except Exception:
                    pass
                last_update = now

            if result_code == 0:
                consecutive_errors = 0
                current_id += 1
            elif result_code in EVENT_SKIP_CODES:
                next_start = next_block_start(current_id)
                print(f">>> {current_id} エラー ({result_code}) → 次ブロック {next_start} へ")

                if next_start <= current_id:
                    end_reason = f"次ブロック計算失敗 (current={current_id})"
                    break

                try:
                    rc2, result2 = await asyncio.to_thread(client.battle, next_start)
                    rc2_code = result2.get("resultCode")
                    print(f">>> ジャンプ先 {next_start} rc={rc2} rc_code={rc2_code}")

                    if rc2_code == 0:
                        loops[loop_key]["success"] += 1
                        await tracker.after_battle(result2)
                        apply_gain_to_loop(loops[loop_key], tracker)
                        processed += 1
                        loops[loop_key]["current"] = processed
                        loops[loop_key]["current_stage"] = next_start
                        await save_loops_async(loops)
                        current_id = next_start + 1
                        continue
                    elif rc2_code in EVENT_SKIP_CODES:
                        end_reason = f"最終ブロック到達 ({next_start} rc={rc2_code})"
                        break
                    else:
                        end_reason = f"ジャンプ先エラー (rc={rc2_code})"
                        break
                except Exception as e:
                    print(f">>> ジャンプ先 例外: {e}")
                    end_reason = f"ジャンプ先例外: {e}"
                    break
            elif result_code in (202, 30):
                loops[loop_key]["status"] = "fatal"
                await save_loops_async(loops)
                end_reason = f"致命的エラー (rc={result_code})"
                break
            else:
                next_start = next_block_start(current_id)
                print(f">>> {current_id} エラー ({result_code}) → 次ブロック {next_start} へ")
                if next_start <= current_id:
                    end_reason = f"次ブロック計算失敗 (rc={result_code})"
                    break
                current_id = next_start
                continue

            cd = rand_cooldown()
            print(f">>> [{processed}] クールダウン {cd:.2f}秒")
            await asyncio.sleep(cd)

        await tracker.refresh()
        loops = load_loops()
        loops[loop_key]["status"] = "done"
        loops[loop_key]["end_reason"] = end_reason or "完了"
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        elapsed = time.time() - start_time
        embed = build_progress_embed(loop_key, loops[loop_key], "✅ 完了", elapsed)
        embed.add_field(name="🏁 終了理由", value=end_reason or "完了", inline=False)
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass
    except Exception as e:
        print(f">>> run_event_progress 致命的: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await finalize_loop(loop_key)


async def run_farm_dm(bot, interaction, user_id, stage_id, count, start_index,
                      request_delay, cooldown, end_at=None, account_id=None):
    loop_key = f"{user_id}_{stage_id}_{int(time.time())}"
    try:
        sdata = get_session(user_id, account_id)
        account_id = str((sdata or {}).get("userId") or account_id or "")
        if not sdata:
            return
        client = build_client_from_account(sdata)
        try:
            await asyncio.to_thread(client.login, sdata["userId"])
        except Exception:
            return
        tracker = GainTracker(client)
        buy_count = 0
        rc5_stage, rc5_streak = None, 0

        loops = load_loops()
        loops[loop_key] = {
            "type": "farm", "user_id": user_id, "stage_id": stage_id,
            "count": count, "current": start_index - 1,
            "success": 0, "fail": 0, "status": "running",
            "account_id": account_id, "player_name": (sdata or {}).get("player_name"),
            "request_delay": request_delay, "cooldown": cooldown,
            "end_at": end_at,
            "buy_count": 0, "buy_ym": 0,
            "started_at": datetime.now(JST).isoformat()
        }
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)

        end_reason = None
        consecutive_errors = 0

        for i in range(start_index, count + 1):
            loops = load_loops()
            if loop_key not in loops or loops.get(loop_key, {}).get("status") == "stopped":
                end_reason = "ユーザー停止"; break
            if end_at and time.time() >= end_at:
                end_reason = "指定日時到達"; break

            await check_odd_hour_rest(loop_key)

            current_hitodama = get_hitodama(client)
            if AUTO_BUY_HITODAMA and current_hitodama < HITODAMA_THRESHOLD:
                bought, buy_info, buy_count = await try_auto_buy(client, buy_count)
                if bought:
                    loops[loop_key]["buy_count"] = buy_count
                    loops[loop_key]["buy_ym"] = buy_count * AUTO_BUY_COST_YM
                else:
                    await tracker.refresh()
                    apply_gain_to_loop(loops[loop_key], tracker)
                    loops[loop_key]["status"] = "paused_hitodama"
                    await save_loops_async(loops)
                    await send_hitodama_dm(bot, user_id, loop_key, stage_id, count, i - 1,
                                           request_delay, cooldown, current_hitodama, end_at, buy_info=buy_info)
                    return

            await asyncio.sleep(rand_request_delay())

            try:
                for retry_idx in range(len(RETRY_WAITS) + 1):
                    rc, result = await asyncio.to_thread(client.battle, stage_id)
                    result_code = result.get("resultCode")
                    if result_code == 0:
                        loops[loop_key]["success"] += 1
                        consecutive_errors = 0
                        await tracker.after_battle(result)
                        apply_gain_to_loop(loops[loop_key], tracker)
                        break
                    if result_code == 32:
                        try:
                            await asyncio.to_thread(client.login, sdata["userId"])
                            await update_token(user_id, account_id, client)
                            continue
                        except Exception:
                            pass
                    if (result_code == LOCKED_RC and rc5_streak >= 1):
                        pass
                    elif result_code in RETRY_CODES and retry_idx < len(RETRY_WAITS):
                        await asyncio.sleep(RETRY_WAITS[retry_idx])
                        continue
                    loops[loop_key]["fail"] += 1
                    if result_code == LOCKED_RC:
                        cur_stage = loops[loop_key].get("current_stage") or loops[loop_key].get("stage_id")
                        if rc5_stage == cur_stage:
                            rc5_streak += 1
                        else:
                            rc5_stage, rc5_streak = cur_stage, 1
                        if rc5_streak >= LOCKED_STAGE_LIMIT:
                            loops[loop_key]["status"] = "fatal"
                            await save_loops_async(loops)
                            end_reason = f"ステージ `{cur_stage}` に入れません（rc=5 が{rc5_streak}周連続）"
                            await send_locked_dm(bot, user_id, cur_stage, loops[loop_key].get("player_name"), rc5_streak)
                            break
                    else:
                        rc5_streak = 0
                    if result_code in (202, 30):
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"致命的エラー (rc={result_code})"
                        break
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        loops[loop_key]["status"] = "fatal"
                        await save_loops_async(loops)
                        end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回"
                        break
                    break
                if end_reason:
                    break
            except Exception:
                loops[loop_key]["fail"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    loops[loop_key]["status"] = "fatal"
                    await save_loops_async(loops)
                    end_reason = f"連続エラー{MAX_CONSECUTIVE_ERRORS}回（例外）"
                    break

            loops[loop_key]["current"] = i
            await save_loops_async(loops)
            if i < count:
                await asyncio.sleep(rand_cooldown())

        await tracker.refresh()
        loops = load_loops()
        loops[loop_key]["status"] = "done"
        loops[loop_key]["end_reason"] = end_reason or "全回数完了"
        apply_gain_to_loop(loops[loop_key], tracker)
        await save_loops_async(loops)
        try:
            user = await bot.fetch_user(user_id)
            embed = discord.Embed(
                title="✅ 周回完了",
                description=(
                    f"**ステージ**: `{stage_id}`\n"
                    f"**成功**: {loops[loop_key]['success']}\n"
                    f"**失敗**: {loops[loop_key]['fail']}\n"
                    f"**終了理由**: {end_reason or '全回数完了'}"
                ),
                color=0x00ff88, timestamp=datetime.now(JST)
            )
            gain_fields(embed, loops[loop_key], done=True)
            await user.send(embed=embed)
        except Exception:
            pass
    except Exception as e:
        print(f">>> run_farm_dm 致命的: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await finalize_loop(loop_key)


# ============================================================
# 進行Embed
# ============================================================
def build_progress_embed(loop_key, data, status_text, elapsed=None):
    loop_type = data.get("type", "farm")
    if loop_type == "bench":
        return build_bench_embed(loop_key, data, status_text, elapsed)
    elif loop_type == "progress":
        return build_progress_embed_progress(loop_key, data, status_text, elapsed)
    elif loop_type == "event":
        return build_progress_embed_event(loop_key, data, status_text, elapsed)
    else:
        return build_progress_embed_farm(loop_key, data, status_text, elapsed)


def build_progress_embed_farm(loop_key, data, status_text, elapsed=None):
    current = data.get("current", 0)
    count = data.get("count", 0)
    success = data.get("success", 0)
    fail = data.get("fail", 0)
    pct = (current / count * 100) if count else 0
    if pct > 100: pct = 100
    filled = int(20 * pct / 100)
    bar = "█" * filled + "░" * (20 - filled)
    embed = discord.Embed(title=f"🎮 ぷにぷに 周回 — {status_text}", color=0x5865F2, timestamp=datetime.now(JST))
    embed.add_field(name="🆔 ステージ", value=f"`{data.get('stage_id')}`", inline=True)
    embed.add_field(name="📊 進捗", value=f"**{current} / {count if count < 999999 else '日時まで'}**", inline=True)
    embed.add_field(name="📈 成功率", value=f"{(success/max(current,1)*100 if current else 0):.1f}%", inline=True)
    embed.add_field(name="✅ 成功", value=str(success), inline=True)
    embed.add_field(name="❌ 失敗", value=str(fail), inline=True)
    if elapsed is not None:
        embed.add_field(name="⏱️ 実行時間", value=f"{elapsed:.1f}秒", inline=True)
    embed.add_field(name="進行状況", value=f"`{bar}` {pct:.1f}%", inline=False)
    end_at = data.get("end_at")
    if end_at:
        embed.add_field(name="⏰ 終了予定", value=f"<t:{int(end_at)}:R>", inline=True)
    gain_fields(embed, data, done=data.get("status") in ("done", "fatal", "stopped"))
    running = get_running_count()
    who = data.get("player_name")
    embed.set_footer(text=(f"{who} | " if who else "") + f"同時実行: {running}/{MAX_CONCURRENT_LOOPS}")
    return embed


def build_progress_embed_progress(loop_key, data, status_text, elapsed=None):
    current = data.get("current", 0)
    total = data.get("total", 0)
    success = data.get("success", 0)
    fail = data.get("fail", 0)
    current_stage = data.get("current_stage", "-")
    pct = (current / total * 100) if total else 0
    if pct > 100: pct = 100
    filled = int(20 * pct / 100)
    bar = "█" * filled + "░" * (20 - filled)
    embed = discord.Embed(title=f"🎯 ステージ進行 — {status_text}", color=0x9B59B6, timestamp=datetime.now(JST))
    embed.add_field(name="🔢 範囲", value=f"`{data.get('start_id')}` 〜 `{data.get('end_id')}`", inline=True)
    embed.add_field(name="📊 進捗", value=f"**{current} / {total}**", inline=True)
    embed.add_field(name="🎮 現在", value=f"`{current_stage}`", inline=True)
    embed.add_field(name="✅ 成功", value=str(success), inline=True)
    embed.add_field(name="❌ 失敗", value=str(fail), inline=True)
    if elapsed is not None:
        embed.add_field(name="⏱️ 実行時間", value=f"{elapsed:.1f}秒", inline=True)
    embed.add_field(name="進行状況", value=f"`{bar}` {pct:.1f}%", inline=False)
    end_at = data.get("end_at")
    if end_at:
        embed.add_field(name="⏰ 終了予定", value=f"<t:{int(end_at)}:R>", inline=True)
    gain_fields(embed, data, done=data.get("status") in ("done", "fatal", "stopped"))
    running = get_running_count()
    who = data.get("player_name")
    embed.set_footer(text=(f"{who} | " if who else "") + f"同時実行: {running}/{MAX_CONCURRENT_LOOPS}")
    return embed


def build_progress_embed_event(loop_key, data, status_text, elapsed=None):
    current = data.get("current", 0)
    success = data.get("success", 0)
    fail = data.get("fail", 0)
    current_stage = data.get("current_stage", "-")
    embed = discord.Embed(title=f"🎪 イベント自動進行 — {status_text}", color=0xE67E22, timestamp=datetime.now(JST))
    embed.add_field(name="🚀 開始ID", value=f"`{data.get('start_id')}`", inline=True)
    embed.add_field(name="🎮 現在", value=f"`{current_stage}`", inline=True)
    embed.add_field(name="📊 処理数", value=f"**{current}**", inline=True)
    embed.add_field(name="✅ 成功", value=str(success), inline=True)
    embed.add_field(name="❌ 失敗", value=str(fail), inline=True)
    if elapsed is not None:
        embed.add_field(name="⏱️ 実行時間", value=f"{elapsed:.1f}秒", inline=True)
    end_at = data.get("end_at")
    if end_at:
        embed.add_field(name="⏰ 終了予定", value=f"<t:{int(end_at)}:R>", inline=True)
    gain_fields(embed, data, done=data.get("status") in ("done", "fatal", "stopped"))
    who = data.get("player_name")
    embed.set_footer(text=(f"{who} | " if who else "") + "エラーで次ブロック001へ自動ジャンプ")
    return embed


# ============================================================
# 効率計測
# ============================================================
def parse_stage_list(raw: str) -> list:
    out = []
    for token in re.split(r"[,\s]+", (raw or "").strip()):
        if not token:
            continue
        if "-" in token:
            a, _, b = token.partition("-")
            start, end = int(a), int(b)
            if end < start:
                raise ValueError(f"範囲が逆: {token}")
            if end - start + 1 > BENCH_MAX_STAGES:
                raise ValueError(f"範囲が広すぎ: {token}")
            out.extend(range(start, end + 1))
        else:
            out.append(int(token))
    uniq = []
    for sid in out:
        if sid not in uniq:
            uniq.append(sid)
    return uniq


def disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad_disp(s: str, width: int, right: bool = False) -> str:
    space = " " * max(0, width - disp_width(s))
    return (space + str(s)) if right else (str(s) + space)


def metric_label(results) -> tuple:
    use_ep = any((r.get("ep_total") or 0) for r in (results or []))
    if use_ep:
        name = next((r.get("point_name") for r in results if r.get("point_name")), "イベントP")
        return "ep", name, ("Yポ" if "ポイント" in name else name[:3])
    return "money", "マネー", "マネ"


def metric_of(r: dict, kind: str, field: str):
    if kind == "ep":
        return r.get("ep_" + field) or 0
    return r.get("money_" + field) or 0


def bench_row(r: dict, kind: str) -> str:
    def num(v, fmt):
        return format(v, fmt) if isinstance(v, (int, float)) else "-"
    return (f"{r['stage_id']:<9}{r['success']:>3}"
            f"{num(metric_of(r, kind, 'per'), '>9.1f')}"
            f"{num(r.get('hitodama_per'), '>8.2f')}"
            f"{num(metric_of(r, kind, 'per_hitodama'), '>10.1f')}"
            f"{num(r.get('sec_per'), '>8.1f')}")


def build_bench_embed(loop_key, data, status_text, elapsed=None):
    done = data.get("status") in ("done", "fatal", "stopped")
    results = data.get("results") or []
    total = data.get("total", 0)
    current = data.get("current", 0)
    embed = discord.Embed(title=f"📐 効率計測 — {status_text}", color=0x1ABC9C, timestamp=datetime.now(JST))
    embed.add_field(name="🎮 現在", value=f"`{data.get('current_stage', '-')}`", inline=True)
    embed.add_field(name="📊 進捗", value=f"**{current} / {total}** 周", inline=True)
    embed.add_field(name="🔁 1ステージ", value=f"{data.get('samples', 0)} 回ずつ", inline=True)
    if results:
        kind, label, short = metric_label(results)
        ranked = sorted(results, key=lambda r: -metric_of(r, kind, "per_hitodama"))
        head = (pad_disp("ステージ", 9) + pad_disp("周", 3, True) +
                pad_disp(short + "/周", 9, True) + pad_disp("人魂/周", 8, True) +
                pad_disp(short + "/人魂", 10, True) + pad_disp("秒/周", 8, True))
        lines = [head, "-" * 47] + [bench_row(r, kind) for r in ranked]
        embed.add_field(name=f"📋 結果（{label}/人魂の高い順）",
                        value="```\n" + "\n".join(lines) + "\n```", inline=False)
        best = ranked[0]
        if metric_of(best, kind, "per_hitodama"):
            extra = ""
            if kind == "ep" and best.get("money_per"):
                extra = f"\nおまけ: マネー {best['money_per']:,.0f}/周"
            embed.add_field(
                name="🏆 一番おいしいステージ",
                value=(f"`{best['stage_id']}` — 人魂1個あたり "
                       f"**{metric_of(best, kind, 'per_hitodama'):,.1f} {label}**\n"
                       f"1周 {metric_of(best, kind, 'per'):,.1f} {label} / "
                       f"{best.get('sec_per') or 0:.1f}秒" + extra),
                inline=False
            )
    if data.get("buy_count"):
        embed.add_field(name="💠 人魂購入", value=f"{data['buy_count']} 回 (-{data.get('buy_ym', 0):,} YM)", inline=True)
    if elapsed is not None:
        embed.add_field(name="⏱️ 実行時間", value=f"{elapsed:.1f}秒", inline=True)
    if done and data.get("end_reason"):
        embed.add_field(name="🏁 終了理由", value=data["end_reason"], inline=False)
    who = data.get("player_name")
    embed.set_footer(text=(f"{who} | " if who else "") + "人魂は計測中も時間回復するので、人魂/周はやや少なめに出ます")
    return embed


async def run_benchmark(bot, interaction, guild_id, user_id, stage_ids, samples, account_id=None):
    loop_key = f"{user_id}_bench_{int(time.time())}"
    try:
        sdata = get_session(user_id, account_id)
        account_id = str((sdata or {}).get("userId") or account_id or "")
        if not sdata:
            await safe_reply(interaction, content="❌ セッションなし", ephemeral=True)
            return
        client = build_client_from_account(sdata)
        try:
            await asyncio.to_thread(client.login, sdata["userId"])
            await update_token(user_id, account_id, client)
        except Exception as e:
            await safe_reply(interaction, content=f"❌ 再ログイン失敗: `{str(e)[:300]}`", ephemeral=True)
            return
        loops = load_loops()
        loops[loop_key] = {
            "type": "bench", "user_id": user_id,
            "stages": stage_ids, "samples": samples,
            "total": len(stage_ids) * samples, "current": 0,
            "success": 0, "fail": 0, "status": "running",
            "account_id": account_id, "player_name": (sdata or {}).get("player_name"),
            "current_stage": stage_ids[0], "results": [],
            "buy_count": 0, "buy_ym": 0,
            "started_at": datetime.now(JST).isoformat()
        }
        await save_loops_async(loops)
        embed = build_bench_embed(loop_key, loops[loop_key], "📐 計測中...")
        try:
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception:
            pass

        start_time = time.time()
        end_reason = None
        buy_count = 0
        results = []
        done_battles = 0
        total_success = 0
        total_fail = 0

        for sid in stage_ids:
            loops = load_loops()
            if loop_key not in loops or loops[loop_key].get("status") == "stopped":
                end_reason = "ユーザー停止"; break
            try:
                await asyncio.to_thread(client.login, sdata["userId"])
            except Exception as e:
                end_reason = f"ログイン失敗: {str(e)[:80]}"; break

            before = get_hitodama_detail(client)
            tracker = GainTracker(client)
            stage_buys = 0
            ok_n = 0
            ng_n = 0
            stage_locked = 0
            skip_stage = False
            stage_start = time.time()

            for _ in range(samples):
                if skip_stage: break
                loops = load_loops()
                if loop_key not in loops or loops[loop_key].get("status") == "stopped":
                    end_reason = "ユーザー停止"; break
                if get_hitodama(client) < 1:
                    bought, buy_info, buy_count = await try_auto_buy(client, buy_count)
                    if bought:
                        stage_buys += 1
                    else:
                        end_reason = f"人魂不足: {describe_buy_failure(buy_info)[:150]}"
                        break
                await asyncio.sleep(rand_request_delay())
                try:
                    for retry_idx in range(len(RETRY_WAITS) + 1):
                        rc, result = await asyncio.to_thread(client.battle, sid)
                        code = result.get("resultCode")
                        if code == 0:
                            ok_n += 1
                            total_success += 1
                            await tracker.after_battle(result)
                            break
                        if code == 32:
                            await asyncio.to_thread(client.login, sdata["userId"])
                            await update_token(user_id, account_id, client)
                            continue
                        if code in RETRY_CODES and retry_idx < len(RETRY_WAITS):
                            await asyncio.sleep(RETRY_WAITS[retry_idx])
                            continue
                        ng_n += 1
                        total_fail += 1
                        if code == LOCKED_RC:
                            stage_locked += 1
                            if stage_locked >= 2:
                                skip_stage = True
                        if code in (202, 30):
                            end_reason = f"致命的エラー (rc={code})"
                        break
                except Exception:
                    ng_n += 1
                    total_fail += 1
                done_battles += 1
                loops = load_loops()
                if loop_key in loops:
                    loops[loop_key]["current"] = done_battles
                    loops[loop_key]["current_stage"] = sid
                    loops[loop_key]["success"] = total_success
                    loops[loop_key]["fail"] = total_fail
                    loops[loop_key]["buy_count"] = buy_count
                    loops[loop_key]["buy_ym"] = buy_count * AUTO_BUY_COST_YM
                    await save_loops_async(loops)
                if end_reason: break
                await asyncio.sleep(rand_cooldown())

            await tracker.refresh()
            after = get_hitodama_detail(client)
            consumed = before["total"] + stage_buys * AUTO_BUY_HITODAMA_GAIN - after["total"]
            elapsed_stage = time.time() - stage_start
            ep = tracker.event_point
            row = {
                "stage_id": sid, "success": ok_n, "fail": ng_n,
                "point_name": tracker.point_name, "ep_total": ep,
                "ep_per": (ep / ok_n) if ok_n else None,
                "ep_per_hitodama": (ep / consumed) if consumed > 0 else None,
                "money_total": tracker.money_gain,
                "money_per": (tracker.money_gain / ok_n) if ok_n else None,
                "money_per_hitodama": (tracker.money_gain / consumed) if consumed > 0 else None,
                "exp_total": tracker.exp_gain,
                "hitodama": consumed,
                "hitodama_per": (consumed / ok_n) if ok_n else None,
                "sec_per": (elapsed_stage / ok_n) if ok_n else None,
            }
            results.append(row)
            save_bench_results([row], user_id)
            loops = load_loops()
            if loop_key in loops:
                loops[loop_key]["results"] = results
                loops[loop_key]["money_gain"] = sum(x.get("money_total") or 0 for x in results)
                loops[loop_key]["money_counted"] = sum(x.get("success") or 0 for x in results)
                loops[loop_key]["event_point"] = sum(x.get("ep_total") or 0 for x in results)
                loops[loop_key]["point_name"] = next((x.get("point_name") for x in results if x.get("point_name")), None)
                await save_loops_async(loops)
                embed = build_bench_embed(loop_key, loops[loop_key], "📐 計測中...")
                try:
                    await interaction.edit_original_response(embed=embed)
                except Exception:
                    pass
            if end_reason: break

        loops = load_loops()
        if loop_key in loops:
            loops[loop_key]["status"] = "done"
            loops[loop_key]["end_reason"] = end_reason or "計測完了"
            loops[loop_key]["results"] = results
            await save_loops_async(loops)
            embed = build_bench_embed(loop_key, loops[loop_key], "✅ 完了", time.time() - start_time)
            try:
                await interaction.edit_original_response(embed=embed)
            except Exception:
                pass
        if results:
            await refresh_panels(bot)
    except Exception as e:
        print(f">>> run_benchmark 致命的: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await finalize_loop(loop_key)


# ============================================================
# ジョブ後始末・日次集計
# ============================================================
def load_daily() -> dict:
    return load_json(DAILY_FILE, {})


def record_daily(user_id, money=0, battles=0, buys=0, stage=None, date_key=None):
    data = load_daily()
    key = date_key or datetime.now(JST).strftime("%Y-%m-%d")
    day = data.setdefault(key, {})
    u = day.setdefault(str(user_id), {"money": 0, "battles": 0, "buys": 0, "ym": 0, "stages": {}})
    u["money"] += int(money or 0)
    u["battles"] += int(battles or 0)
    u["buys"] += int(buys or 0)
    u["ym"] += int(buys or 0) * AUTO_BUY_COST_YM
    if stage and battles:
        u["stages"][str(stage)] = u["stages"].get(str(stage), 0) + int(battles)
    save_json(DAILY_FILE, data)
    return u


async def finalize_loop(loop_key: str):
    loops = load_loops()
    data = loops.get(loop_key)
    if not data:
        return
    changed = False
    if data.get("status") == "running":
        data["status"] = "error"
        data.setdefault("end_reason", "異常終了（例外で中断）")
        changed = True
    if not data.get("daily_recorded"):
        stage = data.get("stage_id") if data.get("type") == "farm" else None
        record_daily(
            data.get("user_id"),
            money=data.get("money_gain") or 0,
            battles=data.get("success") or 0,
            buys=data.get("buy_count") or 0,
            stage=stage,
        )
        data["daily_recorded"] = True
        changed = True
    if changed:
        await save_loops_async(loops)


def reset_stale_loops():
    loops = load_loops()
    stale = [k for k, v in loops.items() if v.get("status") == "running"]
    for k in stale:
        loops[k]["status"] = "error"
        loops[k]["end_reason"] = "Bot再起動で中断"
    if stale:
        save_loops(loops)
    return len(stale)


# ============================================================
# 日次レポート
# ============================================================
def daily_summary(user_id, days: int = 7) -> list:
    data = load_daily()
    out = []
    for i in range(days):
        key = (datetime.now(JST) - timedelta(days=i)).strftime("%Y-%m-%d")
        u = (data.get(key) or {}).get(str(user_id))
        if u:
            out.append((key, u))
    return out


def build_daily_embed(user_id, player_name=None, days: int = 7) -> discord.Embed:
    rows = daily_summary(user_id, days)
    today_key = datetime.now(JST).strftime("%Y-%m-%d")
    today = next((u for k, u in rows if k == today_key), None)
    embed = discord.Embed(title="📅 日次レポート", description=f"**{player_name or user_id}**", color=0xF1C40F, timestamp=datetime.now(JST))
    if today:
        yp = today["money"]
        n = today["battles"]
        embed.add_field(name="💰 今日稼いだマネー", value=f"**{yp:+,}**", inline=True)
        embed.add_field(name="🔁 周回数", value=f"{n:,} 周", inline=True)
        embed.add_field(name="📐 1ステあたり", value=(f"{yp / n:,.1f} マネー" if n else "-"), inline=True)
        if today.get("buys"):
            embed.add_field(name="💠 人魂購入", value=f"{today['buys']} 回 (-{today['ym']:,} YM)", inline=True)
        top = sorted((today.get("stages") or {}).items(), key=lambda kv: -kv[1])[:3]
        if top:
            embed.add_field(name="🎮 よく回したステージ", value=" / ".join(f"`{s}`×{c}" for s, c in top), inline=False)
    else:
        embed.add_field(name="💰 今日稼いだマネー", value="まだ周回していません", inline=False)
    if len(rows) > 1:
        lines = []
        total_yp = total_n = 0
        for key, u in rows:
            per = (u["money"] / u["battles"]) if u["battles"] else 0
            lines.append(f"{key[5:]}  {u['money']:>8,} マネー  {u['battles']:>4}周  ({per:,.0f}/周)")
            total_yp += u["money"]; total_n += u["battles"]
        embed.add_field(name=f"📊 直近{len(rows)}日", value="```\n" + "\n".join(lines) + "\n```", inline=False)
        embed.add_field(name="🧮 合計", value=f"**{total_yp:,}** マネー / {total_n:,}周", inline=False)
    embed.set_footer(text="周回が終わったジョブの分だけ集計されます")
    return embed


async def send_daily_reports(bot):
    if bot is None:
        return 0
    today_key = datetime.now(JST).strftime("%Y-%m-%d")
    day = load_daily().get(today_key) or {}
    sessions = load_sessions()
    sent = 0
    for uid, u in day.items():
        if not u.get("battles"):
            continue
        try:
            user = await bot.fetch_user(int(uid))
            sess = sessions.get(uid) or {}
            name = sess.get("playerName") or sess.get("player_name")
            await user.send(embed=build_daily_embed(int(uid), name))
            sent += 1
        except Exception:
            pass
    return sent


# ============================================================
# 次の休憩までの分数
# ============================================================
def calc_minutes_until_rest() -> int:
    if not ODD_HOUR_REST_ENABLED:
        return 0
    now = datetime.now(JST)
    candidate = now.replace(minute=ODD_HOUR_REST_MIN, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(hours=1)
    while candidate.hour % 2 == 0:
        candidate = candidate + timedelta(hours=1)
    diff = candidate - now
    return max(0, int(diff.total_seconds() // 60))


# ============================================================
# パネルEmbed（実行上限表示）
# ============================================================
def build_panel_embed() -> discord.Embed:
    loops = load_loops()
    running_account_ids = set()
    running_jobs = []
    for d in loops.values():
        if d.get("status") == "running":
            aid = str(d.get("account_id") or "")
            if aid:
                running_account_ids.add(aid)
            running_jobs.append(d)

    now_stage = "-"
    if running_jobs:
        running_jobs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        d = running_jobs[0]
        now_stage = str(d.get("current_stage") or d.get("stage_id") or "-")

    hitodama_str = "-"
    for v in _panel_hitodama_cache.values():
        if v:
            hitodama_str = v
            break

    minutes_left = calc_minutes_until_rest()

    embed = discord.Embed(
        title="ぷにぷに 自動周回 — 状況",
        color=0x57F287,
        timestamp=datetime.now(JST)
    )
    embed.add_field(name="実行中 / 上限", value=f"**{len(running_account_ids)} / {MAX_CONCURRENT_LOOPS}**", inline=True)
    embed.add_field(name="人魂", value=f"**{hitodama_str}**", inline=True)
    embed.add_field(name="現在ステージ", value=f"`{now_stage}`", inline=True)
    embed.add_field(name="次の休憩まで", value=f"**{minutes_left}分**", inline=True)
    embed.set_footer(text="最終更新 — たった今")
    return embed


async def update_panel_hitodama_cache():
    global _panel_hitodama_cache
    store = load_sessions()
    total = 0
    for uid in store:
        accounts = _migrate_session(store[uid]).get("accounts", [])
        if not accounts:
            continue
        acc = accounts[0]
        try:
            client = build_client_from_account(acc)
            await asyncio.to_thread(client.login, acc["userId"])
            detail = get_hitodama_detail(client)
            total += detail["total"]
        except Exception as e:
            print(f">>> 人魂取得失敗 uid={uid}: {e}")
    _panel_hitodama_cache["__all__"] = f"{total}個"


# ============================================================
# アカウント切替UI（ephemeral）
# ============================================================
def build_accounts_embed(user_id) -> discord.Embed:
    accounts = get_user_accounts(user_id)
    active = get_active_id(user_id)
    embed = discord.Embed(
        title="👥 アカウント一覧",
        description=("登録がありません。「➕ 追加ログイン」から登録してください。"
                     if not accounts else
                     "セレクトメニューで**操作対象の垢**を切り替えます。"),
        color=0x5865F2, timestamp=datetime.now(JST)
    )
    loops = load_loops()
    for acc in accounts:
        gid = str(acc.get("player_id") or acc.get("userId"))
        running = [d for d in loops.values()
                   if str(d.get("account_id") or "") == str(gid) and d.get("status") == "running"]
        marks = []
        if gid == active:
            marks.append("✅ 選択中")
        if running:
            d = running[0]
            marks.append(f"🔄 周回中 `{d.get('current_stage') or d.get('stage_id')}` ({d.get('current', 0)}周)")
        embed.add_field(
            name=f"{acc.get('player_name', '不明')}",
            value=(f"ID: `{gid}`\n"
                   f"登録: {str(acc.get('saved_at', ''))[:16].replace('T', ' ')}\n"
                   + ("\n".join(marks) if marks else "待機中")),
            inline=True
        )
    running_total = sum(1 for d in loops.values() if d.get("status") == "running")
    embed.set_footer(text=f"{len(accounts)} 垢登録 / 同時実行 {running_total}/{MAX_CONCURRENT_LOOPS}")
    return embed


class AccountSelect(ui.Select):
    def __init__(self, user_id):
        self.user_id = user_id
        accounts = get_user_accounts(user_id)
        active = get_active_id(user_id)
        loops = load_loops()
        busy = {str(d.get("account_id")) for d in loops.values() if d.get("status") == "running"}
        options = []
        for i, acc in enumerate(accounts[:25]):
            gid = str(acc.get("player_id") or acc.get("userId"))
            desc = f"ID: {gid}"
            if gid in busy:
                desc += " / 周回中"
            options.append(discord.SelectOption(
                label=str(acc.get("player_name", "不明"))[:100],
                value=str(i),
                description=desc[:100],
                default=(gid == active),
                emoji="🔄" if gid in busy else None,
            ))
        if not options:
            options = [discord.SelectOption(label="（登録なし）", value="none")]
        super().__init__(placeholder="操作する垢を選ぶ", options=options,
                         min_values=1, max_values=1, disabled=not accounts)

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        if v == "none":
            return await interaction.response.defer()
        accounts = get_user_accounts(self.user_id)
        try:
            idx = int(v)
        except ValueError:
            return await interaction.response.defer()
        if idx >= len(accounts):
            return await interaction.response.defer()
        acc = accounts[idx]
        gid = str(acc.get("player_id") or acc.get("userId"))
        if not await set_active(self.user_id, gid):
            return await interaction.response.send_message("❌ 切り替えに失敗しました。", ephemeral=True)
        await interaction.response.edit_message(
            content=f"✅ **{acc.get('player_name', '不明')}** に切り替えました。",
            embed=build_accounts_embed(self.user_id),
            view=AccountView(self.user_id)
        )


class AccountView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(AccountSelect(user_id))

    @ui.button(label="追加ログイン", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add(self, interaction, button):
        try:
            await interaction.response.send_modal(LoginModal())
        except (discord.NotFound, discord.HTTPException):
            pass

    @ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=build_accounts_embed(self.user_id),
            view=AccountView(self.user_id)
        )

    @ui.button(label="選択中の垢を削除", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove(self, interaction, button):
        acc = get_active_account(self.user_id)
        if not acc:
            return await interaction.response.send_message("⚠️ 登録がありません。", ephemeral=True)
        gid = str(acc.get("player_id") or acc.get("userId"))
        if account_busy(gid):
            return await interaction.response.send_message(
                "❌ この垢は周回中です。先に 🛑 停止してください。", ephemeral=True)
        name = acc.get("player_name", "不明")
        await remove_account(self.user_id, gid)
        await interaction.response.edit_message(
            content=f"🗑️ **{name}** を削除しました。",
            embed=build_accounts_embed(self.user_id),
            view=AccountView(self.user_id)
        )

    @ui.button(label="閉じる", style=discord.ButtonStyle.secondary, emoji="✅", row=2)
    async def close(self, interaction, button):
        try:
            await interaction.response.edit_message(
                content="✅ 垢の切り替えを終了しました。",
                embed=None,
                view=None
            )
        except (discord.NotFound, discord.HTTPException):
            pass


# ============================================================
# セレクトメニュー
# ============================================================
class AccountActionSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="ログイン", value="login", emoji="🔐",
                                 description="垢を追加ログイン"),
            discord.SelectOption(label="垢切り替え", value="switch", emoji="🔄",
                                 description="使用する垢を切り替え"),
            discord.SelectOption(label="垢一覧", value="list", emoji="📋",
                                 description="登録垢の一覧を表示"),
            discord.SelectOption(label="ログアウト", value="logout", emoji="🚪",
                                 description="選択中の垢をログアウト"),
        ]
        super().__init__(
            placeholder="アカウント操作を選択...",
            options=options,
            custom_id="ywp_panel:account_select",
            min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        if v == "login":
            try:
                await interaction.response.send_modal(LoginModal())
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if v == "switch":
            if not await safe_defer(interaction):
                return
            await safe_reply(
                interaction,
                embed=build_accounts_embed(interaction.user.id),
                view=AccountView(interaction.user.id),
                ephemeral=True
            )
            return
        if v == "list":
            if not await safe_defer(interaction):
                return
            accounts = get_user_accounts(interaction.user.id)
            if not accounts:
                return await safe_reply(interaction, content="❌ 垢が登録されていません。", ephemeral=True)
            active = get_active_id(interaction.user.id)
            embed = discord.Embed(
                title="📋 登録垢一覧",
                description=f"合計: **{len(accounts)}件**",
                color=0x5865F2,
                timestamp=datetime.now(JST)
            )
            for acc in accounts:
                gid = str(acc.get("player_id") or acc.get("userId"))
                mark = "🟢" if gid == active else "⚪"
                embed.add_field(
                    name=f"{mark} {acc.get('player_name', '不明')}",
                    value=f"ID: `{gid}`",
                    inline=True
                )
            await safe_reply(interaction, embed=embed, ephemeral=True)
            return
        if v == "logout":
            if not await safe_defer(interaction):
                return
            acc = get_active_account(interaction.user.id)
            if not acc:
                return await safe_reply(interaction, content="⚠️ ログインしていません。", ephemeral=True)
            name = acc.get("player_name", "不明")
            gid = str(acc.get("player_id") or acc.get("userId"))
            await remove_account(interaction.user.id, gid)
            left = len(get_user_accounts(interaction.user.id))
            await safe_reply(
                interaction,
                content=f"✅ **{name}** をログアウトしました。（残り {left} 垢）",
                ephemeral=True
            )
            return


class ProgressActionSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="ステージ一覧", value="stages", emoji="🎯",
                                 description="クリア済みステージIDを表示"),
            discord.SelectOption(label="ステージ進行", value="progress", emoji="🎯",
                                 description="開始〜終了IDまで順番に周回"),
            discord.SelectOption(label="イベント自動進行", value="event", emoji="🎪",
                                 description="エラーで次ブロック001へ自動ジャンプ"),
            discord.SelectOption(label="進行確認", value="view", emoji="📈",
                                 description="直近の進行状況を表示"),
            discord.SelectOption(label="実行状況", value="running", emoji="📡",
                                 description="現在の実行状況を表示"),
        ]
        super().__init__(
            placeholder="進行 / ステージ操作を選択...",
            options=options,
            custom_id="ywp_panel:progress_select",
            min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        if v == "stages":
            if not await safe_defer(interaction):
                return
            acc = get_active_account(interaction.user.id)
            if not acc:
                return await safe_reply(interaction, content="❌ ログインしていません。", ephemeral=True)
            client = build_client_from_account(acc)
            try:
                await asyncio.to_thread(client.login, acc["userId"])
                update_account_tokens(interaction.user.id, client)
            except Exception as e:
                return await safe_reply(interaction, content=f"❌ ログイン失敗: `{str(e)[:200]}`", ephemeral=True)
            stage_raw = client.save.get("ywp_user_stage")
            if not stage_raw:
                return await safe_reply(interaction, content="❌ ステージ情報がありません。", ephemeral=True)
            stages = parse_stages(stage_raw)
            cleared = sorted([sid for sid, info in stages.items() if info["cleared"]])
            if not cleared:
                return await safe_reply(interaction, content="❌ クリア済みステージがありません。", ephemeral=True)
            categorized = categorize_stages(cleared)
            chunks = []
            current = ""
            for cat_name, cat_ids in categorized.items():
                header = f"\n**{cat_name}** ({len(cat_ids)}件)\n"
                body_lines = []
                for i in range(0, len(cat_ids), 10):
                    c = cat_ids[i:i+10]
                    body_lines.append(" ".join(f"`{sid}`" for sid in c))
                section = header + "\n".join(body_lines) + "\n"
                if len(current) + len(section) > 1900:
                    chunks.append(current); current = section
                else:
                    current += section
            if current:
                chunks.append(current)
            embed = discord.Embed(
                title="🎯 クリア済みステージ",
                description=f"**垢**: {acc.get('player_name', '不明')}\n**合計**: {len(cleared)}件",
                color=0x5865F2, timestamp=datetime.now(JST)
            )
            embed.add_field(name="📋 分類", value=chunks[0][:1024], inline=False)
            await safe_reply(interaction, embed=embed, ephemeral=True)
            for chunk in chunks[1:]:
                try:
                    await interaction.followup.send(chunk, ephemeral=True)
                except Exception:
                    break
            return
        if v == "progress":
            try:
                await interaction.response.send_modal(ProgressModal())
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if v == "event":
            try:
                await interaction.response.send_modal(EventProgressModal())
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if v == "view":
            if not await safe_defer(interaction):
                return
            loops = load_loops()
            my_loops = [
                (k, d) for k, d in loops.items()
                if d.get("user_id") == interaction.user.id
                and d.get("type") in ("progress", "event", "farm", "bench")
            ]
            if not my_loops:
                return await safe_reply(interaction, content="❌ 進行中の処理がありません。", ephemeral=True)
            my_loops.sort(key=lambda x: x[1].get("started_at", ""), reverse=True)
            key, data = my_loops[0]
            status = data.get("status", "running")
            if status == "done":
                st = "✅ 完了"
            elif status == "stopped":
                st = "🛑 停止"
            elif status == "fatal":
                st = "❌ エラー停止"
            elif status == "paused_hitodama":
                st = "⏸️ 人魂不足で停止中"
            else:
                st = "🎯 進行中..."
            embed = build_progress_embed(key, data, st)
            if data.get("end_reason"):
                embed.add_field(name="🏁 終了理由", value=data["end_reason"], inline=False)
            await safe_reply(interaction, embed=embed, ephemeral=True)
            return
        if v == "running":
            if not await safe_defer(interaction):
                return
            loops = load_loops()
            running_loops = [
                (k, d) for k, d in loops.items()
                if d.get("status") in ("running", "paused_hitodama")
            ]
            embed = discord.Embed(
                title="📡 現在の実行状況",
                description=f"**同時実行: {len([r for r in running_loops if r[1].get('status') == 'running'])} / {MAX_CONCURRENT_LOOPS}**",
                color=0x5865F2,
                timestamp=datetime.now(JST)
            )
            if not running_loops:
                embed.add_field(name="状態", value="現在実行中の処理はありません。", inline=False)
            else:
                for key, data in running_loops[:7]:
                    user_id = data.get("user_id")
                    member = interaction.guild.get_member(user_id) if interaction.guild else None
                    name = member.display_name if member else f"<@{user_id}>"
                    status = "🔄 実行中" if data.get("status") == "running" else "⏸️ 停止中"
                    loop_type = data.get("type", "farm")
                    pid = data.get("account_id", "-")
                    if loop_type == "progress":
                        info = f"垢: `{pid}`\n範囲: `{data.get('start_id')}`〜`{data.get('end_id')}`\n進捗: **{data.get('current', 0)} / {data.get('total', 0)}**"
                    elif loop_type == "event":
                        info = f"垢: `{pid}`\n開始: `{data.get('start_id')}`\n処理: **{data.get('current', 0)}**"
                    else:
                        info = f"垢: `{pid}`\nステージ: `{data.get('stage_id')}`\n進捗: **{data.get('current', 0)} / {data.get('count', 0)}**"
                    embed.add_field(name=f"👤 {name} ({status}) [{loop_type}]", value=info, inline=True)
            await safe_reply(interaction, embed=embed, ephemeral=True)
            return


# ============================================================
# パネルView
# ============================================================
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AccountActionSelect())
        self.add_item(ProgressActionSelect())

    @ui.button(label="周回開始", style=discord.ButtonStyle.success, emoji="▶️", custom_id="ywp_panel:farm", row=2)
    async def farm(self, interaction, button):
        try:
            await interaction.response.send_modal(FarmModal())
        except (discord.NotFound, discord.HTTPException):
            pass

    @ui.button(label="停止", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="ywp_panel:stop", row=2)
    async def stop(self, interaction, button):
        if not await safe_defer(interaction):
            return
        loops = load_loops()
        count = 0
        for key, data in loops.items():
            if data.get("user_id") == interaction.user.id and data.get("status") in ("running", "paused_hitodama"):
                data["status"] = "stopped"
                count += 1
        for key in list(loops.keys()):
            if loops[key].get("user_id") != interaction.user.id:
                continue
            if loops[key].get("status") in ("stopped", "done", "fatal", "error"):
                del loops[key]
        await save_loops_async(loops)
        await safe_reply(interaction, content=f"🛑 {count}件の処理を停止しました", ephemeral=True)


# ============================================================
# 計測結果の保存
# ============================================================
def load_bench() -> dict:
    return load_json(BENCH_FILE, {})


def save_bench_results(results: list, user_id=None):
    data = load_bench()
    for r in results:
        kind, label, _ = metric_label([r])
        per_h = metric_of(r, kind, "per_hitodama")
        if not per_h:
            continue
        data[str(r["stage_id"])] = {
            "metric_name": label,
            "yp_per": metric_of(r, kind, "per"),
            "hitodama_per": r.get("hitodama_per"),
            "yp_per_hitodama": per_h,
            "money_per": r.get("money_per"),
            "sec_per": r.get("sec_per"),
            "samples": r.get("success"),
            "measured_at": datetime.now(JST).isoformat(),
            "measured_by": user_id,
        }
    save_json(BENCH_FILE, data)
    return data


async def refresh_panels(bot):
    if bot is None:
        return
    try:
        await update_panel_hitodama_cache()
    except Exception as e:
        print(f">>> 人魂キャッシュ更新失敗: {e}")

    panel = load_panel()
    for guild_id, data in panel.items():
        try:
            ch_id = int(data["channel_id"])
            channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            msg = await channel.fetch_message(int(data["message_id"]))
            await msg.edit(embed=build_panel_embed(), view=PanelView())
        except Exception as e:
            print(f">>> パネル更新失敗 guild={guild_id}: {e}")


# ============================================================
# Cog
# ============================================================
class YWPPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.restore_panel()
        reset_stale_loops()
        if DAILY_REPORT_ENABLED:
            self.daily_report.start()

    def cog_unload(self):
        self.daily_report.cancel()

    @tasks.loop(time=dtime(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE, tzinfo=JST))
    async def daily_report(self):
        await send_daily_reports(self.bot)

    @daily_report.before_loop
    async def before_daily_report(self):
        await self.bot.wait_until_ready()

    def restore_panel(self):
        panel = load_panel()
        for guild_id, data in panel.items():
            msg_id = data.get("message_id")
            if msg_id:
                try:
                    self.bot.add_view(PanelView(), message_id=int(msg_id))
                except Exception as e:
                    print(f"⚠️ パネル復元失敗: {e}")

    @app_commands.command(name="パネル設置", description="🎮 ぷにぷに自動周回パネルを設置（管理者）")
    @app_commands.default_permissions(administrator=True)
    async def ywp_panel(self, interaction, channel: discord.TextChannel):
        if not await safe_defer(interaction):
            return
        embed = build_panel_embed()
        view = PanelView()
        msg = await channel.send(embed=embed, view=view)
        panel = load_panel()
        panel[str(interaction.guild_id)] = {
            "channel_id": channel.id,
            "message_id": msg.id,
            "created_at": datetime.now(JST).isoformat(),
        }
        save_panel(panel)
        self.bot.add_view(PanelView(), message_id=msg.id)
        await safe_reply(interaction, content=f"✅ `{channel.mention}` にパネルを設置しました。", ephemeral=True)


# ============================================================
# setup
# ============================================================
async def setup(bot):
    await bot.add_cog(YWPPanel(bot))