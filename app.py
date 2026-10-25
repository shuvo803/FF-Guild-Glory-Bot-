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

app = Flask(__name__)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


def normalize_db_url(url):
    # Always use the psycopg2 driver (installed via psycopg2-binary),
    # whatever scheme the DATABASE_URL was given with.
    if not url:
        return 'sqlite:///accounts.db'
    url = url.strip()
    for prefix in ('postgres://', 'postgresql://', 'postgresql+psycopg://', 'postgresql+psycopg2://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg2://' + url[len(prefix):]
    return url


app.config['SQLALCHEMY_DATABASE_URI'] = normalize_db_url(os.environ.get('DATABASE_URL'))
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

# The bot always works on the Bangladesh server.
REGION = 'BD'

db = SQLAlchemy(app)

MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/UP1A.231005.007)"
RELEASEVERSION = "OB50"
CLIENT_ID = "100067"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"


class GuestAccount(db.Model):
    __tablename__ = 'guest_accounts'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uid = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    region = db.Column(db.String(10), default=REGION)
    is_working = db.Column(db.Boolean, default=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, nullable=True)
    glory_contributed = db.Column(db.Integer, default=0)
    matches_played = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            'uid': self.uid,
            'region': self.region,
            'is_working': self.is_working,
            'glory_contributed': self.glory_contributed,
            'matches_played': self.matches_played,
        }

with app.app_context():
    db.create_all()


guild_info_cache = {}
GUILD_INFO_CACHE_TTL = 300
# Third-party lookup service. Override with the GUILD_API_URL environment variable on Render if it goes down.
GUILD_API_URL = os.environ.get('GUILD_API_URL', 'https://free-ff-api-src-5plp.onrender.com/api/v1/guildInfo').strip()


def _find_key(obj, names):
    """Search nested JSON for the first matching key."""
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] not in (None, ''):
                return obj[n]
        for v in obj.values():
            r = _find_key(v, names)
            if r not in (None, ''):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, names)
            if r not in (None, ''):
                return r
    return None


def fetch_guild_info(guild_id, region=REGION):
    now = time.time()
    cache_key = guild_id + '_' + region
    if cache_key in guild_info_cache:
        if now - guild_info_cache[cache_key]['timestamp'] < GUILD_INFO_CACHE_TTL:
            return guild_info_cache[cache_key]['data']

    guild_data = {
        'guildFound': False,
        'lookupFailed': False,
        'lookupError': '',
        'guildId': guild_id,
        'guildName': 'Unknown Guild',
        'guildLevel': 0,
        'memberNum': 0,
        'capacity': 35,
        'guildOwner': '',
        'region': region,
    }

    url = GUILD_API_URL + '?region=' + region + '&guildID=' + guild_id
    # The lookup service is a free host that can be asleep, so try twice with a long timeout.
    for attempt in range(2):
        try:
            r = sync_requests.get(url, timeout=25)
            if r.status_code == 200:
                d = r.json()
                name = _find_key(d, ['clanName', 'GuildName', 'guildName', 'name'])
                if name:
                    guild_data['guildFound'] = True
                    guild_data['guildName'] = name
                    guild_data['guildLevel'] = _find_key(d, ['clanLevel', 'GuildLevel', 'guildLevel', 'level']) or 0
                    guild_data['memberNum'] = _find_key(d, ['memberNum', 'GuildMember', 'memberCount', 'members']) or 0
                    guild_data['capacity'] = _find_key(d, ['capacity', 'GuildCapacity']) or 35
                    guild_data['guildOwner'] = _find_key(d, ['captainId', 'GuildOwner', 'ownerId']) or ''
                    guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
                    return guild_data
                guild_data['lookupError'] = 'Service replied but no guild name in it'
                break
            if r.status_code == 404:
                guild_data['lookupError'] = 'Guild not found in region ' + region
                break
            guild_data['lookupFailed'] = True
            guild_data['lookupError'] = 'Lookup service returned HTTP ' + str(r.status_code)
        except Exception as e:
            guild_data['lookupFailed'] = True
            guild_data['lookupError'] = (type(e).__name__ + ': ' + str(e))[:120]
        time.sleep(1)

    # Only cache a definite "not found". Temporary failures are never cached.
    if not guild_data['lookupFailed']:
        guild_info_cache[cache_key] = {'data': guild_data, 'timestamp': now}
    return guild_data


def aes_encrypt(key, iv, plaintext):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))


async def get_access_token(uid, password):
    url = 'https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant'
    payload = {
        'client_id': int(CLIENT_ID),
        'client_secret': CLIENT_SECRET,
        'client_type': 2,
        'password': password,
        'response_type': 'token',
        'uid': int(uid),
    }
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
        'Content-Type': 'application/json; charset=utf-8',
        'Connection': 'Keep-Alive',
        'Accept-Encoding': 'gzip',
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers, timeout=15)
        try:
            data = resp.json()
        except Exception:
            raise ValueError('Step 1 (guest login): HTTP ' + str(resp.status_code) + ', reply is not JSON')
        inner = data.get('data', data) if isinstance(data, dict) else {}
        token = inner.get('access_token', '0')
        if token == '0':
            raise ValueError('Step 1 (guest login) rejected, HTTP ' + str(resp.status_code) + ': ' + str(data)[:90])
        return token, inner.get('open_id', '0')


async def create_jwt(uid, password):
    access_token, open_id = await get_access_token(uid, password)
    if access_token == '0':
        raise ValueError('Failed to get access token')

    login_req = {
        'open_id': open_id,
        'open_id_type': '4',
        'login_token': access_token,
        'orign_platform_type': '4',
    }
    proto_msg = json_format.ParseDict(login_req, freefire_pb2.LoginReq())
    proto_bytes = proto_msg.SerializeToString()
    payload = aes_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)

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
        resp = await client.post(
            'https://loginbp.ggblueshark.com/MajorLogin',
            content=payload,
            headers=headers,
            timeout=15,
        )
        login_res = freefire_pb2.LoginRes()
        try:
            login_res.ParseFromString(resp.content)
        except Exception:
            raise ValueError('Step 2 (MajorLogin): HTTP ' + str(resp.status_code) + ', reply is not valid protobuf')
        msg = json.loads(json_format.MessageToJson(login_res))
        token = msg.get('token', '0')
        lock_region = msg.get('lockRegion', '0')
        if token == '0':
            raise ValueError('Step 2 (MajorLogin): HTTP ' + str(resp.status_code) + ', no token in reply')
        return token, lock_region


async def send_ff_request(guest_uid, guest_password, target_uid, region):
    jwt, lock_region = await create_jwt(guest_uid, guest_password)

    msg = send_like_pb2.like()
    msg.uid = int(target_uid)
    msg.region = region
    proto_bytes = msg.SerializeToString()
    payload = aes_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)

    if region == 'IND':
        base_url = 'https://client.ind.freefiremobile.com'
    else:
        base_url = 'https://clientbp.ggblueshark.com'

    headers = {
        'User-Agent': USER_AGENT,
        'Connection': 'Keep-Alive',
        'Accept-Encoding': 'gzip',
        'Content-Type': 'application/octet-stream',
        'Expect': '100-continue',
        'Authorization': 'Bearer ' + jwt,
        'X-Unity-Version': '2018.4.11f1',
        'X-GA': 'v1 1',
        'ReleaseVersion': RELEASEVERSION,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            base_url + '/LikeProfile',
            content=payload,
            headers=headers,
            timeout=30,
        )
        return resp.status_code


def send_ff_sync(uid, pwd, target, region):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        status = loop.run_until_complete(send_ff_request(uid, pwd, target, region))
        if status == 200:
            return {'success': True, 'status': status, 'jwt': 'sent'}
        return {'success': False, 'status': status, 'message': 'Step 3 (LikeProfile): HTTP ' + str(status)}
    except Exception as e:
        return {'success': False, 'message': (type(e).__name__ + ': ' + str(e))[:160]}


class BotState:
    def __init__(self):
        self.guild_id = ''
        self.region = REGION
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
        start = bot_state.stats['start_time']
        if start:
            elapsed = time.time() - start
            bot_state.stats['running_hours'] = round(elapsed / 3600, 2)
            if elapsed > 0:
                rate = int(bot_state.stats['success'] * 4 / (elapsed / 3600))
                bot_state.stats['glory_per_hour'] = rate
        active = 0
        for b in bot_state.bots.values():
            s = b.get('status', '')
            if s in ('connecting', 'playing', 'success'):
                active += 1
        bot_state.stats['active_bots'] = active
        payload = {
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
                q.put_nowait(payload)
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
    key = 'bot_' + str(bot_id)
    total_sent = 0
    total_ok = 0
    last_error = ''
    while bot_state.running:
        with bot_state.lock:
            bot_state.bots[key] = {
                'uid': uid,
                'status': 'connecting',
                'sent': total_sent,
                'ok': total_ok,
                'last_active': time.time(),
                'error': last_error,
            }
        result = send_ff_sync(uid, password, guild_id, region)
        total_sent += 1
        if result.get('success'):
            total_ok += 1
            last_error = ''
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
                }
        else:
            last_error = result.get('message') or ('HTTP ' + str(result.get('status', '?')))
            with bot_state.lock:
                bot_state.stats['failed'] += 1
                bot_state.bots[key] = {
                    'uid': uid,
                    'status': 'failed',
                    'sent': total_sent,
                    'ok': total_ok,
                    'last_active': time.time(),
                    'error': last_error,
                }
        for _ in range(random.randint(60, 180)):
            if not bot_state.running:
                break
            time.sleep(1)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/health')
def health():
    return jsonify({'status': 'alive'})


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

    def gen():
        try:
            init = json.dumps({
                'guild_id': bot_state.guild_id,
                'guild_info': bot_state.guild_info,
                'running': bot_state.running,
                'bots': dict(bot_state.bots),
                'stats': dict(bot_state.stats),
            })
            yield 'data: ' + init + '\n\n'
            while True:
                try:
                    data = q.get(timeout=25)
                    yield 'data: ' + json.dumps(data) + '\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        except GeneratorExit:
            with sse_lock:
                if q in sse_queues:
                    sse_queues.remove(q)

    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/guild/info')
def api_guild_info():
    guild_id = request.args.get('guild_id', '').strip()
    region = REGION
    if not guild_id or not guild_id.isdigit():
        return jsonify({'error': 'Valid guild_id required'})
    info = fetch_guild_info(guild_id, region)
    return jsonify(info)


@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json()
    guild_id = data.get('guild_id', '').strip()
    bot_count = int(data.get('bot_count', 5))
    region = REGION
    if not guild_id or not guild_id.isdigit():
        return jsonify({'success': False, 'message': 'Valid numeric Guild ID required'})
    accounts = GuestAccount.query.filter_by(is_working=True).order_by(GuestAccount.last_used).limit(bot_count).all()
    if len(accounts) < bot_count:
        return jsonify({
            'success': False,
            'message': 'Need ' + str(bot_count) + ' working accounts, have ' + str(len(accounts)) + '. Generate more!',
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
            'start_time': time.time(), 'running_hours': 0,
        }
    for i, acc in enumerate(accounts):
        with bot_state.lock:
            bot_state.bots['bot_' + str(i)] = {
                'uid': acc.uid, 'status': 'starting',
                'sent': 0, 'ok': 0, 'last_active': time.time(),
            }
        threading.Thread(
            target=bot_worker,
            args=(i, acc.uid, acc.password, guild_id, region),
            daemon=True
        ).start()
        acc.last_used = datetime.utcnow()
        db.session.commit()
    return jsonify({
        'success': True,
        'message': 'Started ' + str(bot_count) + ' bots for guild ' + guild_id,
        'guild_info': guild_info,
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
        'working': GuestAccount.query.filter_by(is_working=True).count(),
    })


@app.route('/api/guests/add', methods=['POST'])
def api_guests_add():
    data = request.get_json()
    uid = data.get('uid', '').strip()
    password = data.get('password', '').strip()
    region = REGION
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
    region = REGION
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
        'page': page,
    })


@app.route('/api/test/guest', methods=['POST'])
def api_test_guest():
    data = request.get_json()
    result = send_ff_sync(
        data.get('uid', ''),
        data.get('password', ''),
        data.get('target', '0000000000'),
        REGION,
    )
    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
