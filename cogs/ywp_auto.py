import base64
import hashlib
import json
import random
import re
import time
import zlib
from urllib.parse import urljoin, urlparse, parse_qs

from Crypto.Cipher import AES
import requests
from discord.ext import commands

# ============================================================================
# 定数設定
# ============================================================================

AESK = bytes.fromhex('a865d7e5e2458f8ce1b5ecd087e54594')
K = b'0bk2kvtFE2'
APKEY = 'a-zrhgm09pgcgjc1iv9cxvpk3xm9b0ynyo4u00sny6bjq10nrx3up2yrhjnq2lhg'
SIGNATURE = ('s4X9CoyxGma3kGuAp5woThgvBX3dCi77Slh5RcOo6ybmMTt0J4CGiZwyiCsil7P3'
             'MVgjiVt+kGE1MqvttCXLB+hlOpyTkJp5a78TXthBNVw=')
GS = 'https://gameserver.yw-p.com'
L5 = 'https://api.level5-id.com'

MODEL = 'GA00747-UK'
OSVER = '9'
APPVER = '4.175.0'
BATTERY = {'level': 100, 'state': 3, 'technology': 'Li-poly', 'temperature': 261, 'voltage': 4300}

UA = 'Dalvik/2.1.0 (Linux; U; Android %s; %s Build/PI) com.Level5.YWP/%s' % (OSVER, MODEL, APPVER)
HDR = {'Accept-Encoding': 'identity', 'User-Agent': UA, 'Accept': 'application/json',
       'Content-Type': 'application/json', 'Connection': 'Keep-Alive'}

RC = {0: 'OK', -1: 'IPブロック/復号不可', 20: 'appVerかマスタ版が古い', 30: '署名の不整合',
      32: 'tokenズレ', 37: 'チュートリアル未完了', 101: 'マスタ版が現行と違う', 202: 'BAN'}

# Yポイント（Yポ）はアイテムとして持たれている。IDが違う場合はここを書き換える
YPOINT_ITEM_ID = 80102

# セーブに含まれる「所持品系」キー（レスポンスから差分取得するのに使う）
SAVE_PREFIX = 'ywp_user_'

GAME_CONST = [
    {"constType": 5, "mstKey": "blockComboAdjustNumA", "mstValue": "0.051800"},
    {"constType": 5, "mstKey": "blockComboAdjustNumB", "mstValue": "0.995500"},
    {"constType": 5, "mstKey": "blockSizeAdjustA", "mstValue": "0.000800"},
    {"constType": 5, "mstKey": "blockSizeAdjustB", "mstValue": "0.077000"},
    {"constType": 5, "mstKey": "blockSizeAdjustC", "mstValue": "-0.058500"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillA", "mstValue": "5.780200"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillB", "mstValue": "-2.201000"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillC", "mstValue": "1.000000"},
    {"constType": 5, "mstKey": "blockSizeRate1", "mstValue": "0.019300"},
    {"constType": 5, "mstKey": "blockSizeRate2", "mstValue": "0.058600"},
    {"constType": 5, "mstKey": "blockSizeRate3", "mstValue": "0.137900"},
    {"constType": 5, "mstKey": "comboEnableSec", "mstValue": "3.000000"},
    {"constType": 5, "mstKey": "comboEnableSize", "mstValue": "2"},
    {"constType": 5, "mstKey": "damageSwitchSize", "mstValue": "3"},
    {"constType": 5, "mstKey": "feverDamageAdjustNum", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "feverScoreAdjustNum", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubA", "mstValue": "0.006200"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubB", "mstValue": "-0.024500"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubC", "mstValue": "0.416900"},
    {"constType": 5, "mstKey": "scoreAdjustNumA", "mstValue": "11.223000"},
    {"constType": 5, "mstKey": "scoreAdjustNumB", "mstValue": "0.047700"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize1", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize2", "mstValue": "1.200000"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize3", "mstValue": "2.400000"},
]


# ============================================================================
# 暗号化・復号化関数
# ============================================================================

def salt20(body):
    return hashlib.sha1(K + hashlib.sha1(K + b' ' + body).digest()).digest()


def enc(body):
    pt = salt20(body) + body
    p = 16 - len(pt) % 16
    pt += bytes([p]) * p
    return base64.urlsafe_b64encode(AES.new(AESK, AES.MODE_ECB).encrypt(pt)).decode().rstrip('=')


def dec(s):
    s = s.strip()
    ct = base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
    pt = AES.new(AESK, AES.MODE_ECB).decrypt(ct)
    rest = pt[20:]
    g = rest.find(b'\x1f\x8b')
    if g >= 0:
        try:
            return zlib.decompress(rest[g:], 47)
        except Exception:
            pass
    try:
        rest = rest[:-pt[-1]]
    except Exception:
        pass
    e = max(rest.rfind(b'}'), rest.rfind(b']'))
    return rest[:e + 1] if e >= 0 else rest


def jbody(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


# ============================================================================
# API通信関数
# ============================================================================

def post_nhn(name, obj, timeout=30, retry=3):
    for attempt in range(1, retry + 1):
        try:
            r = requests.post('%s/%s' % (GS, name), data=enc(jbody(obj)),
                              headers={**HDR, 'Host': 'gameserver.yw-p.com'}, timeout=timeout)

            if r.status_code != 200:
                if attempt < retry:
                    time.sleep(2 ** attempt)
                    continue
                return r.status_code, {'resultCode': -1, '_error': f'HTTP{r.status_code}'}

            try:
                out = dec(r.text)
            except Exception:
                if attempt < retry:
                    time.sleep(2 ** attempt)
                    continue
                return r.status_code, {'resultCode': -1, '_raw': (r.text or '')[:200]}

            try:
                return r.status_code, json.loads(out)
            except Exception:
                if attempt < retry:
                    time.sleep(2 ** attempt)
                    continue
                return r.status_code, {'_raw': out[:300].decode('utf-8', 'replace')}

        except requests.exceptions.Timeout:
            if attempt < retry:
                time.sleep(3 * attempt)
                continue
            return None, {'resultCode': -1, '_error': 'Timeout'}
        except requests.exceptions.ConnectionError:
            if attempt < retry:
                time.sleep(3 * attempt)
                continue
            return None, {'resultCode': -1, '_error': 'ConnectionError'}

    return None, {'resultCode': -1, '_error': 'Max retries exceeded'}


def active(udkey=None, timeout=30):
    p = {'apkey': APKEY, 'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android',
         'sign': 'true', 'version': APPVER}
    if udkey:
        p['udkey'] = udkey
    return requests.get('%s/api/v1/active/' % L5, params=p,
                        headers={'User-Agent': UA}, timeout=timeout).json()


def new_udkey():
    return active()['udkey']['value']


def create_gdkey(udkey, timeout=30):
    r = requests.get('%s/api/v1/create_gdkey' % L5,
                     params={'apkey': APKEY, 'udkey': udkey,
                             'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android',
                             'version': APPVER, 'sign': 'true'},
                     headers={'User-Agent': UA}, timeout=timeout).json()
    if not r.get('result'):
        raise RuntimeError('create_gdkey失敗: %s' % r)
    return r['gdkey']['value']


def _parse_forms(html):
    out = []
    for fm in re.finditer(r'<form\b[^>]*>(.*?)</form>', html, re.S | re.I):
        block = fm.group(0)
        am = re.search(r'action="([^"]*)"', block, re.I)
        inputs = {}
        for im in re.finditer(r'<input\b[^>]*>', block, re.I):
            nm = re.search(r'name="([^"]*)"', im.group(0), re.I)
            vm = re.search(r'value="([^"]*)"', im.group(0), re.I)
            if nm:
                inputs[nm.group(1)] = vm.group(1) if vm else ''
        out.append({'action': am.group(1) if am else '', 'inputs': inputs})
    return out


def link_email(udkey, email, pw, timeout=25):
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                      'Accept-Language': 'ja'})
    r = s.get('%s/api/v1/link_account' % L5, params={'apkey': APKEY, 'udkey': udkey},
              allow_redirects=True, timeout=timeout)
    lf = [x for x in _parse_forms(r.text) if any('email' in k.lower() for k in x['inputs'])]
    if not lf:
        raise RuntimeError('ログイン画面が出ない(既に連携済み or メール不正)')
    inp = dict(lf[0]['inputs'])
    inp['form[email]'] = email
    inp['form[password]'] = pw
    r = s.post(urljoin(r.url, lf[0]['action']), data=inp, allow_redirects=True, timeout=timeout)
    ap = [x for x in _parse_forms(r.text) if 'client_id' in x['inputs'] and x['inputs'].get('_method', '') != 'delete']
    if not ap:
        raise RuntimeError('consent画面が出ない(メール/パスが違う?)')
    inp = dict(ap[0]['inputs'])
    inp.setdefault('commit', 'Authorize')
    r2 = s.post(urljoin(r.url, ap[0]['action']), data=inp, allow_redirects=False, timeout=timeout)
    code = (parse_qs(urlparse(r2.headers.get('Location', '')).query).get('code') or [None])[0]
    if not code:
        raise RuntimeError('認可コードが取れない')
    fin = s.get('%s/api/v1/link_account' % L5,
                params={'code': code, 'apkey': APKEY, 'udkey': udkey,
                        'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android'},
                timeout=timeout).json()
    if not fin.get('result'):
        raise RuntimeError('連携finalize失敗: %s' % fin)
    return fin


def rows(s):
    if isinstance(s, str):
        s = json.loads(s) if s.startswith('{') else s
    return [r.split('|') for r in (s or '').split('*') if r] if isinstance(s, str) else (s.get('rows') if isinstance(s, dict) else (s or []))


def parse_item_rows(s):
    """'80102|100313*20502|1' 形式を {itemId: 個数} に変換"""
    out = {}
    if not s:
        return out
    for r in str(s).split('*'):
        if not r:
            continue
        c = r.split('|')
        if len(c) < 2:
            continue
        try:
            out[int(c[0])] = int(c[1])
        except ValueError:
            pass
    return out


def parse_user_data(d):
    """ywp_user_data を dict にして返す（str でも dict でもOK）"""
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            return {}
    return d if isinstance(d, dict) else {}


# ============================================================================
# Clientクラス
# ============================================================================

class Client:
    def __init__(self, udkey):
        self.udkey = udkey
        self.gdkey = None
        self.userId = None
        self.token = '0'
        self.mst = 16897
        self.save = {}

    def _active_with_gdkeys(self, retries=6):
        a = active(self.udkey)
        for _ in range(retries):
            if a.get('gdkeys'):
                break
            time.sleep(1.2)
            a = active(self.udkey)
        if not a.get('gdkeys'):
            raise RuntimeError('gdkeyが無い(このudkeyに紐づくゲームデータが無い)')
        return a

    def _enum(self, a):
        gds = a['gdkeys']
        pl = []
        for _ in range(5):
            rc, j = post_nhn('getGdkeyAccounts.nhn', {
                'appVer': APPVER, 'deviceId': self.udkey,
                'gdkeys': [{'gdkey': g['value']} for g in gds],
                'level5UserId': '0', 'mstVersionVer': self.mst, 'osType': 2,
                'userId': '0', 'ywpToken': '0'})
            pl = j.get('udkeyPlayerList') or []
            if len(pl) >= len(gds):
                break
            time.sleep(1.2)
        by_g = {str(p.get('gdkey')): p for p in pl if p.get('gdkey')}
        out = []
        for i, g in enumerate(gds):
            p = by_g.get(g['value'], {})
            out.append({'idx': i, 'userId': p.get('userId'), 'playerName': p.get('playerName'),
                        'gdkey': g['value'], 'gdsig': g['signature']})
        return out

    def init_nhn(self):
        rc, j = post_nhn('init.nhn', {
            'appGuardDeviceId': hashlib.sha256(self.udkey.encode()).hexdigest(),
            'appVer': APPVER, 'deviceId': self.udkey, 'level5UserId': '0',
            'mstVersionVer': self.mst, 'osType': 2, 'signature': SIGNATURE,
            'userId': '0', 'ywpToken': '0'})
        v = j.get('mstVersionMaster')
        if isinstance(v, int) and v > 0:
            self.mst = v
        return j

    def login(self, userId=None):
        self.init_nhn()
        a = self._active_with_gdkeys()
        accs = self._enum(a)
        sel = None
        if userId is not None:
            sel = next((x for x in accs if str(x['userId']) == str(userId)), None)
            if sel is None:
                raise RuntimeError('userId %s が見つからない' % userId)
        if sel is None:
            sel = accs[0]
        rc, j = post_nhn('login.nhn', {
            'appVer': APPVER, 'batteryInfo': BATTERY, 'deviceId': self.udkey,
            'deviceName': MODEL, 'gdkeySignature': sel['gdsig'], 'gdkeyValue': sel['gdkey'],
            'isL5IDLinked': 1,
            'level5UserId': sel['gdkey'],
            'modelName': MODEL, 'mstVersionVer': self.mst, 'osType': 2, 'osVersion': OSVER,
            'signNonce': a['sign_nonce'], 'signTimestamp': str(a['sign_timestamp']),
            'signature': SIGNATURE, 'udkeySignature': a['udkey']['signature'],
            'udkeyValue': self.udkey, 'userId': sel['userId'], 'ywpToken': '0'})
        if j.get('resultCode') != 0:
            code = j.get('resultCode')
            error_desc = RC.get(code, '不明なエラー')
            error_detail = j.get('_raw') or j.get('_error') or j.get('dialogMsg') or ''
            raise RuntimeError('login失敗: rc=%s (%s) %s' % (code, error_desc, error_detail[:100]))
        self.gdkey, self.userId, self.token = sel['gdkey'], sel['userId'], j.get('token')
        self.save = j
        return j

    def accounts(self):
        self.init_nhn()
        return [{k: v for k, v in a.items() if k != 'gdsig'}
                for a in self._enum(self._active_with_gdkeys())]

    def call(self, name, extra=None):
        if not self.token or self.token == '0':
            raise RuntimeError('先に login() してください')
        body = {'activeDeckId': 1, 'appVer': APPVER, 'deviceId': self.udkey,
                'level5UserId': self.gdkey, 'mstVersionVer': self.mst, 'osType': 2,
                'token': self.token, 'userId': str(self.userId), 'ywpToken': '0'}
        if extra:
            body.update(extra)
        rc, j = post_nhn(name, body)
        t = j.get('token')
        if t and t != 'null':
            self.token = t
        self.merge_save(j)
        return rc, j

    def merge_save(self, j):
        """レスポンスに含まれる ywp_user_* を save に反映（所持数を最新に保つ）"""
        if not isinstance(j, dict) or j.get('resultCode') != 0:
            return False
        merged = False
        for k, v in j.items():
            if k.startswith(SAVE_PREFIX) and v is not None:
                self.save[k] = v
                merged = True
        return merged

    def currency(self):
        """現在の所持通貨。取得できない項目は None"""
        d = parse_user_data(self.save.get('ywp_user_data'))
        items = parse_item_rows(self.save.get('ywp_user_item'))
        ymoney = d.get('ymoney')
        return {
            'ypoint': items.get(YPOINT_ITEM_ID),
            'ymoney': int(ymoney) if isinstance(ymoney, (int, float, str)) and str(ymoney).lstrip('-').isdigit() else None,
        }

    def master(self, key):
        rc, j = self.call('getMaster.nhn', {'key': key})
        if j.get('resultCode') != 0:
            return {}
        return j

    def build_game_end(self, stageId, battleType, start, clear_time_sec=None):
        reqId = start.get('requestId')
        yk = start.get('userYoukaiList') or []
        en = start.get('enemyYoukaiList') or []

        # ★ 敵HPからダメージを計算
        total_enemy_hp = sum(e.get('hp', 0) for e in en)
        if total_enemy_hp <= 0:
            total_enemy_hp = 50000

        overkill = random.randint(int(total_enemy_hp * 0.05), int(total_enemy_hp * 0.15))
        dmg = total_enemy_hp + overkill
        score = dmg   # スコア = ダメージ

        # ★ クリアタイム
        if clear_time_sec is None:
            clear_time_sec = round(random.uniform(8.0, 10.0), 1)
        clear_time_int = int(round(clear_time_sec))

        # ★ 消したぷに数
        erase_num = random.randint(20, 80)

        # ★ コンボ（消したぷに数と整合）
        combo_min = max(5, erase_num // 5)
        combo_max_val = max(combo_min + 1, min(30, erase_num // 2))
        combo_max = random.randint(combo_min, combo_max_val)

        # ★ フィーバーは0固定
        fever_num = 0

        # 最大ぷにサイズ（消したぷに数と連動）
        if erase_num < 30:
            erase_size_max = random.randint(100, 106)
        elif erase_num < 60:
            erase_size_max = random.randint(104, 110)
        else:
            erase_size_max = random.randint(108, 112)

        # 平均サイズ（最大の90〜100%）
        erase_size_ave = random.uniform(erase_size_max * 0.9, erase_size_max * 1.0)

        # 最大同時消し
        if erase_num < 30:
            link_size_max = random.randint(2, 3)
        elif erase_num < 50:
            link_size_max = random.randint(2, 4)
        else:
            link_size_max = random.randint(3, 5)

        # 残HP（クリアタイムと連動）
        if clear_time_int <= 8:
            result_hp = random.randint(600, 700)
        elif clear_time_int == 9:
            result_hp = random.randint(650, 750)
        else:
            result_hp = random.randint(700, 800)

        # 妖怪ごとの消した数・サイズ
        user_erase_num = max(1, erase_num // random.randint(1, 2))
        user_erase_size = random.randint(
            int(erase_size_max * 15),
            int(erase_size_max * 20)
        )

        # users
        users = []
        for i, y in enumerate(yk):
            is_first = (i == 0)
            users.append({
                'damageMax': int(dmg * random.uniform(0.05, 0.07)) if is_first else 0,
                'damageTotal': dmg if is_first else 0,
                'eraseNum': user_erase_num if is_first else 0,
                'eraseSize': user_erase_size if is_first else 0,
                'eraseSizeMax': erase_size_max if is_first else 0,
                'linkSizeMax': link_size_max if is_first else 0,
                'recoveryActual': 0,
                'recoveryMax': 0,
                'sSkillUseNum': 0,
                'skillUseNum': 0,
                'youkaiId': y.get('youkaiId'),
            })

        # enemies
        enemies = []
        for i, e in enumerate(en):
            enemies.append({
                'deadEndOrder': i + 1,
                'deadEndType': 0,
                'dropItemCheckKey': (e.get('lotItemInfoList') or '00000|0').split('|')[0],
                'dropItemFlg': 0,
                'dropItemId': 0,
                'dropTreasureFlg': 0,
                'dropTreasureId': 0,
                'dropYoukaiCheckKey': (e.get('lotYoukaiInfoList') or '00000|0').split('|')[0],
                'dropYoukaiFlg': 0,
                'enemyId': e.get('enemyId'),
                'itemId': 0,
                'useItemLLarge': 0,
                'useItemLarge': 0,
                'useItemMiddle': 0,
                'useItemSmall': 0,
            })

        return {
            'stageId': stageId,
            'battleType': battleType,
            'requestId': str(reqId),
            'damageTotal': dmg,
            'score': score,
            'userYoukaiResultList': users,
            'enemyYoukaiResultList': enemies,
            'bonusBlockNum': 0,
            'cheatFlg': 0,
            'clearTimeLongSec': 0,
            'clearTimeSec': clear_time_int,
            'comboMax': combo_max,
            'eraseNumTotal': erase_num,
            'eraseSizeAve': f"{erase_size_ave:.2f}",
            'eraseSizeMax': erase_size_max,
            'eventPoint': 0,
            'eventSubPoint': 0,
            'eventTeamPoint': 0,
            'feverTimeNum': fever_num,
            'linkSizeMax': link_size_max,
            'pauseAtkNum': 0,
            'recvDamageTotal': 0,
            'resultRecvAtkNum': 0,
            'resultYoukaiHP': result_hp,
            'scoreLog': '',
            'spMissionIntValue1': 0,
            'suspendFlg': 0,
            'themeResultList': [],
            'ywp_mst_game_const': GAME_CONST,
        }

    def battle(self, stageId, battleType=None, wait=None):
        if battleType is None:
            battleType = 6 if str(stageId).startswith(('28805', '28904', '29008', '29304')) else 1
        rc, js = self.call('gameStart.nhn', {'stageId': stageId, 'battleType': battleType,
                                             'battleCode': '', 'retryFlg': 0})
        if js.get('resultCode') != 0:
            return js.get('resultCode'), js

        # ★ プレイ時間をランダム化（8.0〜10.0、0.1秒刻み）
        if wait is None:
            wait = round(random.uniform(8.0, 10.0), 1)
        print(f">>> [battle] プレイ時間 {wait:.1f}秒")
        time.sleep(wait)

        ge = self.build_game_end(stageId, battleType, js, clear_time_sec=wait)
        return self.call('gameEnd.nhn', ge)

    def info(self):
        d = self.save.get('ywp_user_data')
        if isinstance(d, str):
            d = json.loads(d)
        return d or {}

    def youkai(self):
        return [{'id': int(r[0]), 'raw': r} for r in rows(self.save.get('ywp_user_youkai'))]

    def items(self):
        return {int(r[0]): int(r[1]) for r in rows(self.save.get('ywp_user_item')) if len(r) >= 2}

    def stages(self):
        return {int(r[0]): r for r in rows(self.save.get('ywp_user_stage'))}


# ============================================================================
# ログイン関数
# ============================================================================

def login_email(email, pw, userId=None):
    print('  UDkey取得中...')
    udkey = new_udkey()
    print(f'  UDkey: {udkey}')

    print('  メール連携中...')
    link_email(udkey, email, pw)
    print(f'  メール連携成功')

    print('  サーバー反映待機中... 3秒')
    time.sleep(3)

    print('  ゲームサーバーログイン中...')
    c = Client(udkey)
    c.login(userId=userId)

    return c


# ============================================================================
# 複数垢対応
# ============================================================================

def list_accounts(email, pw):
    """
    メアド＋パスワードでログインし、紐づく全垢の一覧を返す。
    戻り値: (udkey, [{"userId": "...", "playerName": "..."}, ...])
    """
    print('  [list_accounts] UDkey取得中...')
    udkey = new_udkey()
    print(f'  [list_accounts] UDkey: {udkey}')

    print('  [list_accounts] メール連携中...')
    link_email(udkey, email, pw)
    print('  [list_accounts] メール連携成功')

    print('  [list_accounts] サーバー反映待機中... 3秒')
    time.sleep(3)

    c = Client(udkey)
    c.init_nhn()
    a = c._active_with_gdkeys()
    accs = c._enum(a)

    players = [
        {"userId": x.get("userId"), "playerName": x.get("playerName")}
        for x in accs if x.get("userId")
    ]
    print(f'  [list_accounts] 検出垢数: {len(players)}')
    for p in players:
        print(f'    - userId={p["userId"]} name={p["playerName"]}')
    return udkey, players


def login_email_with_udkey(email, pw, userId, udkey=None):
    """
    udkey を指定すれば link_email をスキップ（2回目以降の垢ログインが高速）
    戻り値: (Client, udkey)
    """
    if udkey is None:
        print('  [login_email_with_udkey] UDkey取得中...')
        udkey = new_udkey()
        print('  [login_email_with_udkey] メール連携中...')
        link_email(udkey, email, pw)
        print('  [login_email_with_udkey] サーバー反映待機中... 3秒')
        time.sleep(3)

    print(f'  [login_email_with_udkey] userId={userId} でログイン中...')
    c = Client(udkey)
    c.login(userId=userId)
    print(f'  [login_email_with_udkey] ログイン成功: {c.userId}')
    return c, udkey


# ============================================================================
# Cog
# ============================================================================

class YWPAuto(commands.Cog):
    """API・暗号・Client を提供するCog"""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def login_email(email, pw, userId=None):
        return login_email(email, pw, userId)

    @staticmethod
    def list_accounts(email, pw):
        return list_accounts(email, pw)

    @staticmethod
    def login_email_with_udkey(email, pw, userId, udkey=None):
        return login_email_with_udkey(email, pw, userId, udkey)

    @staticmethod
    def Client(udkey):
        return Client(udkey)

    @staticmethod
    def RC():
        return RC


async def setup(bot):
    await bot.add_cog(YWPAuto(bot))