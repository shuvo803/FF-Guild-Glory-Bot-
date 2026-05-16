import os
import json
import time
import queue
import threading
import random
import string
import base64
import asyncio
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from google.protobuf import json_format
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import httpx
import requests as sync_requests

from ff_proto import freefire_pb2, send_like_pb2

# Flask App Setup
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///accounts.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db_url = app.config['SQLALCHEMY_DATABASE_URI']
if db_url and db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url

db = SQLAlchemy(app)

# Garena API Constants
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/UP1A.231005.007)"
RELEASEVERSION = "OB50"
CLIENT_ID = "100067"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

# Database Model
class GuestAccount(db.Model):
    __tablename__ = 'guest_accounts'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uid = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    region = db.Column(db.String(10), default='IND')
    is_working = db.Column(db.Boolean, default=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, nullable=True)
    glory_contributed = db.Column(db.Integer, default=0)
    matches_played = db.Column(db.Integer, default=0)

    def to_dict(self):
        d = {
            'uid': self.uid,
            'region': self.region,
            'is_working': self.is_working,
            'glory_contributed': self.glory_contributed,
            'matches_played': self.matches_played,
        }
        if self.added_at:
            d['added_at'] = self.added_at.isoformat()
        if self.last_used:
            d['last_used'] = self.last_used.isoformat()
        return d

with app.app_context():
    db.create_all()

# Guild Info Cache
guild_info_cache = {}
GUILD_INFO_CACHE_TTL = 300

def fetch_guild_info(guild_id, region='IND'):
    now = time.time()
    cache_key = f"{guild_id}_{region}"
    if cache_key in guild_info_cache:
        cached = guild_info_cache[cache_key]
        if now - cached['timestamp'] < GUILD_INFO_CACHE_TTL:
            return cached['data']

    guild_data = {
        'guildFound': False,
        'guildId': guild_id,
        'guildName': 'Unknown Guild',
        'guildLevel': 0,
        'memberNum': 0,
        'capacity': 35,
        'guildOwner': '',
        'region': region
    }

    try:
        url = f"https://free-ff-api-src-5plp.onrender.com/api/v1/guildInfo?region={region}&guildID={guild_id}"
        r = sync_requests.get(url, timeout=8)
        if r.status_code == 200:
            d = r.json()
            n = d.get('clanName', '') or d.get('GuildName', '') or 'Unknown'
            lvl = d.get('clanLevel', 0) or d.get('GuildLevel', 0)
            mem = d.get('memberNum', 0) or d.get('GuildMember', 0)
            cap = d.get('capacity', 35) or d.get('GuildCapacity', 35)
            owner = d.get('captainId', '') or d.get('GuildOwner', '')
            guild_data.update({
                'guildFound': True,
                'guildName': n,
                'guildLevel': lvl,
                'memberNum': mem,
                'capacity': cap,
                'guildOwner': owner,
            })
            guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
            return guild_data
    except Exception:
        pass

    try:
        url = f"https://www.public.freefireinfo.site/api/info/{region.lower()}/{guild_id}?key=astute_ff"
        r = sync_requests.get(url, timeout=8)
        if r.status_code == 200:
            d = r.json()
            clan = d.get('clanBasicInfo', None)
            if clan and clan.get('clanName', ''):
                guild_data.update({
                    'guildFound': True,
                    'guildName': clan.get('clanName', 'Unknown'),
                    'guildLevel': clan.get('clanLevel', 0),
                    'memberNum': clan.get('memberNum', 0),
                    'guildOwner': clan.get('captainId', ''),
                })
                guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
                return guild_data
    except Exception:
        pass

    guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
    return guild_data

# Free Fire JWT + API Functions
def aes_encrypt(key, iv, plaintext):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

async def get_access_token(uid, password):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = f"uid={uid}&password={password}&response_type=token&client_type=2&client_secret={CLIENT_SECRET}&client_id={CLIENT_ID}"
    headers = {
        'User-Agent': USER_AGENT,
        'Connection': 'Keep-Alive',
        'Accept-Encoding': 'gzip',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers, timeout=15)
        data = resp.json()
        return data.get("access_token", "0"), data.get("open_id", "0")

async def create_jwt(uid, password):
    access_token, open_id = await get_access_token(uid, password)
    if access_token == "0":
        try:
            r = sync_requests.get(f"https://jwt-gen-api-v2.onrender.com/token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('token', '0')
            if token != '0':
                region = d.get('lockRegion', '') or d.get('region', '') or 'IND'
                return token, region
        except Exception:
            pass
        try:
            r = sync_requests.get(f"https://grant-access-token.deno.dev/get_token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('access_token', '') or d.get('token', '0')
            if token != '0':
                return token, 'IND'
        except Exception:
            pass
        raise ValueError("Failed to get access token")

    login_data = {
        "open_id": open_id,
        "open_id_type": "4",
        "login_token": access_token,
        "orign_platform_type": "4",
    }
    json_data = json.dumps(login_data)
    proto_msg = json_format.ParseDict(json.loads(json_data), freefire_pb2.LoginReq())
    proto_bytes = proto_msg.SerializeToString()
    payload = aes_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)

    url = "https://loginbp.ggblueshark.com/MajorLogin"
    headers = {
        'User-Agent': USER_AGENT,
        'Connection': 'Keep-Alive',
        'Accept-Encoding': 'gzip',
        'Content-Type': 'application/octet-stream',
        'Expect': '100-continue',
        'X-Unity-Version': '2018.4.11f1',
        'X-GA': 'v1 1',
        'ReleaseVersion': RELEASEVERSION,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers, timeout=15)
        login_res = freefire_pb2.LoginRes()
        login_res.ParseFromString(resp.content)
        msg = json.loads(json_format.MessageToJson(login_res))
        token = msg.get("token", "0")
        region = msg.get("lockRegion", "0")
        if token == "0":
            raise ValueError("Failed to get JWT")
        return token, region

def create_like_payload(target_uid, region):
    msg = send_like_pb2.like()
    msg.uid = int(target_uid)
    msg.region = region
    proto_bytes = msg.SerializeToString()
    return aes_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)

def get_base_url(region):
    if region == "IND":
        return "https://client.ind.freefiremobile.com"
    return "https://clientbp.ggblueshark.com"

async def send_ff_request(guest_uid, guest_password, target_uid, region):
    jwt, lock_region = await create_jwt(guest_uid, guest_password)
    payload = create_like_payload(target_uid, region)
    base_url = get_base_url(region)
    headers = {
        "User-Agent": USER_AGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/octet-stream",
        "Expect": "100-continue",
        "Authorization": f"Bearer {jwt}",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": RELEASEVERSION,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/LikeProfile", data=payload, headers=headers, timeout=30)
        return resp.status_code, jwt[:30] + "..."

def send_ff_sync(uid, pwd, target, region):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        status, jwt_preview = loop.run_until_complete(send_ff_request(uid, pwd, target, region))
        result = {"success": False, "status": status, "jwt": jwt_preview}
        if status == 200:
            result["success"] = True
        return result
    except Exception as e:
        msg = str(e)
        if len(msg) > 120:
            msg = msg[:120]
        return {"success": False, "message": msg}

# Bot State Engine
class BotState:
    def __init__(self):
        self.guild_id = ""
        self.region = "IND"
        self.guild_info = {}
        self.running = False
        self.bots = {}
        self.stats = {
            'total_glory': 0,
            'matches': 0,
            'success': 0,
            'failed': 0,
            'glory_per_hour': 0,
            'active_bots': 0,
            'bot_count': 0,
            'start_time': None,
            'running_hours': 0,
        }
        self.lock = threading.Lock()

bot_state = BotState()
sse_queues = []
sse_lock = threading.Lock()

def broadcast():
    with bot_state.lock:
        if bot_state.stats['start_time']:
            e = time.time() - bot_state.stats['start_time']
            bot_state.stats['running_hours'] = round(e / 3600, 2)
            if e > 0:
                bot_state.stats['glory_per_hour'] = int(bot_state.stats['success'] * 4 / (e / 3600))
        active = 0
        for b in bot_state.bots.values():
            if b.get('status') in ('connecting', 'playing', 'success'):
                active += 1
        bot_state.stats['active_bots'] = active
        s = {
            'guild_id': bot_state.guild_id,
            'guild_info': bot_state.guild_info,
            'running': bot_state.running,
            'bots': dict(bot_state.bots),
            'stats': dict(bot_state.stats),
            'timestamp': datetime.utcnow().isoformat(),
        }
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(s)
            except Exception:
                dead.append(q)
        for d in dead:
            if d in sse_queues:
                sse_queues.remove(d)

def _broadcaster():
    while True:
        broadcast()
        time.sleep(2)

threading.Thread(target=_broadcaster, daemon=True).start()

def bot_worker(bot_id, uid, password, guild_id, region):
    key = f"bot_{bot_id}"
    total_sent = 0
    total_ok = 0
    while bot_state.running:
        bd = {
            'uid': uid,
            'status': 'connecting',
            'sent': total_sent,
            'ok': total_ok,
            'last_active': time.time(),
        }
        with bot_state.lock:
            bot_state.bots[key] = bd
        result = send_ff_sync(uid, password, guild_id, region)
        total_sent += 1
        if result.get('success'):
            total_ok += 1
            bd = {
                'uid': uid,
                'status': 'success',
                'sent': total_sent,
                'ok': total_ok,
                'last_active': time.time(),
                'jwt': result.get('jwt', ''),
            }
            with bot_state.lock:
                bot_state.stats['success'] += 1
                bot_state.stats['total_glory'] += 4
                bot_state.stats['matches'] += 1
                bot_state.bots[key] = bd
        else:
            bd = {
                'uid': uid,
                'status': 'failed',
                'sent': total_sent,
                'ok': total_ok,
                'last_active': time.time(),
                'error': result.get('message', ''),
            }
            with bot_state.lock:
                bot_state.stats['failed'] += 1
                bot_state.bots[key] = bd
        time.sleep(random.randint(60, 180))

# Flask Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/health')
def health():
    ac = GuestAccount.query.count()
    return jsonify({'status': 'alive', 'accounts': ac, 'time': datetime.utcnow().isoformat()})

@app.route('/api/status')
def api_status():
    broadcast()
    with bot_state.lock:
        return jsonify({
            'guild_id': bot_state.guild_id,
            'guild_info': bot_state.guild_info,
            'running': bot_state.running,
            'bots': dict(bot_state.bots),
            'stats': dict(bot_state.stats),
        })

@app.route('/api/status/stream')
def status_stream():
    q = queue.Queue()
    with sse_lock:
        sse_queues.append(q)

    def generate():
        try:
            init_data = {
                'guild_id': bot_state.guild_id,
                'guild_info': bot_state.guild_info,
                'running': bot_state.running,
                'bots': dict(bot_state.bots),
                'stats': dict(bot_state.stats),
            }
            yield f"data: {json.dumps(init_data)}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_queues:
                    sse_queues.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

@app.route('/api/guild/info')
def api_guild_info():
    guild_id = request.args.get('guild_id', '').strip()
    region = request.args.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'error': 'Valid guild_id required'})
    info = fetch_guild_info(guild_id, region)
    return jsonify(info)

@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    guild_id = data.get('guild_id', '').strip()
    bot_count = int(data.get('bot_count', 5))
    region = data.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'success': False, 'message': 'Valid numeric Guild ID required'})
    accounts = GuestAccount.query.filter_by(is_working=True).order_by(GuestAccount.last_used).limit(bot_count).all()
    if len(accounts) < bot_count:
        msg = f'Need {bot_count} working accounts, have {len(accounts)}. Generate more!'
        return jsonify({'success': False, 'message': msg})
    if bot_state.running:
        bot_state.running = False
        time.sleep(1)
    guild_info = fetch_guild_info(guild_id, region)
    with bot_state.lock:
        bot_state.guild_id = guild_id
        bot_state.guild_info = guild_info
        bot_state.region = region
        bot_state.running = True
        bot_state.bots = {}
        bot_state.stats = {
            'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
            'glory_per_hour': 0, 'active_bots': 0, 'bot_count': bot_count,
            'start_time': time.time(), 'running_hours': 0,
        }
    for i, acc in enumerate(accounts):
        t = threading.Thread(
            target=bot_worker,
            args=(i, acc.uid, acc.password, guild_id, region),
            daemon=True
        )
        t.start()
        bd = {
            'uid': acc.uid, 'status': 'starting',
            'sent': 0, 'ok': 0, 'last_active': time.time(),
        }
        with bot_state.lock:
            bot_state.bots[f"bot_{i}"] = bd
        acc.last_used = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': True, 'message': f'Started {bot_count} bots for guild {guild_id}', 'guild_info': guild_info})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    bot_state.running = False
    with bot_state.lock:
        bot_state.bots = {}
    return jsonify({'success': True, 'message': 'Bot stopped'})

@app.route('/api/guests/count')
def api_guests_count():
    total = GuestAccount.query.count()
    working = GuestAccount.query.filter_by(is_working=True).count()
    return jsonify({'total': total, 'working': working})

@app.route('/api/guests/add', methods=['POST'])
def api_guests_add():
    data = request.get_json()
    uid = data.get('uid', '').strip()
    password = data.get('password', '').strip()
    region = data.get('region', 'IND')
    if not uid or not password:
        return jsonify({'success': False, 'message': 'UID and password required'})
    existing = GuestAccount.query.filter_by(uid=uid).first()
    if existing:
        return jsonify({'success': False, 'message': 'Account already exists'})
    acc = GuestAccount(uid=uid, password=password, region=region)
    db.session.add(acc)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Account added'})

@app.route('/api/guests/generate', methods=['POST'])
def api_guests_generate():
    data = request.get_json()
    count = int(data.get('count', 10))
    region = data.get('region', 'IND')
    generated = []
    for _ in range(count):
        fake_uid = str(random.randint(1000000000, 9999999999))
        fake_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        existing = GuestAccount.query.filter_by(uid=fake_uid).first()
        if not existing:
            acc = GuestAccount(uid=fake_uid, password=fake_pass, region=region)
            db.session.add(acc)
            generated.append(acc.to_dict())
    db.session.commit()
    return jsonify({'generated': len(generated), 'accounts': generated})

@app.route('/api/guests/delete', methods=['POST'])
def api_guests_delete():
    data = request.get_json()
    uid = data.get('uid', '')
    acc = GuestAccount.query.filter_by(uid=uid).first()
    if acc:
        db.session.delete(acc)
        db.session.commit()
    return jsonify({'success': True})

@app.route('/api/guests/list')
def api_guests_list():
    page = request.args.get('page', 1, type=int)
    accounts = GuestAccount.query.order_by(GuestAccount.added_at.desc()).paginate(page=page, per_page=50)
    items = [a.to_dict() for a in accounts.items]
    return jsonify({'accounts': items, 'total': accounts.total, 'pages': accounts.pages, 'page': page})

@app.route('/api/test/guest', methods=['POST'])
def api_test_guest():
    data = request.get_json()
    uid = data.get('uid', '')
    password = data.get('password', '')
    target = data.get('target', '0000000000')
    result = send_ff_sync(uid, password, target, 'IND')
    return jsonify(result)

# Main
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)tive_bots': 0,
            'bot_count': 0,
            'start_time': None,
            'running_hours': 0
        }
        self.lock = threading.Lock()

bot_state = BotState()
sse_queues = []
sse_lock = threading.Lock()

def broadcast():
    with bot_state.lock:
        if bot_state.stats['start_time']:
            e = time.time() - bot_state.stats['start_time']
            bot_state.stats['running_hours'] = round(e / 3600, 2)
            if e > 0:
                bot_state.stats['glory_per_hour'] = int(
                    bot_state.stats['success'] * 4 / (e / 3600)
                )
        bot_state.stats['active_bots'] = sum(
            1 for b in bot_state.bots.values()
            if b.get('status') in ('connecting', 'playing', 'success')
        )
        s = {
            'guild_id': bot_state.guild_id,
            'guild_info': bot_state.guild_info,
            'running': bot_state.running,
            'bots': dict(bot_state.bots),
            'stats': dict(bot_state.stats),
            'timestamp': datetime.utcnow().isoformat()
        }
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(s)
            except:
                dead.append(q)
        for d in dead:
            if d in sse_queues:
                sse_queues.remove(d)

def _broadcaster():
    while True:
        broadcast()
        time.sleep(2)

threading.Thread(target=_broadcaster, daemon=True).start()

def bot_worker(bot_id, uid, password, guild_id, region):
    key = f"bot_{bot_id}"
    total_sent = 0
    total_ok = 0
    while bot_state.running:
        with bot_state.lock:
            bot_state.bots[key] = {
                'uid': uid,
                'status': 'connecting',
                'sent': total_sent,
                'ok': total_ok,
                'last_active': time.time()
            }
        result = send_ff_sync(uid, password, guild_id, region)
        total_sent += 1
        if result.get('success'):
            total_ok += 1
            with bot_state.lock:
                bot_state.stats['success'] += 1
                bot_state.stats['total_glory'] += 4
                bot_state.stats['matches'] += 1
                bot_state.bots[key] = {
                    'uid': uid,
                    'status': 'success',
                    'sent': total_sent,
                    'ok': total_ok,
                    'last_active': time.time(),
                    'jwt': result.get('jwt', '')
                }
        else:
            with bot_state.lock:
                bot_state.stats['failed'] += 1
                bot_state.bots[key] = {
                    'uid': uid,
                    'status': 'failed',
                    'sent': total_sent,
                    'ok': total_ok,
                    'last_active': time.time(),
                    'error': result.get('message', '')
                }
        time.sleep(random.randint(60, 180))

# ─── Flask Routes ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/health')
def health():
    return jsonify({
        'status': 'alive',
        'accounts': GuestAccount.query.count(),
        'time': datetime.utcnow().isoformat()
    })

@app.route('/api/status')
def api_status():
    broadcast()
    with bot_state.lock:
        return jsonify({
            'guild_id': bot_state.guild_id,
            'guild_info': bot_state.guild_info,
            'running': bot_state.running,
            'bots': dict(bot_state.bots),
            'stats': dict(bot_state.stats)
        })

@app.route('/api/status/stream')
def status_stream():
    q = queue.Queue()
    with sse_lock:
        sse_queues.append(q)

    def generate():
        try:
            with bot_state.lock:
                init = json.dumps({
                    'guild_id': bot_state.guild_id,
                    'guild_info': bot_state.guild_info,
                    'running': bot_state.running,
                    'bots': dict(bot_state.bots),
                    'stats': dict(bot_state.stats)
                })
                yield f"data: {init}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_queues:
                    sse_queues.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/guild/info')
def api_guild_info():
    guild_id = request.args.get('guild_id', '').strip()
    region = request.args.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'error': 'Valid guild_id required'})
    info = fetch_guild_info(guild_id, region)
    return jsonify(info)

@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    guild_id = data.get('guild_id', '').strip()
    bot_count = int(data.get('bot_count', 5))
    region = data.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'success': False, 'message': 'Valid numeric Guild ID required'})
    accounts = GuestAccount.query.filter_by(is_working=True).order_by(GuestAccount.last_used).limit(bot_count).all()
    if len(accounts) < bot_count:
        return jsonify({
            'success': False,
            'message': f'Need {bot_count} working accounts, have {len(accounts)}. Generate more!'
        })
    if bot_state.running:
        bot_state.running = False
        time.sleep(1)
    guild_info = fetch_guild_info(guild_id, region)
    with bot_state.lock:
        bot_state.guild_id = guild_id
        bot_state.guild_info = guild_info
        bot_state.region = region
        bot_state.running = True
        bot_state.bots = {}
        bot_state.stats = {
            'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
            'glory_per_hour': 0, 'active_bots': 0, 'bot_count': bot_count,
            'start_time': time.time(), 'running_hours': 0
        }
    for i, acc in enumerate(accounts):
        t = threading.Thread(
            target=bot_worker,
            args=(i, acc.uid, acc.password, guild_id, region),
            daemon=True
        )
        t.start()
        with bot_state.lock:
            bot_state.bots[f"bot_{i}"] = {
                'uid': acc.uid, 'status': 'starting',
                'sent': 0, 'ok': 0, 'last_active': time.time()
            }
        acc.last_used = datetime.utcnow()
        db.session.commit()
    return jsonify({
        'success': True,
        'message': f'Started {bot_count} bots for guild {guild_id}',
        'guild_info': guild_info
    })

@app.route('/api/stop', methods=['POST'])
def api_stop():
    bot_state.running = False
    with bot_state.lock:
        bot_state.bots = {}
    return jsonify({'success': True, 'message': 'Bot stopped'})

@app.route('/api/guests/count')
def api_guests_count():
    return jsonify({
        'total': GuestAccount.query.count(),
        'working': GuestAccount.query.filter_by(is_working=True).count()
    })

@app.route('/api/guests/add', methods=['POST'])
def api_guests_add():
    data = request.get_json()
    uid = data.get('uid', '').strip()
    password = data.get('password', '').strip()
    region = data.get('region', 'IND')
    if not uid or not password:
        return jsonify({'success': False, 'message': 'UID and password required'})
    existing = GuestAccount.query.filter_by(uid=uid).first()
    if existing:
        return jsonify({'success': False, 'message': 'Account already exists'})
    acc = GuestAccount(uid=uid, password=password, region=region)
    db.session.add(acc)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Account added'})

@app.route('/api/guests/generate', methods=['POST'])
def api_guests_generate():
    data = request.get_json()
    count = int(data.get('count', 10))
    region = data.get('region', 'IND')
    generated = []
    for _ in range(count):
        fake_uid = str(random.randint(1000000000, 9999999999))
        fake_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        existing = GuestAccount.query.filter_by(uid=fake_uid).first()
        if not existing:
            acc = GuestAccount(uid=fake_uid, password=fake_pass, region=region)
            db.session.add(acc)
            generated.append(acc.to_dict())
    db.session.commit()
    return jsonify({'generated': len(generated), 'accounts': generated})

@app.route('/api/guests/delete', methods=['POST'])
def api_guests_delete():
    data = request.get_json()
    uid = data.get('uid', '')
    acc = GuestAccount.query.filter_by(uid=uid).first()
    if acc:
        db.session.delete(acc)
        db.session.commit()
    return jsonify({'success': True})

@app.route('/api/guests/list')
def api_guests_list():
    page = request.args.get('page', 1, type=int)
    accounts = GuestAccount.query.order_by(GuestAccount.added_at.desc()).paginate(page=page, per_page=50)
    return jsonify({
        'accounts': [a.to_dict() for a in accounts.items],
        'total': accounts.total,
        'pages': accounts.pages,
        'page': page
    })

@app.route('/api/test/guest', methods=['POST'])
def api_test_guest():
    data = request.get_json()
    uid = data.get('uid', '')
    password = data.get('password', '')
    target = data.get('target', '0000000000')
    result = send_ff_sync(uid, password, target, "IND")
    return jsonify(result)

# ─── Main Entry Point ────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.en        'guildName': 'Unknown Guild',
        'guildLevel': 0,
        'memberNum': 0,
        'capacity': 35,
        'guildOwner': '',
        'region': region
    }

    # Method 1: free-ff-api 
    try:
        r = sync_requests.get(f"https://free-ff-api-src-5plp.onrender.com/api/v1/guildInfo?region={region}&guildID={guild_id}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            guild_data.update({
                'guildFound': True,
                'guildName': d.get('clanName', d.get('guildName', d.get('GuildName', 'Unknown'))),
                'guildLevel': d.get('clanLevel', d.get('guildLevel', d.get('GuildLevel', 0))),
                'memberNum': d.get('memberNum', d.get('guildMember', d.get('GuildMember', 0))),
                'capacity': d.get('capacity', d.get('guildCapacity', d.get('GuildCapacity', 35))),
                'guildOwner': d.get('captainId', d.get('guildOwner', d.get('GuildOwner', ''))),
            })
            guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
            return guild_data
    except: pass

    # Method 2: Free Fire Community API with captain lookup
    # Try to find a guild member by checking some known patterns
    # For demo, we use the guild ID directly

    guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
    return guild_data

# ─── Free Fire JWT + API ─────────────────────────────────────────────────

def aes_encrypt(key, iv, plaintext):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

async def get_access_token(uid, password):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = f"uid={uid}&password={password}&response_type=token&client_type=2&client_secret={CLIENT_SECRET}&client_id={CLIENT_ID}"
    headers = {'User-Agent': USER_AGENT, 'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
               'Content-Type': 'application/x-www-form-urlencoded'}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers, timeout=15)
        data = resp.json()
        return data.get("access_token", "0"), data.get("open_id", "0")

async def create_jwt(uid, password):
    access_token, open_id = await get_access_token(uid, password)
    if access_token == "0":
        try:
            r = sync_requests.get(f"https://jwt-gen-api-v2.onrender.com/token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('token', '0')
            if token != '0': return token, d.get('lockRegion', d.get('region', 'IND'))
        except: pass
        try:
            r = sync_requests.get(f"https://grant-access-token.deno.dev/get_token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('access_token', d.get('token', '0'))
            if token != '0': return token, 'IND'
        except: pass
        raise ValueError("Failed to get access token")

    json_data = json.dumps({"open_id": open_id, "open_id_type": "4",
                           "login_token": access_token, "orign_platform_type": "4"})
    proto_msg = json_format.ParseDict(json.loads(json_data), freefire_pb2.LoginReq())
    payload = aes_encrypt(MAIN_KEY, MAIN_IV, proto_msg.SerializeToString())

    headers = {'User-Agent': USER_AGENT, 'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
               'Content-Type': 'application/octet-stream', 'Expect': '100-continue',
               'X-Unity-Version': '2018.4.11f1', 'X-GA': 'v1 1', 'ReleaseVersion': RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://loginbp.ggblueshark.com/MajorLogin", data=payload, headers=headers, timeout=15)
        login_res = freefire_pb2.LoginRes()
        login_res.ParseFromString(resp.content)
        msg = json.loads(json_format.MessageToJson(login_res))
        token = msg.get("token", "0")
        region = msg.get("lockRegion", "0")
        if token == "0": raise ValueError("Failed to get JWT")
        return token, region

def create_like_payload(target_uid, region):
    msg = send_like_pb2.like()
    msg.uid = int(target_uid)
    msg.region = region
    return aes_encrypt(MAIN_KEY, MAIN_IV, msg.SerializeToString())

def get_base_url(region):
    if region == "IND": return "https://client.ind.freefiremobile.com"
    return "https://clientbp.ggblueshark.com"

async def send_ff_request(guest_uid, guest_password, target_uid, region):
    jwt, lock_region = await create_jwt(guest_uid, guest_password)
    payload = create_like_payload(target_uid, region)
    base_url = get_base_url(region)
    headers = {"User-Agent": USER_AGENT, "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
               "Content-Type": "application/octet-stream", "Expect": "100-continue",
               "Authorization": f"Bearer {jwt}", "X-Unity-Version": "2018.4.11f1",
               "X-GA": "v1 1", "ReleaseVersion": RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/LikeProfile", data=payload, headers=headers, timeout=30)
        return resp.status_code, jwt[:30] + "..."

def send_ff_sync(uid, pwd, target, region):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        status, jwt_preview = loop.run_until_complete(send_ff_request(uid, pwd, target, region))
        return {"success": status == 200, "status": status, "jwt": jwt_preview}
    except Exception as e:
        return {"success": False, "message": str(e)[:120]}

# ─── Bot State Engine ─────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.guild_id = ""
        self.region = "IND"
        self.guild_info = {}
        self.running = False
        self.bots = {}
        self.stats = {'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
                      'glory_per_hour': 0, 'active_bots': 0, 'bot_count': 0,
                      'start_time': None, 'running_hours': 0}
        self.lock = threading.Lock()

bot_state = BotState()
sse_queues, sse_lock = [], threading.Lock()

def broadcast():
    with bot_state.lock:
        if bot_state.stats['start_time']:
            e = time.time() - bot_state.stats['start_time']
            bot_state.stats['running_hours'] = round(e / 3600, 2)
            bot_state.stats['glory_per_hour'] = int(bot_state.stats['success'] * 4 / (e / 3600)) if e > 0 else 0
        bot_state.stats['active_bots'] = sum(1 for b in bot_state.bots.values()
                                            if b.get('status') in ('connecting', 'playing', 'success'))
        s = {'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info,
             'running': bot_state.running, 'bots': dict(bot_state.bots),
             'stats': dict(bot_state.stats), 'timestamp': datetime.utcnow().isoformat()}
    with sse_lock:
        for q in sse_queues:
            try: q.put_nowait(s)
            except: pass

threading.Thread(target=lambda: [time.sleep(2) or broadcast() for _ in iter(int,1)], daemon=True).start()

def bot_worker(bot_id, uid, password, guild_id, region):
    key = f"bot_{bot_id}"
    total_sent, total_ok = 0, 0
    while bot_state.running:
        with bot_state.lock:
            bot_state.bots[key] = {'uid': uid, 'status': 'connecting',
                                   'sent': total_sent, 'ok': total_ok, 'last_active': time.time()}
        result = send_ff_sync(uid, password, guild_id, region)
        total_sent += 1
        if result.get('success'):
            total_ok += 1
            with bot_state.lock:
                bot_state.stats['success'] += 1
                bot_state.stats['total_glory'] += 4
                bot_state.stats['matches'] += 1
                bot_state.bots[key] = {'uid': uid, 'status': 'success', 'sent': total_sent,
                                       'ok': total_ok, 'last_active': time.time(), 'jwt': result.get('jwt','')}
        else:
            with bot_state.lock:
                bot_state.stats['failed'] += 1
                bot_state.bots[key] = {'uid': uid, 'status': 'failed', 'sent': total_sent,
                                       'ok': total_ok, 'last_active': time.time(), 'error': result.get('message','')}
        time.sleep(random.randint(60, 180))

# ─── Flask Routes ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/health')
def health():
    return jsonify({'status': 'alive', 'accounts': GuestAccount.query.count(), 'time': datetime.utcnow().isoformat()})

@app.route('/api/status')
def api_status():
    broadcast()
    with bot_state.lock:
        return jsonify({'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info,
                        'running': bot_state.running, 'bots': dict(bot_state.bots),
                        'stats': dict(bot_state.stats)})

@app.route('/api/status/stream')
def status_stream():
    q = queue.Queue()
    with sse_lock: sse_queues.append(q)
    def gen():
        try:
            with bot_state.lock:
                yield f"data: {json.dumps({'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info, 'running': bot_state.running, 'bots': dict(bot_state.bots), 'stats': dict(bot_state.stats)})}\n\n"
            while True:
                try: yield f"data: {json.dumps(q.get(timeout=25))}\n\n"
                except queue.Empty: yield ": keepalive\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_queues: sse_queues.remove(q)
    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/guild/info')
def api_guild_info():
    guild_id = request.args.get('guild_id', '').strip()
    region = request.args.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'error': 'Valid guild_id required'})
    info = fetch_guild_info(guild_id, region)
    return jsonify(info)

@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    guild_id = data.get('guild_id', '').strip()
    bot_count = int(data.get('bot_count', 5))
    region = data.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'success': False, 'message': 'Valid numeric Guild ID required'})
    accounts = GuestAccount.query.filter_by(is_working=True).order_by(GuestAccount.last_used).limit(bot_count).all()
    if len(accounts) < bot_count:
        return jsonify({'success': False, 'message': f'Need {bot_count} working accounts, have {len(accounts)}. Generate more!'})
    if bot_state.running:
        bot_state.running = False
        time.sleep(1)
    guild_info = fetch_guild_info(guild_id, region)
    with bot_state.lock:
        bot_state.guild_id = guild_id
        bot_state.guild_info = guild_info
        bot_state.region = region
        bot_state.running = True
        bot_state.bots = {}
        bot_state.stats = {'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
                          'glory_per_hour': 0, 'active_bots': 0, 'bot_count': bot_count,
                          'start_time': time.time(), 'running_hours': 0}
    for i, acc in enumerate(accounts):
        t = threading.Thread(target=bot_worker, args=(i, acc.uid, acc.password, guild_id, region), daemon=True)
        t.start()
        with bot_state.lock:
            bot_state.bots[f"bot_{i}"] = {'uid': acc.uid, 'status': 'starting', 'sent': 0, 'ok': 0, 'last_active': time.time()}
        acc.last_used = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': True, 'message': f'Started {bot_count} bots for guild {guild_id}', 'guild_info': guild_info})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    bot_state.running = False
    with bot_state.lock: bot_state.bots = {}
    return jsonify({'success': True, 'message': 'Bot stopped'})

@app.route('/api/guests/count')
def api_guests_count():
    return jsonify({'total': GuestAccount.query.count(), 'working': GuestAccount.query.filter_by(is_working=True).count()})

@app.route('/api/guests/add', methods=['POST'])
def api_guests_add():
    data = request.get_json()
    uid, password = data.get('uid', '').strip(), data.get('password', '').strip()
    region = data.get('region', 'IND')
    if not uid or not password:
        return jsonify({'success': False, 'message': 'UID and password required'})
    if GuestAccount.query.filter_by(uid=uid).first():
        return jsonify({'success': False, 'message': 'Account already exists'})
    acc = GuestAccount(uid=uid, password=password, region=region)
    db.session.add(acc)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Account added'})

@app.route('/api/guests/generate', methods=['POST'])
def api_guests_generate():
    data = request.get_json()
    count = int(data.get('count', 10))
    region = data.get('region', 'IND')
    generated = []
    for _ in range(count):
        fake_uid = str(random.randint(1000000000, 9999999999))
        fake_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        if not GuestAccount.query.filter_by(uid=fake_uid).first():
            acc = GuestAccount(uid=fake_uid, password=fake_pass, region=region)
            db.session.add(acc)
            generated.append(acc.to_dict())
    db.session.commit()
    return jsonify({'generated': len(generated), 'accounts': generated})

@app.route('/api/guests/delete', methods=['POST'])
def api_guests_delete():
    data = request.get_json()
    acc = GuestAccount.query.filter_by(uid=data.get('uid', '')).first()
    if acc: db.session.delete(acc); db.session.commit()
    return jsonify({'success': True})

@app.route('/api/test/guest', methods=['POST'])
def api_test_guest():
    data = request.get_json()
    uid, password = data.get('uid', ''), data.get('password', '')
    target = data.get('target', '0000000000')
    return jsonify(send_ff_sync(uid, password, target, "IND"))

@app.route('/api/guests/list')
def api_guests_list():
    page = request.args.get('page', 1, type=int)
    accounts = GuestAccount.query.order_by(GuestAccount.added_at.desc()).paginate(page=page, per_page=50)
    return jsonify({'accounts': [a.to_dict() for a in accounts.items], 'total': accounts.total})

# ─── Main ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)        'capacity': 35,
        'guildOwner': '',
        'region': region
    }

    # Method 1: free-ff-api 
    try:
        r = sync_requests.get(f"https://free-ff-api-src-5plp.onrender.com/api/v1/guildInfo?region={region}&guildID={guild_id}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            guild_data.update({
                'guildFound': True,
                'guildName': d.get('clanName', d.get('guildName', d.get('GuildName', 'Unknown'))),
                'guildLevel': d.get('clanLevel', d.get('guildLevel', d.get('GuildLevel', 0))),
                'memberNum': d.get('memberNum', d.get('guildMember', d.get('GuildMember', 0))),
                'capacity': d.get('capacity', d.get('guildCapacity', d.get('GuildCapacity', 35))),
                'guildOwner': d.get('captainId', d.get('guildOwner', d.get('GuildOwner', ''))),
            })
            guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
            return guild_data
    except: pass

    # Method 2: Free Fire Community API with captain lookup
    # Try to find a guild member by checking some known patterns
    # For demo, we use the guild ID directly

    guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
    return guild_data

# ─── Free Fire JWT + API ─────────────────────────────────────────────────

def aes_encrypt(key, iv, plaintext):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

async def get_access_token(uid, password):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = f"uid={uid}&password={password}&response_type=token&client_type=2&client_secret={CLIENT_SECRET}&client_id={CLIENT_ID}"
    headers = {'User-Agent': USER_AGENT, 'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
               'Content-Type': 'application/x-www-form-urlencoded'}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers, timeout=15)
        data = resp.json()
        return data.get("access_token", "0"), data.get("open_id", "0")

async def create_jwt(uid, password):
    access_token, open_id = await get_access_token(uid, password)
    if access_token == "0":
        try:
            r = sync_requests.get(f"https://jwt-gen-api-v2.onrender.com/token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('token', '0')
            if token != '0': return token, d.get('lockRegion', d.get('region', 'IND'))
        except: pass
        try:
            r = sync_requests.get(f"https://grant-access-token.deno.dev/get_token?uid={uid}&password={password}", timeout=10)
            d = r.json()
            token = d.get('access_token', d.get('token', '0'))
            if token != '0': return token, 'IND'
        except: pass
        raise ValueError("Failed to get access token")

    json_data = json.dumps({"open_id": open_id, "open_id_type": "4",
                           "login_token": access_token, "orign_platform_type": "4"})
    proto_msg = json_format.ParseDict(json.loads(json_data), freefire_pb2.LoginReq())
    payload = aes_encrypt(MAIN_KEY, MAIN_IV, proto_msg.SerializeToString())

    headers = {'User-Agent': USER_AGENT, 'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
               'Content-Type': 'application/octet-stream', 'Expect': '100-continue',
               'X-Unity-Version': '2018.4.11f1', 'X-GA': 'v1 1', 'ReleaseVersion': RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://loginbp.ggblueshark.com/MajorLogin", data=payload, headers=headers, timeout=15)
        login_res = freefire_pb2.LoginRes()
        login_res.ParseFromString(resp.content)
        msg = json.loads(json_format.MessageToJson(login_res))
        token = msg.get("token", "0")
        region = msg.get("lockRegion", "0")
        if token == "0": raise ValueError("Failed to get JWT")
        return token, region

def create_like_payload(target_uid, region):
    msg = send_like_pb2.like()
    msg.uid = int(target_uid)
    msg.region = region
    return aes_encrypt(MAIN_KEY, MAIN_IV, msg.SerializeToString())

def get_base_url(region):
    if region == "IND": return "https://client.ind.freefiremobile.com"
    return "https://clientbp.ggblueshark.com"

async def send_ff_request(guest_uid, guest_password, target_uid, region):
    jwt, lock_region = await create_jwt(guest_uid, guest_password)
    payload = create_like_payload(target_uid, region)
    base_url = get_base_url(region)
    headers = {"User-Agent": USER_AGENT, "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
               "Content-Type": "application/octet-stream", "Expect": "100-continue",
               "Authorization": f"Bearer {jwt}", "X-Unity-Version": "2018.4.11f1",
               "X-GA": "v1 1", "ReleaseVersion": RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/LikeProfile", data=payload, headers=headers, timeout=30)
        return resp.status_code, jwt[:30] + "..."

def send_ff_sync(uid, pwd, target, region):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        status, jwt_preview = loop.run_until_complete(send_ff_request(uid, pwd, target, region))
        return {"success": status == 200, "status": status, "jwt": jwt_preview}
    except Exception as e:
        return {"success": False, "message": str(e)[:120]}

# ─── Bot State Engine ─────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.guild_id = ""
        self.region = "IND"
        self.guild_info = {}
        self.running = False
        self.bots = {}
        self.stats = {'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
                      'glory_per_hour': 0, 'active_bots': 0, 'bot_count': 0,
                      'start_time': None, 'running_hours': 0}
        self.lock = threading.Lock()

bot_state = BotState()
sse_queues, sse_lock = [], threading.Lock()

def broadcast():
    with bot_state.lock:
        if bot_state.stats['start_time']:
            e = time.time() - bot_state.stats['start_time']
            bot_state.stats['running_hours'] = round(e / 3600, 2)
            bot_state.stats['glory_per_hour'] = int(bot_state.stats['success'] * 4 / (e / 3600)) if e > 0 else 0
        bot_state.stats['active_bots'] = sum(1 for b in bot_state.bots.values()
                                            if b.get('status') in ('connecting', 'playing', 'success'))
        s = {'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info,
             'running': bot_state.running, 'bots': dict(bot_state.bots),
             'stats': dict(bot_state.stats), 'timestamp': datetime.utcnow().isoformat()}
    with sse_lock:
        for q in sse_queues:
            try: q.put_nowait(s)
            except: pass

threading.Thread(target=lambda: [time.sleep(2) or broadcast() for _ in iter(int,1)], daemon=True).start()

def bot_worker(bot_id, uid, password, guild_id, region):
    key = f"bot_{bot_id}"
    total_sent, total_ok = 0, 0
    while bot_state.running:
        with bot_state.lock:
            bot_state.bots[key] = {'uid': uid, 'status': 'connecting',
                                   'sent': total_sent, 'ok': total_ok, 'last_active': time.time()}
        result = send_ff_sync(uid, password, guild_id, region)
        total_sent += 1
        if result.get('success'):
            total_ok += 1
            with bot_state.lock:
                bot_state.stats['success'] += 1
                bot_state.stats['total_glory'] += 4
                bot_state.stats['matches'] += 1
                bot_state.bots[key] = {'uid': uid, 'status': 'success', 'sent': total_sent,
                                       'ok': total_ok, 'last_active': time.time(), 'jwt': result.get('jwt','')}
        else:
            with bot_state.lock:
                bot_state.stats['failed'] += 1
                bot_state.bots[key] = {'uid': uid, 'status': 'failed', 'sent': total_sent,
                                       'ok': total_ok, 'last_active': time.time(), 'error': result.get('message','')}
        time.sleep(random.randint(60, 180))

# ─── Flask Routes ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/health')
def health():
    return jsonify({'status': 'alive', 'accounts': GuestAccount.query.count(), 'time': datetime.utcnow().isoformat()})

@app.route('/api/status')
def api_status():
    broadcast()
    with bot_state.lock:
        return jsonify({'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info,
                        'running': bot_state.running, 'bots': dict(bot_state.bots),
                        'stats': dict(bot_state.stats)})

@app.route('/api/status/stream')
def status_stream():
    q = queue.Queue()
    with sse_lock: sse_queues.append(q)
    def gen():
        try:
            with bot_state.lock:
                yield f"data: {json.dumps({'guild_id': bot_state.guild_id, 'guild_info': bot_state.guild_info, 'running': bot_state.running, 'bots': dict(bot_state.bots), 'stats': dict(bot_state.stats)})}\n\n"
            while True:
                try: yield f"data: {json.dumps(q.get(timeout=25))}\n\n"
                except queue.Empty: yield ": keepalive\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_queues: sse_queues.remove(q)
    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/guild/info')
def api_guild_info():
    guild_id = request.args.get('guild_id', '').strip()
    region = request.args.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'error': 'Valid guild_id required'})
    info = fetch_guild_info(guild_id, region)
    return jsonify(info)

@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    guild_id = data.get('guild_id', '').strip()
    bot_count = int(data.get('bot_count', 5))
    region = data.get('region', 'IND')
    if not guild_id or not guild_id.isdigit():
        return jsonify({'success': False, 'message': 'Valid numeric Guild ID required'})
    accounts = GuestAccount.query.filter_by(is_working=True).order_by(GuestAccount.last_used).limit(bot_count).all()
    if len(accounts) < bot_count:
        return jsonify({'success': False, 'message': f'Need {bot_count} working accounts, have {len(accounts)}. Generate more!'})
    if bot_state.running:
        bot_state.running = False
        time.sleep(1)
    guild_info = fetch_guild_info(guild_id, region)
    with bot_state.lock:
        bot_state.guild_id = guild_id
        bot_state.guild_info = guild_info
        bot_state.region = region
        bot_state.running = True
        bot_state.bots = {}
        bot_state.stats = {'total_glory': 0, 'matches': 0, 'success': 0, 'failed': 0,
                          'glory_per_hour': 0, 'active_bots': 0, 'bot_count': bot_count,
                          'start_time': time.time(), 'running_hours': 0}
    for i, acc in enumerate(accounts):
        t = threading.Thread(target=bot_worker, args=(i, acc.uid, acc.password, guild_id, region), daemon=True)
        t.start()
        with bot_state.lock:
            bot_state.bots[f"bot_{i}"] = {'uid': acc.uid, 'status': 'starting', 'sent': 0, 'ok': 0, 'last_active': time.time()}
        acc.last_used = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': True, 'message': f'Started {bot_count} bots for guild {guild_id}', 'guild_info': guild_info})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    bot_state.running = False
    with bot_state.lock: bot_state.bots = {}
    return jsonify({'success': True, 'message': 'Bot stopped'})

@app.route('/api/guests/count')
def api_guests_count():
    return jsonify({'total': GuestAccount.query.count(), 'working': GuestAccount.query.filter_by(is_working=True).count()})

@app.route('/api/guests/add', methods=['POST'])
def api_guests_add():
    data = request.get_json()
    uid, password = data.get('uid', '').strip(), data.get('password', '').strip()
    region = data.get('region', 'IND')
    if not uid or not password:
        return jsonify({'success': False, 'message': 'UID and password required'})
    if GuestAccount.query.filter_by(uid=uid).first():
        return jsonify({'success': False, 'message': 'Account already exists'})
    acc = GuestAccount(uid=uid, password=password, region=region)
    db.session.add(acc)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Account added'})

@app.route('/api/guests/generate', methods=['POST'])
def api_guests_generate():
    data = request.get_json()
    count = int(data.get('count', 10))
    region = data.get('region', 'IND')
    generated = []
    for _ in range(count):
        fake_uid = str(random.randint(1000000000, 9999999999))
        fake_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        if not GuestAccount.query.filter_by(uid=fake_uid).first():
            acc = GuestAccount(uid=fake_uid, password=fake_pass, region=region)
            db.session.add(acc)
            generated.append(acc.to_dict())
    db.session.commit()
    return jsonify({'generated': len(generated), 'accounts': generated})

@app.route('/api/guests/delete', methods=['POST'])
def api_guests_delete():
    data = request.get_json()
    acc = GuestAccount.query.filter_by(uid=data.get('uid', '')).first()
    if acc: db.session.delete(acc); db.session.commit()
    return jsonify({'success': True})

@app.route('/api/test/guest', methods=['POST'])
def api_test_guest():
    data = request.get_json()
    uid, password = data.get('uid', ''), data.get('password', '')
    target = data.get('target', '0000000000')
    return jsonify(send_ff_sync(uid, password, target, "IND"))

@app.route('/api/guests/list')
def api_guests_list():
    page = request.args.get('page', 1, type=int)
    accounts = GuestAccount.query.order_by(GuestAccount.added_at.desc()).paginate(page=page, per_page=50)
    return jsonify({'accounts': [a.to_dict() for a in accounts.items], 'total': accounts.total})

# ─── Main ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
