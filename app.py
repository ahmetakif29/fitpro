from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import hashlib, os, json, hmac, base64, random
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime
import requests
import urllib.request

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_CRED_JSON = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if FIREBASE_CRED_JSON and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_CRED_JSON))
        firebase_admin.initialize_app(cred)
except Exception as _fb_err:
    firebase_admin = None
    print('Firebase Admin baslatilamadi:', _fb_err)


def send_push_notification(fcm_token, title, body):
    """Tek bir kullaniciya bildirim gonderir. Basarili ise True doner."""
    if not fcm_token or firebase_admin is None or not firebase_admin._apps:
        return False
    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=fcm_token,
        )
        messaging.send(message)
        return True
    except Exception as e:
        print('Push gonderim hatasi:', e)
        return False


def notify_friends_of_activity(user_id, activity_type):
    """Kullanicinin kabul edilmis arkadaslarina, kullanicinin yeni aktivite
    kaydettigini bildirir (ör. antrenman). Bildirim izni olmayanlar atlanir."""
    try:
        conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT username FROM users WHERE id = %s', (user_id,))
        me_row = cur.fetchone()
        if not me_row:
            cur.close(); conn.close()
            return
        username = me_row['username']

        cur.execute('''
            SELECT CASE WHEN requester_id = %s THEN addressee_id ELSE requester_id END AS friend_id
            FROM friendships
            WHERE status = 'accepted' AND (requester_id = %s OR addressee_id = %s)
        ''', (user_id, user_id, user_id))
        friend_ids = [r['friend_id'] for r in cur.fetchall()]

        if not friend_ids:
            cur.close(); conn.close()
            return

        titles = {'workout': ('Arkadaşın Antrenmanda! 💪', f'{username} bugün bir antrenman kaydetti')}
        title, body = titles.get(activity_type, ('Arkadaş Aktivitesi', f'{username} yeni bir kayıt ekledi'))

        for fid in friend_ids:
            cur.execute('SELECT fcm_token FROM users WHERE id = %s', (fid,))
            row = cur.fetchone()
            if row and row.get('fcm_token'):
                send_push_notification(row['fcm_token'], title, body)

        cur.close(); conn.close()
    except Exception as e:
        print('notify_friends_of_activity hatasi:', e)

# ====================== SHOPIER AYARLARI ======================
SHOPIER_API_KEY        = os.environ.get('SHOPIER_API_KEY', '')
SHOPIER_API_SECRET     = os.environ.get('SHOPIER_API_SECRET', '')
SHOPIER_PAT            = os.environ.get('SHOPIER_PAT', '')
SHOPIER_WEBHOOK_TOKEN  = os.environ.get('SHOPIER_WEBHOOK_TOKEN', '')
SHOPIER_OSB_USERNAME   = os.environ.get('SHOPIER_OSB_USERNAME', '')
SHOPIER_OSB_PASSWORD   = os.environ.get('SHOPIER_OSB_PASSWORD', '')
SHOPIER_PRODUCT_URL    = os.environ.get(
    'SHOPIER_PRODUCT_URL',
    'https://www.shopier.com/fit_pro/47386919'
)
SHOPIER_PAYMENT_URL    = 'https://www.shopier.com/ShowProduct/api_pay4.php'
SHOPIER_CALLBACK_URL   = os.environ.get(
    'SHOPIER_CALLBACK_URL',
    'https://fitpro-mytg.onrender.com/api/payment/callback'
)
SHOPIER_API_BASE = 'https://api.shopier.com/v1'

app = Flask(__name__)
app.config['SECRET_KEY'] = 'fitpro-secret-key-2024'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# ====================== VERİTABANI ======================
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres.ubpygbiljtaupnqwefja:TRyasuo2908@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres'
)

def get_db():
    """Her istek için yeni bir psycopg2 bağlantısı döner."""
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


@app.before_request
def _touch_last_active():
    """Giriş yapmış kullanıcının son aktif olduğu zamanı günceller.
    Her isteğe yazmamak için en fazla 60 saniyede bir DB'ye dokunur."""
    if 'user_id' not in session:
        return
    now = datetime.utcnow()
    last_ping = session.get('_last_active_ping')
    if last_ping:
        try:
            if (now - datetime.fromisoformat(last_ping)).total_seconds() < 60:
                return
        except Exception:
            pass
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('UPDATE users SET last_active = NOW() WHERE id = %s', (session['user_id'],))
        conn.commit()
        cur.close(); conn.close()
        session['_last_active_ping'] = now.isoformat()
    except Exception as e:
        print('last_active güncellenemedi:', e)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # --- Tablolar ---
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id               SERIAL PRIMARY KEY,
            username         TEXT UNIQUE NOT NULL,
            email            TEXT UNIQUE NOT NULL,
            password         TEXT NOT NULL,
            is_premium       INTEGER DEFAULT 0,
            height           REAL,
            goal_weight      REAL,
            age              INTEGER,
            gender           TEXT,
            activity_level   TEXT DEFAULT 'moderate',
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id        SERIAL PRIMARY KEY,
            user_id   INTEGER NOT NULL,
            weight    REAL NOT NULL,
            date      TEXT NOT NULL,
            notes     TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS workout_logs (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL,
            name            TEXT NOT NULL,
            duration        INTEGER,
            calories_burned INTEGER,
            workout_type    TEXT,
            date            TEXT NOT NULL,
            notes           TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            amount     REAL NOT NULL,
            plan       TEXT,
            status     TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS pending_checkouts (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL,
            email            TEXT NOT NULL,
            plan             TEXT NOT NULL,
            status           TEXT DEFAULT 'pending',
            shopier_order_id TEXT,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS processed_shopier_orders (
            shopier_order_id TEXT PRIMARY KEY,
            user_id          INTEGER NOT NULL,
            processed_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS nutrition_logs (
            id       SERIAL PRIMARY KEY,
            user_id  INTEGER NOT NULL,
            name     TEXT NOT NULL,
            meal     TEXT,
            calories INTEGER DEFAULT 0,
            protein  REAL    DEFAULT 0,
            carbs    REAL    DEFAULT 0,
            fat      REAL    DEFAULT 0,
            date     TEXT NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS measurements (
            id      SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            date    TEXT NOT NULL,
            waist   REAL,
            chest   REAL,
            hip     REAL,
            arm     REAL,
            thigh   REAL,
            neck    REAL
        )
    ''')

    # --- Migration: eksik kolonları ekle ---
    for sql in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS activity_level TEXT DEFAULT 'moderate'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE measurements ADD COLUMN IF NOT EXISTS neck REAL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS goal_type TEXT DEFAULT 'lose'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS goal_intensity TEXT DEFAULT 'moderate'",
    ]:
        try:
            cur.execute(sql)
        except Exception:
            conn.rollback()

    conn.commit()

    # --- Demo kullanıcı ---
    cur.execute('SELECT id FROM users WHERE email = %s', ('demo@fitpro.com',))
    if not cur.fetchone():
        cur.execute(
            'INSERT INTO users (username,email,password,height,goal_weight,age,gender) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            ('demo_user', 'demo@fitpro.com', hash_pw('demo123'), 175, 75, 28, 'male')
        )
        uid = cur.fetchone()[0]
        today = date.today()
        for i in range(14):
            cur.execute(
                'INSERT INTO weight_logs (user_id,weight,date) VALUES (%s,%s,%s)',
                (uid, round(82 - i * 0.3, 1), (today - timedelta(days=13 - i)).isoformat())
            )
        for i, (nm, wt, dur, cal) in enumerate([
            ('Göğüs Günü', 'strength', 60, 350),
            ('Kardio',     'cardio',   45, 420),
            ('Sırt Günü',  'strength', 55, 310),
            ('Bacak Günü', 'strength', 65, 400),
            ('HIIT',       'cardio',   30, 380),
        ]):
            cur.execute(
                'INSERT INTO workout_logs (user_id,name,workout_type,duration,calories_burned,date) '
                'VALUES (%s,%s,%s,%s,%s,%s)',
                (uid, nm, wt, dur, cal, (today - timedelta(days=i * 2)).isoformat())
            )
        conn.commit()

    cur.close()
    conn.close()


# ====================== AUTH ======================

@app.route('/')
def index():
    if 'user_id' in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE id = %s', (session['user_id'],))
        u = cur.fetchone()
        cur.close(); conn.close()
        if u:
            return redirect('/dashboard')
        session.clear()
    return render_template('index.html')


@app.route('/app')
def mobile_app():
    if 'user_id' in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE id = %s', (session['user_id'],))
        u = cur.fetchone()
        cur.close(); conn.close()
        if u:
            return redirect('/dashboard')
        session.clear()
    return render_template('app_mobile.html')


@app.route('/app')
def mobile_entry():
    if 'user_id' in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE id = %s', (session['user_id'],))
        u = cur.fetchone()
        cur.close(); conn.close()
        if u:
            return redirect('/dashboard')
        session.clear()
    return render_template('app_mobile.html')


FRIEND_CODE_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # 0,O,1,I gibi karışabilecek karakterler çıkarıldı


def generate_unique_friend_code(cur):
    while True:
        code = ''.join(random.choices(FRIEND_CODE_CHARS, k=5))
        cur.execute('SELECT 1 FROM users WHERE friend_code = %s', (code,))
        if not cur.fetchone():
            return code


@app.route('/api/register', methods=['POST'])
def register():
    d = request.json
    conn = get_db(); cur = conn.cursor()
    try:
        friend_code = generate_unique_friend_code(cur)
        cur.execute(
            'INSERT INTO users (username,email,password,height,goal_weight,age,gender,friend_code,goal_type,goal_intensity) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (d['username'], d['email'], hash_pw(d['password']),
             d.get('height'), d.get('goal_weight'), d.get('age'), d.get('gender'), friend_code,
             d.get('goal_type', 'lose'), d.get('goal_intensity', 'moderate'))
        )
        uid = cur.fetchone()[0]
        conn.commit()
        session.permanent = True
        session['user_id'] = uid
        return jsonify({'success': True, 'user': {'id': uid, 'username': d['username'], 'is_premium': False}})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({'error': 'Email veya kullanıcı adı zaten kayıtlı'}), 400
    finally:
        cur.close(); conn.close()


@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE email = %s', (d['email'],))
    u = cur.fetchone()
    cur.close(); conn.close()
    if not u or u['password'] != hash_pw(d['password']):
        return jsonify({'error': 'Email veya şifre hatalı'}), 401
    session.permanent = True
    session['user_id'] = u['id']
    return jsonify({'success': True, 'user': {'id': u['id'], 'username': u['username'], 'is_premium': bool(u['is_premium'])}})


# ── Bildirim mesaj kütüphanesi ──────────────────────────────────────────
# {kg}, {days}, {n}, {lost} gibi yer tutucular ilgili duruma göre doldurulur.

MSG_CLOSE_TO_GOAL = [
    ('Çok yaklaştın! 🎯', 'Hedefine sadece {kg} kg kaldı, şimdi pes etme!'),
    ('Neredeyse orada! 🔥', '{kg} kg sonra hedefindesin, son viraj!'),
    ('Bitmek üzere 💪', 'Hedefe {kg} kg kaldı, bu son kısım en önemlisi.'),
    ('Son düzlük 🏁', 'Sadece {kg} kg kaldı, bırakma şimdi!'),
    ('Az kaldı, devam! ⚡', '{kg} kg sonra hedefine ulaşacaksın, hızını kesme.'),
    ('Hedef gözüküyor 👀', '{kg} kg kaldı, artık çok yakınsın.'),
    ('Finalist gibisin 🏆', 'Hedefine {kg} kg kaldı, bu son adımları at.'),
]

MSG_PROGRESS = [
    ('Harika gidiyorsun 📉', 'Başladığından beri {lost} kg verdin, bu gerçek bir başarı!'),
    ('Etkileyici ilerleme 🚀', '{lost} kg vererek harika bir yol kat ettin, devam!'),
    ('Emeğin boşa gitmiyor 💚', 'Şu ana kadar {lost} kg kaybettin, gurur duy kendinle.'),
    ('Fark yaratıyorsun ✨', '{lost} kg verdin, vücudun bunu hissediyor.'),
    ('Küçümsenmeyecek bir başarı 🔥', 'Toplamda {lost} kg verdin, bu emeğin karşılığı.'),
    ('Gurur verici 💪', '{lost} kg kaybettin, bu emek boşa gitmiyor.'),
    ('İlerleme gerçek 📊', 'Başlangıçtan bu yana {lost} kg indin, tebrikler.'),
]

MSG_STREAK_GOOD = [
    ('Seri devam ediyor 🔥', 'Bu hafta {n} antrenman yaptın, bu tempoyu koru!'),
    ('Bu hafta harikaydın 💪', '{n} antrenmanla haftayı domine ettin, aynen devam!'),
    ('İyi gidiyorsun 👏', 'Bu hafta {n} kez antrenmana gittin, seriyi bozma!'),
    ('Tempo tutturdun ⚡', '{n} antrenmanla bu hafta gerçekten iyiydin.'),
    ('Disiplin bu işte 🎯', 'Bu hafta {n} kere antrenman yaptın, harika bir alışkanlık.'),
    ('Form yakaladın 🔥', '{n} antrenmanlık bir hafta geçirdin, devam et.'),
    ('Süpersin 🌟', 'Bu hafta {n} antrenmanla kendini geçtin.'),
]

MSG_LONG_INACTIVE = [
    ('Seni özledik 👋', '{days} gündür seni görmedik, geri dönmeye ne dersin?'),
    ('Neredesin? 🤔', '{days} gündür uygulamaya girmedin, hedefin seni bekliyor.'),
    ('Formunu kaybetme ⚠️', '{days} gündür ara verdin, küçük bir adımla geri dön.'),
    ('Geri dönüş zamanı 🔄', '{days} gündür yoksun, bugün tekrar başlayabilirsin.'),
    ('Seni bekliyoruz 💙', '{days} gün oldu, hadi kaldığın yerden devam et.'),
    ('Ara uzadı 📅', '{days} gündür aktif değilsin, küçük bir adım yeter.'),
    ('Hâlâ oradayız 🙌', '{days} gündür seni görmüyoruz, geri gelmen bizi mutlu eder.'),
]

MSG_STREAK_BROKEN = [
    ('Serin bozuluyor! 🔥💔', '{days} gündür hiç veri girmedin, serini kaybetmeden geri dön!'),
    ('Ateş sönüyor 🥺', '{days} gündür sessizsin, kısa bir kayıt bile seriyi kurtarır.'),
    ('Serine dikkat! ⚠️🔥', '{days} gündür kilo, antrenman veya beslenme girmedin.'),
    ('Az kaldı, kaybetme! 🔥', '{days} gündür veri yok, serini bugün kurtarabilirsin.'),
    ('Streak tehlikede 😬', '{days} gündür uygulamayı kullanmadın, serin sıfırlanmak üzere.'),
]

MSG_NO_WORKOUT = [
    ('Hadi devam et 💪', 'Birkaç gündür antrenman kaydın yok, bugün küçük bir seans bile fark yaratır.'),
    ('Bugün sıra sende 🔥', 'Antrenman zamanı geldi, hadi başlayalım.'),
    ('5 dakika bile yeter 🏃', 'Uzun süredir antrenman yok, kısa bir seansla başla.'),
    ('Vücudun seni bekliyor', 'Antrenmana ara verdin gibi, bugün devam edelim mi?'),
    ('Harekete geç ⚡', 'Birkaç gündür antrenman yok, bugün bunu değiştir.'),
    ('Küçük bir başlangıç yeter 🌱', 'Uzun bir antrenman şart değil, bugün 10 dakika bile iş görür.'),
    ('Bahane yok 😤', 'Antrenman kaydın birkaç gündür yok, hadi bugün yap.'),
]

MSG_NO_WEIGHT = [
    ('Kilonu unutma ⚖️', 'Birkaç gündür kilo girmemişsin, birkaç saniyende güncelle.'),
    ('Takipte kal 📊', 'Kilonu güncel tutmak ilerlemeni net görmeni sağlar.'),
    ('Hızlı bir güncelleme 📝', 'Bugünkü kilonu ekleyip grafiğini güncel tut.'),
    ('Veri güçtür 💡', 'Kilonu girmen, ilerlemeni doğru görmen için önemli.'),
    ('Bir dakikanı ayır ⏱️', 'Kilo girişini unutma, grafiğin seni bekliyor.'),
    ('Küçük bir adım 📈', 'Bugünkü kilonu eklemeyi unutma.'),
    ('Sayılar konuşsun 🔢', 'Kilonu güncelleyerek gerçek ilerlemeni gör.'),
]

MSG_GENERIC_MOTIVATION = [
    ('Bugün senin günün ☀️', 'Küçük bir adım, büyük bir farkın başlangıcı olabilir.'),
    ('Disiplin motivasyondan güçlüdür', 'Canın istemese de bugün devam et, ileride teşekkür edeceksin.'),
    ('Kendine yatırım yap 💚', 'Vücudun senin en değerli yatırımın, bugün ona zaman ayır.'),
    ('Pes etme 🎯', 'Hedefin hâlâ orada, sen de oraya gidebilirsin.'),
    ('Bir adım daha 👣', 'Her gün biraz daha yakınsın, bugün de devam et.'),
    ('Kendini geçmeye devam 🏃', 'Dünkü halinden daha iyisin, bugün bunu kanıtla.'),
    ('Şimdi ya da hiç 🔥', 'Ertelemek yerine bugün küçük bir adım at.'),
]


def pick_message(pool, **kwargs):
    title, body = random.choice(pool)
    return title, body.format(**kwargs)


@app.route('/api/cron/daily-reminders', methods=['POST'])
def cron_daily_reminders():
    cron_secret = os.environ.get('CRON_SECRET', '')
    incoming = request.headers.get('X-Cron-Secret', '')
    if not cron_secret or incoming != cron_secret:
        return jsonify({'error': 'yetkisiz'}), 403

    monday_str = (date.today() - timedelta(days=date.today().weekday())).isoformat()

    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT u.id, u.fcm_token, u.goal_weight,
          (SELECT MAX(date) FROM workout_logs WHERE user_id = u.id) AS last_workout,
          (SELECT MAX(date) FROM weight_logs WHERE user_id = u.id) AS last_weight,
          (SELECT MAX(date) FROM nutrition_logs WHERE user_id = u.id) AS last_nutrition,
          (SELECT TO_CHAR(MAX(updated_at), 'YYYY-MM-DD') FROM personal_records WHERE user_id = u.id) AS last_pr,
          (SELECT weight FROM weight_logs WHERE user_id = u.id ORDER BY date DESC LIMIT 1) AS current_weight,
          (SELECT weight FROM weight_logs WHERE user_id = u.id ORDER BY date ASC LIMIT 1) AS start_weight,
          (SELECT COUNT(*) FROM workout_logs WHERE user_id = u.id AND date >= %s) AS weekly_workouts
        FROM users u
        WHERE u.fcm_token IS NOT NULL AND u.fcm_token <> ''
    ''', (monday_str,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    today = date.today()
    sent = 0

    for r in rows:
        last_workout = None
        if r['last_workout']:
            try:
                last_workout = date.fromisoformat(str(r['last_workout'])[:10])
            except Exception:
                pass

        last_weight = None
        if r['last_weight']:
            try:
                last_weight = date.fromisoformat(str(r['last_weight'])[:10])
            except Exception:
                pass

        days_since_workout = (today - last_workout).days if last_workout else None
        days_since_weight = (today - last_weight).days if last_weight else None

        # Herhangi bir veri türü (kilo, antrenman, beslenme, PR) üzerinden birleşik son aktiflik
        activity_dates = []
        for key in ('last_workout', 'last_weight', 'last_nutrition', 'last_pr'):
            val = r.get(key)
            if val:
                try:
                    activity_dates.append(date.fromisoformat(str(val)[:10]))
                except Exception:
                    pass
        last_any_activity = max(activity_dates) if activity_dates else None
        days_since_any = (today - last_any_activity).days if last_any_activity else None

        remaining_kg = None
        if r['current_weight'] is not None and r['goal_weight']:
            remaining_kg = round(abs(r['current_weight'] - r['goal_weight']), 1)

        lost_kg = None
        if r['start_weight'] is not None and r['current_weight'] is not None:
            diff = round(r['start_weight'] - r['current_weight'], 1)
            if diff > 0:
                lost_kg = diff

        weekly_workouts = r['weekly_workouts'] or 0

        title = body = None

        # Öncelik sırası: seri tehlikede (2+ gün hiç veri yok) > uzun süredir yok >
        # hedefe çok yakın > antrenman eksik > kilo eksik > iyi seri (övgü) >
        # ilerleme övgüsü > genel motivasyon
        if days_since_any is not None and days_since_any >= 2:
            title, body = pick_message(MSG_STREAK_BROKEN, days=days_since_any)
        elif days_since_workout is not None and days_since_workout >= 7:
            title, body = pick_message(MSG_LONG_INACTIVE, days=days_since_workout)
        elif remaining_kg is not None and 0 < remaining_kg <= 2:
            title, body = pick_message(MSG_CLOSE_TO_GOAL, kg=remaining_kg)
        elif days_since_workout is None or days_since_workout >= 3:
            title, body = pick_message(MSG_NO_WORKOUT)
        elif days_since_weight is None or days_since_weight >= 2:
            title, body = pick_message(MSG_NO_WEIGHT)
        elif weekly_workouts >= 3:
            title, body = pick_message(MSG_STREAK_GOOD, n=weekly_workouts)
        elif lost_kg is not None and lost_kg >= 2:
            title, body = pick_message(MSG_PROGRESS, lost=lost_kg)
        else:
            title, body = pick_message(MSG_GENERIC_MOTIVATION)

        if title and r['fcm_token']:
            if send_push_notification(r['fcm_token'], title, body):
                sent += 1

    return jsonify({'success': True, 'checked': len(rows), 'sent': sent})


@app.route('/api/push/register', methods=['POST'])
def push_register():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    d = request.json or {}
    token = d.get('token')
    if not token:
        return jsonify({'error': 'Token eksik'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE users SET fcm_token = %s WHERE id = %s', (token, session['user_id']))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'success': True})


@app.route('/api/push/test', methods=['POST'])
def push_test():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT fcm_token FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    cur.close(); conn.close()
    if not u or not u['fcm_token']:
        return jsonify({'error': 'Kayıtlı bildirim token bulunamadı, önce app üzerinden giriş yapıp izin verdiğinden emin ol'}), 400
    ok = send_push_notification(u['fcm_token'], 'FitPro', 'Bu bir test bildirimi 🎉')
    if not ok:
        return jsonify({'error': 'Bildirim gönderilemedi, Firebase ayarlarını kontrol et'}), 500
    return jsonify({'success': True})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/me')
def me():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        session.clear()
        return jsonify({'error': 'Unauthorized'}), 401

    friend_code = u.get('friend_code')
    if not friend_code:
        plain_cur = conn.cursor()
        friend_code = generate_unique_friend_code(plain_cur)
        plain_cur.execute('UPDATE users SET friend_code = %s WHERE id = %s', (friend_code, u['id']))
        conn.commit()
        plain_cur.close()

    cur.close(); conn.close()
    return jsonify({
        'id': u['id'], 'username': u['username'], 'email': u['email'],
        'is_premium': bool(u['is_premium']), 'height': u['height'],
        'goal_weight': u['goal_weight'], 'age': u['age'], 'gender': u['gender'],
        'activity_level': u.get('activity_level', 'moderate'),
        'goal_type': u.get('goal_type', 'lose'),
        'goal_intensity': u.get('goal_intensity', 'moderate'),
        'created_at': u.get('created_at', ''),
        'friend_code': friend_code,
        'avatar_data': u.get('avatar_data'),
    })


@app.route('/api/profile/avatar', methods=['POST'])
def upload_avatar():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    d = request.json or {}
    image = d.get('image', '')
    if not image or not image.startswith('data:image/'):
        return jsonify({'error': 'Geçersiz görsel verisi'}), 400
    if len(image) > 700_000:  # ~500KB civarı, DB'yi şişirmesin
        return jsonify({'error': 'Görsel çok büyük, daha küçük bir fotoğraf dene'}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE users SET avatar_data = %s WHERE id = %s', (image, session['user_id']))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'success': True})


@app.route('/api/friends/request', methods=['POST'])
def friends_request():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    code = (request.json or {}).get('code', '').strip().upper()
    if not code:
        return jsonify({'error': 'Kod eksik'}), 400

    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT id, username FROM users WHERE friend_code = %s', (code,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        return jsonify({'error': 'Bu kodla eşleşen bir kullanıcı bulunamadı'}), 404
    if target['id'] == session['user_id']:
        cur.close(); conn.close()
        return jsonify({'error': 'Kendini ekleyemezsin'}), 400

    cur.execute('''
        SELECT id, status FROM friendships
        WHERE (requester_id = %s AND addressee_id = %s)
           OR (requester_id = %s AND addressee_id = %s)
    ''', (session['user_id'], target['id'], target['id'], session['user_id']))
    existing = cur.fetchone()
    if existing:
        cur.close(); conn.close()
        if existing['status'] == 'accepted':
            return jsonify({'error': 'Zaten arkadaşsınız'}), 400
        return jsonify({'error': 'Zaten bekleyen bir istek var'}), 400

    plain_cur = conn.cursor()
    plain_cur.execute(
        'INSERT INTO friendships (requester_id, addressee_id, status) VALUES (%s, %s, %s)',
        (session['user_id'], target['id'], 'pending')
    )
    conn.commit()
    plain_cur.close()

    # Karşı tarafa anında bildirim gönder (cron beklemeden)
    cur.execute('SELECT username, fcm_token FROM users WHERE id = %s', (session['user_id'],))
    me_row = cur.fetchone()
    cur.close(); conn.close()

    conn2 = get_db(); cur2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2.execute('SELECT fcm_token FROM users WHERE id = %s', (target['id'],))
    target_row = cur2.fetchone()
    cur2.close(); conn2.close()

    if target_row and target_row.get('fcm_token'):
        send_push_notification(
            target_row['fcm_token'],
            'Yeni Arkadaşlık İsteği 🤝',
            f"{me_row['username']} sana arkadaşlık isteği gönderdi"
        )

    return jsonify({'success': True, 'username': target['username']})


@app.route('/api/friends/requests')
def friends_requests():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT f.id AS request_id, u.id AS user_id, u.username, u.avatar_data
        FROM friendships f
        JOIN users u ON u.id = f.requester_id
        WHERE f.addressee_id = %s AND f.status = 'pending'
        ORDER BY f.created_at DESC
    ''', (session['user_id'],))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/friends/respond', methods=['POST'])
def friends_respond():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    d = request.json or {}
    request_id = d.get('request_id')
    accept = bool(d.get('accept'))

    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM friendships WHERE id = %s AND addressee_id = %s', (request_id, session['user_id']))
    fr = cur.fetchone()
    if not fr:
        cur.close(); conn.close()
        return jsonify({'error': 'İstek bulunamadı'}), 404

    plain_cur = conn.cursor()
    if accept:
        plain_cur.execute("UPDATE friendships SET status = 'accepted' WHERE id = %s", (request_id,))
    else:
        plain_cur.execute('DELETE FROM friendships WHERE id = %s', (request_id,))
    conn.commit()
    plain_cur.close(); cur.close(); conn.close()
    return jsonify({'success': True})


@app.route('/api/friends')
def friends_list():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    uid = session['user_id']
    monday_str = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT CASE WHEN requester_id = %s THEN addressee_id ELSE requester_id END AS friend_id
        FROM friendships
        WHERE status = 'accepted' AND (requester_id = %s OR addressee_id = %s)
    ''', (uid, uid, uid))
    friend_ids = [r['friend_id'] for r in cur.fetchall()]

    result = []
    for fid in friend_ids:
        cur.execute('SELECT id, username, goal_weight, height, last_active, avatar_data FROM users WHERE id = %s', (fid,))
        fu = cur.fetchone()
        if not fu:
            continue
        cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date DESC LIMIT 1', (fid,))
        cw = cur.fetchone()
        cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date ASC LIMIT 1', (fid,))
        sw = cur.fetchone()
        cur.execute('SELECT MAX(date) AS d FROM workout_logs WHERE user_id = %s', (fid,))
        lw = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS c FROM workout_logs WHERE user_id = %s AND date >= %s",
            (fid, monday_str)
        )
        wk = cur.fetchone()

        current_weight = cw['weight'] if cw else None
        start_weight = sw['weight'] if sw else None
        lost = round(start_weight - current_weight, 1) if (start_weight is not None and current_weight is not None) else None

        result.append({
            'id': fu['id'],
            'username': fu['username'],
            'current_weight': current_weight,
            'goal_weight': fu['goal_weight'],
            'lost_kg': lost,
            'last_workout': lw['d'] if lw else None,
            'weekly_workouts': wk['c'] if wk else 0,
            'last_active': fu['last_active'].isoformat() if fu['last_active'] else None,
            'avatar_data': fu.get('avatar_data'),
        })

    cur.close(); conn.close()
    return jsonify(result)


@app.route('/api/friends/<int:friend_id>/profile')
def friends_profile(friend_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    uid = session['user_id']
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute('''
        SELECT 1 FROM friendships
        WHERE status = 'accepted' AND (
            (requester_id = %s AND addressee_id = %s) OR
            (requester_id = %s AND addressee_id = %s)
        )
    ''', (uid, friend_id, friend_id, uid))
    if not cur.fetchone():
        cur.close(); conn.close()
        return jsonify({'error': 'Bu kullanıcı arkadaşın değil'}), 403

    cur.execute('SELECT id, username, goal_weight, height, created_at, last_active, avatar_data FROM users WHERE id = %s', (friend_id,))
    fu = cur.fetchone()
    if not fu:
        cur.close(); conn.close()
        return jsonify({'error': 'Kullanıcı bulunamadı'}), 404

    cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date DESC LIMIT 1', (friend_id,))
    cw = cur.fetchone()
    cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date ASC LIMIT 1', (friend_id,))
    sw = cur.fetchone()
    cur.execute('SELECT COUNT(*) AS c FROM workout_logs WHERE user_id = %s', (friend_id,))
    total_workouts = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*) AS c FROM workout_logs WHERE user_id = %s AND date >= %s",
        (friend_id, (date.today() - timedelta(days=date.today().weekday())).isoformat())
    )
    weekly = cur.fetchone()
    cur.execute('SELECT MAX(date) AS d FROM workout_logs WHERE user_id = %s', (friend_id,))
    last_workout = cur.fetchone()

    # basit ardışık gün serisi (son 14 gün içinde)
    cur.execute("SELECT DISTINCT date FROM workout_logs WHERE user_id = %s ORDER BY date DESC LIMIT 14", (friend_id,))
    dates = {str(row['date'])[:10] for row in cur.fetchall()}
    streak = 0
    d = date.today()
    while str(d) in dates:
        streak += 1
        d -= timedelta(days=1)

    cur.close(); conn.close()

    current_weight = cw['weight'] if cw else None
    start_weight = sw['weight'] if sw else None
    lost_kg = round(start_weight - current_weight, 1) if (start_weight is not None and current_weight is not None) else None
    remaining_kg = round(abs(current_weight - fu['goal_weight']), 1) if (current_weight is not None and fu['goal_weight']) else None

    progress_pct = None
    if start_weight is not None and current_weight is not None and fu['goal_weight']:
        total_needed = abs(start_weight - fu['goal_weight'])
        done = abs(start_weight - current_weight)
        if total_needed > 0:
            progress_pct = max(0, min(100, round(done / total_needed * 100)))

    return jsonify({
        'id': fu['id'],
        'username': fu['username'],
        'height': fu['height'],
        'goal_weight': fu['goal_weight'],
        'current_weight': current_weight,
        'start_weight': start_weight,
        'lost_kg': lost_kg,
        'remaining_kg': remaining_kg,
        'progress_pct': progress_pct,
        'total_workouts': total_workouts['c'] if total_workouts else 0,
        'weekly_workouts': weekly['c'] if weekly else 0,
        'last_workout': last_workout['d'] if last_workout else None,
        'streak': streak,
        'member_since': str(fu['created_at'])[:10] if fu.get('created_at') else None,
        'last_active': fu['last_active'].isoformat() if fu.get('last_active') else None,
        'avatar_data': fu.get('avatar_data'),
    })


@app.route('/api/friends/<int:friend_id>', methods=['DELETE'])
def friends_remove(friend_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    uid = session['user_id']
    conn = get_db(); cur = conn.cursor()
    cur.execute('''
        DELETE FROM friendships
        WHERE status = 'accepted' AND (
            (requester_id = %s AND addressee_id = %s) OR
            (requester_id = %s AND addressee_id = %s)
        )
    ''', (uid, friend_id, friend_id, uid))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'success': True})


PR_EXERCISES = ('bench', 'squat', 'deadlift')


def estimate_1rm(weight, reps):
    """Epley formülü: 1 tekrarlık tahmini maksimum."""
    reps = max(1, int(reps))
    return round(float(weight) * (1 + reps / 30.0), 1)


@app.route('/api/prs/me')
def prs_me():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT exercise, weight, reps, updated_at FROM personal_records WHERE user_id = %s', (session['user_id'],))
    rows = {r['exercise']: {
        'weight': r['weight'], 'reps': r['reps'],
        'e1rm': estimate_1rm(r['weight'], r['reps']),
        'updated_at': str(r['updated_at'])[:10] if r['updated_at'] else None,
    } for r in cur.fetchall()}
    cur.close(); conn.close()
    return jsonify(rows)


@app.route('/api/prs', methods=['POST'])
def prs_save():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    d = request.json or {}
    exercise = (d.get('exercise') or '').strip().lower()
    weight = d.get('weight')
    reps = d.get('reps') or 1
    if exercise not in PR_EXERCISES:
        return jsonify({'error': 'Geçersiz hareket'}), 400
    try:
        weight = float(weight)
        reps = int(reps)
        if weight <= 0 or reps <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': 'Geçersiz kilo/tekrar değeri'}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute('''
        INSERT INTO personal_records (user_id, exercise, weight, reps, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id, exercise)
        DO UPDATE SET weight = EXCLUDED.weight, reps = EXCLUDED.reps, updated_at = NOW()
    ''', (session['user_id'], exercise, weight, reps))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'success': True, 'e1rm': estimate_1rm(weight, reps)})


@app.route('/api/prs/leaderboard')
def prs_leaderboard():
    if 'user_id' not in session:
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    exercise = (request.args.get('exercise') or '').strip().lower()
    if exercise not in PR_EXERCISES:
        return jsonify({'error': 'Geçersiz hareket'}), 400

    uid = session['user_id']
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT CASE WHEN requester_id = %s THEN addressee_id ELSE requester_id END AS friend_id
        FROM friendships
        WHERE status = 'accepted' AND (requester_id = %s OR addressee_id = %s)
    ''', (uid, uid, uid))
    ids = [r['friend_id'] for r in cur.fetchall()] + [uid]

    rows = []
    for pid in ids:
        cur.execute('SELECT username, avatar_data FROM users WHERE id = %s', (pid,))
        u = cur.fetchone()
        if not u:
            continue
        cur.execute('SELECT weight, reps, updated_at FROM personal_records WHERE user_id = %s AND exercise = %s', (pid, exercise))
        pr = cur.fetchone()
        if not pr:
            continue
        rows.append({
            'user_id': pid,
            'username': u['username'],
            'avatar_data': u.get('avatar_data'),
            'weight': pr['weight'],
            'reps': pr['reps'],
            'e1rm': estimate_1rm(pr['weight'], pr['reps']),
            'is_me': pid == uid,
        })

    cur.close(); conn.close()
    rows.sort(key=lambda r: r['e1rm'], reverse=True)
    for i, r in enumerate(rows):
        r['rank'] = i + 1
    return jsonify(rows)


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/')
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT id FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    cur.close(); conn.close()
    if not u:
        session.clear()
        return redirect('/')
    return render_template('dashboard.html')


# ====================== KİLO ======================

@app.route('/api/weight', methods=['GET'])
def get_weights():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM weight_logs WHERE user_id = %s ORDER BY date', (session['user_id'],))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([{'id': r['id'], 'weight': r['weight'], 'date': r['date'], 'notes': r['notes']} for r in rows])


@app.route('/api/streak')
def get_streak():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    uid = session['user_id']
    conn = get_db(); cur = conn.cursor()
    cur.execute('''
        SELECT date FROM weight_logs WHERE user_id = %s
        UNION
        SELECT date FROM workout_logs WHERE user_id = %s
        UNION
        SELECT date FROM nutrition_logs WHERE user_id = %s
        UNION
        SELECT TO_CHAR(updated_at, 'YYYY-MM-DD') FROM personal_records WHERE user_id = %s
    ''', (uid, uid, uid, uid))
    active_dates = {row[0] for row in cur.fetchall() if row[0]}
    cur.close(); conn.close()

    today_str = date.today().isoformat()
    streak = 0
    d = date.today()
    while d.isoformat() in active_dates:
        streak += 1
        d -= timedelta(days=1)

    return jsonify({'streak': streak, 'today_active': today_str in active_dates})


@app.route('/api/weight', methods=['POST'])
def add_weight():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'INSERT INTO weight_logs (user_id,weight,date,notes) VALUES (%s,%s,%s,%s) RETURNING id',
        (session['user_id'], d['weight'], d.get('date', date.today().isoformat()), d.get('notes', ''))
    )
    lid = cur.fetchone()[0]; conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True, 'id': lid})


@app.route('/api/weight/<int:lid>', methods=['DELETE'])
def delete_weight(lid):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM weight_logs WHERE id = %s AND user_id = %s', (lid, session['user_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== ANTRENMAN ======================

@app.route('/api/workouts', methods=['GET'])
def get_workouts():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        'SELECT * FROM workout_logs WHERE user_id = %s ORDER BY date DESC LIMIT 50',
        (session['user_id'],)
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([{
        'id': r['id'], 'name': r['name'], 'duration': r['duration'],
        'calories_burned': r['calories_burned'], 'workout_type': r['workout_type'],
        'date': r['date'], 'notes': r['notes'],
    } for r in rows])


@app.route('/api/workouts', methods=['POST'])
def add_workout():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    uid = session['user_id']
    workout_date = d.get('date', date.today().isoformat())

    conn = get_db(); cur = conn.cursor()

    # Bugün (ya da seçilen tarihte) bu kullanıcının ilk kaydı mı kontrol et
    # (çoklu hareket eklerken arkadaşlara tek bildirim gitsin, her hareket için ayrı ayrı değil)
    cur.execute('SELECT COUNT(*) FROM workout_logs WHERE user_id = %s AND date = %s', (uid, workout_date))
    is_first_of_day = cur.fetchone()[0] == 0

    cur.execute(
        'INSERT INTO workout_logs (user_id,name,workout_type,date,duration,calories_burned,notes) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (uid, d['name'], d.get('workout_type', 'other'),
         workout_date, d.get('duration', 0),
         d.get('calories_burned', 0), d.get('notes', ''))
    )
    lid = cur.fetchone()[0]; conn.commit(); cur.close(); conn.close()

    if is_first_of_day:
        notify_friends_of_activity(uid, 'workout')

    return jsonify({'success': True, 'id': lid})


@app.route('/api/workouts/<int:lid>', methods=['DELETE'])
def delete_workout(lid):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM workout_logs WHERE id = %s AND user_id = %s', (lid, session['user_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== İSTATİSTİK ======================

@app.route('/api/stats')
def get_stats():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    uid = session['user_id']
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date', (uid,))
    weights = cur.fetchall()
    week_ago = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    cur.execute(
        'SELECT duration, calories_burned FROM workout_logs WHERE user_id = %s AND date >= %s',
        (uid, week_ago)
    )
    ww = cur.fetchall(); cur.close(); conn.close()
    cur_w = weights[-1]['weight'] if weights else None
    st_w  = weights[0]['weight']  if weights else None
    return jsonify({
        'current_weight':      cur_w,
        'weight_change':       round(cur_w - st_w, 1) if cur_w and st_w else 0,
        'workouts_this_week':  len(ww),
        'calories_this_week':  sum(w['calories_burned'] or 0 for w in ww),
        'duration_this_week':  sum(w['duration'] or 0 for w in ww),
        'total_logs':          len(weights),
    })


# ====================== BESLENİM ======================

@app.route('/api/nutrition', methods=['GET'])
def get_nutrition():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        'SELECT * FROM nutrition_logs WHERE user_id = %s ORDER BY date DESC LIMIT 100',
        (session['user_id'],)
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([{
        'id': r['id'], 'name': r['name'], 'meal': r['meal'],
        'calories': r['calories'], 'protein': r['protein'],
        'carbs': r['carbs'], 'fat': r['fat'], 'date': r['date'],
    } for r in rows])


@app.route('/api/nutrition', methods=['POST'])
def add_nutrition():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'INSERT INTO nutrition_logs (user_id,name,meal,calories,protein,carbs,fat,date) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (session['user_id'], d['name'], d.get('meal', 'other'), d.get('calories', 0),
         d.get('protein', 0), d.get('carbs', 0), d.get('fat', 0),
         d.get('date', date.today().isoformat()))
    )
    lid = cur.fetchone()[0]; conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True, 'id': lid})


@app.route('/api/nutrition/<int:lid>', methods=['DELETE'])
def delete_nutrition(lid):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM nutrition_logs WHERE id = %s AND user_id = %s', (lid, session['user_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== ÖLÇÜMLER ======================

@app.route('/api/measurements', methods=['GET'])
def get_measurements():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM measurements WHERE user_id = %s ORDER BY date', (session['user_id'],))
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([{
        'id': r['id'], 'date': r['date'], 'waist': r['waist'],
        'chest': r['chest'], 'hip': r['hip'], 'arm': r['arm'],
        'thigh': r['thigh'], 'neck': r.get('neck'),
    } for r in rows])


@app.route('/api/measurements', methods=['POST'])
def add_measurement():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'INSERT INTO measurements (user_id,date,waist,chest,hip,arm,thigh,neck) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (session['user_id'], d.get('date', date.today().isoformat()),
         d.get('waist'), d.get('chest'), d.get('hip'),
         d.get('arm'), d.get('thigh'), d.get('neck'))
    )
    lid = cur.fetchone()[0]; conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True, 'id': lid})


@app.route('/api/measurements/<int:lid>', methods=['DELETE'])
def delete_measurement(lid):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM measurements WHERE id = %s AND user_id = %s', (lid, session['user_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== PROFİL ======================

@app.route('/api/profile', methods=['POST'])
def update_profile():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'UPDATE users SET height=%s, goal_weight=%s, age=%s, gender=%s, activity_level=%s, goal_type=%s, goal_intensity=%s WHERE id=%s',
        (d.get('height'), d.get('goal_weight'), d.get('age'),
         d.get('gender'), d.get('activity_level', 'moderate'),
         d.get('goal_type', 'lose'), d.get('goal_intensity', 'moderate'), session['user_id'])
    )
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


@app.route('/api/change-password', methods=['POST'])
def change_password():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT password FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    if u['password'] != hash_pw(d['old_password']):
        cur.close(); conn.close()
        return jsonify({'error': 'Mevcut şifre yanlış'}), 400
    cur.execute('UPDATE users SET password = %s WHERE id = %s', (hash_pw(d['new_password']), session['user_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== ÖDEME (confirm) ======================

@app.route('/api/payment/confirm', methods=['POST'])
def confirm_payment():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'UPDATE payments SET status = %s WHERE id = %s AND user_id = %s',
        ('completed', d['payment_id'], session['user_id'])
    )
    cur.execute('UPDATE users SET is_premium = 1 WHERE id = %s', (session['user_id'],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success': True})


# ====================== AI ======================

@app.route('/api/ai/custom-program', methods=['POST'])
def ai_custom_program():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date DESC LIMIT 1', (session['user_id'],))
    weights = cur.fetchone()
    cur.close(); conn.close()
    if not u['is_premium']: return jsonify({'error': 'Premium özellik'}), 403

    d = request.json
    answers = d.get('answers', {})
    current_weight = weights['weight'] if weights else 'belirtilmemiş'

    split_labels = {
        'fullbody':    'Full Body (her seanste tüm vücut)',
        'upper_lower': 'Upper/Lower Split',
        'ppl':         'Push Pull Legs Split',
        'brosplit':    'Bro Split (günlük tek kas grubu)',
    }
    cardio_labels = {
        'none':           'Kardio yok',
        'end_of_workout': 'Her antrenmandan sonra 10-15 dk steady-state kardio',
        'same_day_hiit':  'Antrenmanla aynı günde HIIT',
        'separate_days':  'Kardio ve ağırlık günleri tamamen ayrı',
        'mixed':          'Karma: antrenman sonu + ayrı kardio günleri',
    }
    equip_labels = {
        'none':     'Sadece vücut ağırlığı',
        'minimal':  'Dumbbell ve direnç bandı',
        'home_gym': 'Barbell, dumbbell, bench',
        'full_gym': 'Tam spor salonu (tüm ekipmanlar)',
    }
    limit_labels = {
        'none':     'Fiziksel kısıtlama yok',
        'back':     'Bel/sırt problemi var',
        'knee':     'Diz problemi var',
        'shoulder': 'Omuz problemi var',
    }

    prompt = f"""Sen deneyimli bir kişisel fitness antrenörüsün. Aşağıdaki bilgilere göre gerçekçi, bilimsel temelli ve tamamen kişiselleştirilmiş antrenman programı oluştur.

KULLANICI PROFİLİ:
- Yaş: {u['age'] or 'belirtilmemiş'}
- Cinsiyet: {'Erkek' if u['gender']=='male' else 'Kadın' if u['gender']=='female' else 'belirtilmemiş'}
- Boy: {u['height'] or '?'} cm / Kilo: {current_weight} kg
- Hedef kilo: {u['goal_weight'] or 'belirtilmemiş'} kg
- Aktivite: {u['activity_level'] or 'orta'}

PROGRAM TERCİHLERİ:
- Hedef: {answers.get('goal','genel')}
- Program tipi: {split_labels.get(answers.get('split',''), answers.get('split',''))}
- Seviye: {answers.get('level','beginner')}
- Haftada gün: {answers.get('days','3')}
- Kardio: {cardio_labels.get(answers.get('cardio','none'), answers.get('cardio',''))}
- Süre: {answers.get('duration','60')} dakika
- Ekipman: {equip_labels.get(answers.get('equipment',''), answers.get('equipment',''))}
- Kısıtlama: {limit_labels.get(answers.get('limitation','none'), answers.get('limitation',''))}

KURALLAR:
1. Program tipine kesinlikle uy.
2. Kardio tercihine göre hareket et.
3. Sadece seçilen ekipmana uygun egzersizler kullan.
4. Fiziksel kısıtlama varsa alternatif ver.
5. Seviyeye göre set/tekrar ayarla.
6. Her egzersizde kısa teknik not olsun.

SADECE JSON döndür:
{{
  "program_adi": "...",
  "aciklama": "...",
  "haftalik_ozet": "...",
  "gunler": [
    {{
      "gun": "Pazartesi",
      "tip": "Push",
      "odak": "Göğüs, Omuz, Triceps",
      "isitma": ["5 dk hafif koşu"],
      "egzersizler": [
        {{"ad": "Barbell Bench Press", "set": 4, "tekrar": "6-8", "dinlenme": "90sn", "not": "Kürek kemiklerini sıkıştır"}}
      ],
      "kardio": "20 dk orta tempo koşu",
      "soguma": ["Göğüs germe 30sn"]
    }}
  ],
  "beslenme_ipuclari": ["..."],
  "progressiyon": "Her hafta ağırlığı 2.5kg artır...",
  "onemli_notlar": ["..."]
}}"""

    try:
        payload = json.dumps({
            'model': 'llama-3.3-70b-versatile',
            'max_tokens': 3000,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode('utf-8')

        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/chat/completions',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'User-Agent': 'FitPro/1.0',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        text = result['choices'][0]['message']['content']
        clean = text.strip()
        if '```' in clean:
            clean = clean.split('```')[1]
            if clean.startswith('json'): clean = clean[4:]
        program = json.loads(clean.strip())
        return jsonify({'program': program})

    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8')
        return jsonify({'error': f'API hatası: {err}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT is_premium FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    if not u['is_premium']:
        cur.close(); conn.close()
        return jsonify({'error': 'Premium özellik'}), 403
    cur.execute('SELECT weight FROM weight_logs WHERE user_id = %s ORDER BY date', (session['user_id'],))
    weights = cur.fetchall()
    cur.execute(
        'SELECT duration, workout_type FROM workout_logs WHERE user_id = %s ORDER BY date DESC LIMIT 20',
        (session['user_id'],)
    )
    workouts = cur.fetchall(); cur.close(); conn.close()

    ws = [w['weight'] for w in weights]
    if len(ws) < 2:
        return jsonify({'analysis': {
            'summary': 'Daha fazla kilo kaydı girerek detaylı analiz alın.',
            'trend': 'neutral',
            'suggestions': ['Her gün kilo kaydı girin', 'Antrenman loglarınızı ekleyin'],
            'progress_score': 10,
        }})
    change = ws[-1] - ws[0]
    weekly = change / max(len(ws) / 7, 1)
    total_w = len(workouts)
    avg_dur = sum(w['duration'] or 0 for w in workouts) / max(total_w, 1)

    if change < 0:
        trend, summary = 'down', f'Harika! {len(ws)} kayıtta toplam {abs(round(change,1))} kg verdiniz. Haftalık ort. {abs(round(weekly,2))} kg düşüş.'
    elif change > 0:
        trend, summary = 'up', f'{len(ws)} kayıtta {round(change,1)} kg aldınız. Antrenman yoğunluğunu artırın.'
    else:
        trend, summary = 'neutral', 'Kilonuz stabil. Hedefe göre diyet veya antrenman yoğunluğunu ayarlayın.'

    suggs = []
    if total_w < 3: suggs.append('Haftada en az 3 antrenman yapmanızı öneririz')
    if avg_dur < 30: suggs.append('Antrenman sürenizi 45-60 dakikaya çıkarmayı deneyin')
    if len(ws) < 7: suggs.append('Daha tutarlı takip için her gün ölçüm yapın')
    if not suggs: suggs.append('Harika gidiyorsunuz, bu rutini koruyun!')

    return jsonify({'analysis': {
        'summary': summary, 'trend': trend, 'suggestions': suggs,
        'progress_score': min(100, total_w * 10 + len(ws) * 3),
    }})


@app.route('/api/ai/program', methods=['POST'])
def ai_program():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT is_premium FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone(); cur.close(); conn.close()
    if not u['is_premium']: return jsonify({'error': 'Premium özellik'}), 403

    goal = request.json.get('goal', 'general')
    programs = {
        'fat_loss': {'name': 'Yağ Yakma Programı', 'description': 'HIIT ve güç antrenmanı kombinasyonu', 'days': [
            {'day': 'Pazartesi', 'focus': 'HIIT + Üst Vücut', 'exercises': [
                {'name': 'Burpee',           'sets': 3, 'reps': '12',    'rest': '30sn'},
                {'name': 'Push-up',          'sets': 4, 'reps': '15',    'rest': '45sn'},
                {'name': 'Dumbbell Row',     'sets': 3, 'reps': '12',    'rest': '60sn'},
                {'name': 'Mountain Climbers','sets': 3, 'reps': '30sn',  'rest': '30sn'},
            ]},
            {'day': 'Çarşamba', 'focus': 'Alt Vücut + Kardio', 'exercises': [
                {'name': 'Squat',      'sets': 4, 'reps': '15',      'rest': '60sn'},
                {'name': 'Lunges',     'sets': 3, 'reps': '12/taraf','rest': '45sn'},
                {'name': 'Jump Rope',  'sets': 1, 'reps': '10dk',    'rest': '0'},
                {'name': 'Plank',      'sets': 3, 'reps': '45sn',    'rest': '30sn'},
            ]},
            {'day': 'Cuma', 'focus': 'Full Body + Core', 'exercises': [
                {'name': 'Deadlift',      'sets': 3, 'reps': '10', 'rest': '90sn'},
                {'name': 'Pull-up',       'sets': 3, 'reps': '8',  'rest': '60sn'},
                {'name': 'Russian Twist', 'sets': 3, 'reps': '20', 'rest': '30sn'},
                {'name': 'Box Jump',      'sets': 3, 'reps': '10', 'rest': '60sn'},
            ]},
        ]},
        'muscle_gain': {'name': 'Kas Geliştirme Programı', 'description': 'Hypertrophy odaklı split program', 'days': [
            {'day': 'Pazartesi', 'focus': 'Göğüs + Triceps', 'exercises': [
                {'name': 'Bench Press',          'sets': 4, 'reps': '8-10',  'rest': '90sn'},
                {'name': 'Incline Dumbbell Press','sets': 3, 'reps': '10-12','rest': '75sn'},
                {'name': 'Cable Fly',            'sets': 3, 'reps': '12-15','rest': '60sn'},
                {'name': 'Tricep Pushdown',      'sets': 3, 'reps': '12',   'rest': '60sn'},
            ]},
            {'day': 'Çarşamba', 'focus': 'Sırt + Biceps', 'exercises': [
                {'name': 'Deadlift',    'sets': 4, 'reps': '6-8', 'rest': '120sn'},
                {'name': 'Pull-up',     'sets': 4, 'reps': 'Max', 'rest': '90sn'},
                {'name': 'Barbell Row', 'sets': 3, 'reps': '10',  'rest': '75sn'},
                {'name': 'Barbell Curl','sets': 3, 'reps': '12',  'rest': '60sn'},
            ]},
            {'day': 'Cuma', 'focus': 'Bacak + Omuz', 'exercises': [
                {'name': 'Squat',         'sets': 4, 'reps': '8-10', 'rest': '120sn'},
                {'name': 'Leg Press',     'sets': 3, 'reps': '12',   'rest': '90sn'},
                {'name': 'Military Press','sets': 3, 'reps': '10',   'rest': '75sn'},
                {'name': 'Lateral Raise', 'sets': 3, 'reps': '15',   'rest': '45sn'},
            ]},
        ]},
        'general': {'name': 'Genel Fitness Programı', 'description': 'Dengeli kuvvet ve kondisyon programı', 'days': [
            {'day': 'Pazartesi', 'focus': 'Full Body A', 'exercises': [
                {'name': 'Squat',        'sets': 3, 'reps': '10',   'rest': '75sn'},
                {'name': 'Push-up',      'sets': 3, 'reps': '12',   'rest': '60sn'},
                {'name': 'Dumbbell Row', 'sets': 3, 'reps': '10',   'rest': '60sn'},
                {'name': 'Plank',        'sets': 3, 'reps': '30sn', 'rest': '30sn'},
            ]},
            {'day': 'Çarşamba', 'focus': 'Kardio + Core', 'exercises': [
                {'name': 'Koşu/Bisiklet','sets': 1, 'reps': '20-30dk','rest': '0'},
                {'name': 'Crunch',       'sets': 3, 'reps': '20',     'rest': '30sn'},
                {'name': 'Leg Raise',    'sets': 3, 'reps': '15',     'rest': '30sn'},
            ]},
            {'day': 'Cuma', 'focus': 'Full Body B', 'exercises': [
                {'name': 'Deadlift',     'sets': 3, 'reps': '8',  'rest': '90sn'},
                {'name': 'Shoulder Press','sets': 3, 'reps': '10','rest': '60sn'},
                {'name': 'Bicep Curl',   'sets': 3, 'reps': '12', 'rest': '45sn'},
                {'name': 'Tricep Dip',   'sets': 3, 'reps': '12', 'rest': '45sn'},
            ]},
        ]},
    }
    return jsonify({'program': programs.get(goal, programs['general'])})


# ====================== SHOPIER YARDIMCI ======================

def _shopier_signature(random_nr, order_id, amount, currency=0):
    data = f"{random_nr}{order_id}{amount}{currency}"
    digest = hmac.new(
        SHOPIER_API_SECRET.encode('utf-8'),
        data.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode('utf-8')


def _verify_shopier_callback(params):
    random_nr  = params.get('random_nr')
    order_id   = params.get('platform_order_id')
    signature  = params.get('signature')
    if not all([random_nr, order_id, signature, SHOPIER_API_SECRET]):
        return False
    expected = hmac.new(
        SHOPIER_API_SECRET.encode('utf-8'),
        f"{random_nr}{order_id}".encode('utf-8'),
        hashlib.sha256
    ).digest()
    try:
        received = base64.b64decode(signature)
    except Exception:
        return False
    return hmac.compare_digest(received, expected)


def _order_already_processed(shopier_order_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'SELECT shopier_order_id FROM processed_shopier_orders WHERE shopier_order_id = %s',
        (str(shopier_order_id),)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    return row is not None


def activate_premium_from_payment(email=None, user_id=None, shopier_order_id=None):
    if shopier_order_id and _order_already_processed(shopier_order_id):
        return False

    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = None
    if user_id:
        cur.execute('SELECT id, email FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
    elif email:
        cur.execute(
            'SELECT id, email FROM users WHERE lower(trim(email)) = lower(trim(%s))',
            (email,)
        )
        user = cur.fetchone()

    if not user:
        cur.close(); conn.close()
        print(f"Shopier: kullanıcı bulunamadı email={email} user_id={user_id}")
        return False

    uid = user['id']
    cur2 = conn.cursor()
    cur2.execute('UPDATE users SET is_premium = 1 WHERE id = %s', (uid,))
    cur2.execute(
        "UPDATE pending_checkouts SET status='completed', shopier_order_id=%s "
        "WHERE user_id=%s AND status='pending'",
        (str(shopier_order_id) if shopier_order_id else None, uid)
    )
    if shopier_order_id:
        cur2.execute(
            'INSERT INTO processed_shopier_orders (shopier_order_id, user_id) '
            'VALUES (%s, %s) ON CONFLICT DO NOTHING',
            (str(shopier_order_id), uid)
        )
    conn.commit(); cur.close(); cur2.close(); conn.close()
    print(f"Shopier: premium aktif user_id={uid} order={shopier_order_id}")
    return True


def _process_shopier_order_via_api(order_id):
    if not SHOPIER_PAT:
        return False
    try:
        r = requests.get(
            f'{SHOPIER_API_BASE}/orders/{order_id}',
            headers={'Authorization': f'Bearer {SHOPIER_PAT}', 'Accept': 'application/json'},
            timeout=20
        )
        if r.status_code != 200:
            print(f"Shopier API order {order_id}: {r.status_code}")
            return False
        order = r.json()
        if order.get('paymentStatus') not in (None, 'paid'):
            return False
        shipping = order.get('shippingInfo') or {}
        billing  = order.get('billingInfo')  or {}
        email = shipping.get('email') or billing.get('email')
        if not email:
            return False
        return activate_premium_from_payment(email=email, shopier_order_id=str(order_id))
    except Exception as e:
        print(f"Shopier API hata: {e}")
        return False


def _create_pending_checkout(user_id, email, plan):
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "UPDATE pending_checkouts SET status='expired' WHERE user_id=%s AND status='pending'",
        (user_id,)
    )
    cur.execute(
        'INSERT INTO pending_checkouts (user_id, email, plan) VALUES (%s, %s, %s)',
        (user_id, email, plan)
    )
    conn.commit(); cur.close(); conn.close()


# ====================== SHOPIER ROUTE'LAR ======================

@app.route('/api/payment/create', methods=['POST'])
def create_shopier_payment():
    if 'user_id' not in session:
        return jsonify({'error': 'Önce giriş yapmalısın'}), 401

    data = request.get_json(silent=True) or {}
    plan = data.get('plan', 'monthly')

    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT id, username, email FROM users WHERE id = %s', (session['user_id'],))
    user = cur.fetchone(); cur.close(); conn.close()
    if not user:
        return jsonify({'error': 'Kullanıcı bulunamadı'}), 404

    email = user['email']
    _create_pending_checkout(user['id'], email, plan)

    if SHOPIER_API_SECRET and SHOPIER_API_KEY:
        amount     = 99 if plan == 'monthly' else 799
        username   = user['username'] or 'Kullanici'
        name_parts = username.split(' ', 1)
        order_id   = f"FP-{session['user_id']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        random_nr  = random.randint(100000, 999999)
        form_fields = {
            'API_key':           SHOPIER_API_KEY,
            'website_index':     '1',
            'platform_order_id': order_id,
            'product_name':      f'FitPro Premium - {plan.capitalize()}',
            'product_type':      '1',
            'total_order_value': str(amount),
            'currency':          '0',
            'platform':          '0',
            'is_in_frame':       '0',
            'current_language':  '0',
            'modul_version':     '1.0.4',
            'random_nr':         str(random_nr),
            'signature':         _shopier_signature(random_nr, order_id, amount),
            'callback':          SHOPIER_CALLBACK_URL,
            'buyer_name':        name_parts[0],
            'buyer_surname':     name_parts[1] if len(name_parts) > 1 else 'Kullanici',
            'buyer_email':       email,
            'buyer_account_age': '0',
            'buyer_id_nr':       str(session['user_id']),
            'buyer_phone':       '5555555555',
            'billing_address':   'Turkiye',
            'billing_city':      'Istanbul',
            'billing_country':   'Turkey',
            'billing_postcode':  '34000',
            'shipping_address':  'Turkiye',
            'shipping_city':     'Istanbul',
            'shipping_country':  'Turkey',
            'shipping_postcode': '34000',
        }
        return jsonify({'success': True, 'mode': 'form', 'payment_url': SHOPIER_PAYMENT_URL,
                        'form_fields': form_fields, 'email': email})

    return jsonify({
        'success': True, 'mode': 'redirect',
        'redirect_url': SHOPIER_PRODUCT_URL, 'email': email,
        'message': (
            f"Ödeme sırasında Shopier'da FitPro hesabınızdaki e-postayı kullanın: {email}"
        ),
    })


@app.route('/api/payment/status')
def payment_status():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT is_premium FROM users WHERE id = %s', (session['user_id'],))
    u = cur.fetchone()
    cur2 = conn.cursor()
    cur2.execute(
        "SELECT id FROM pending_checkouts WHERE user_id=%s AND status='pending' "
        "ORDER BY id DESC LIMIT 1",
        (session['user_id'],)
    )
    pending = cur2.fetchone()
    cur.close(); cur2.close(); conn.close()
    return jsonify({
        'is_premium':       bool(u and u['is_premium']),
        'pending_checkout': pending is not None,
    })


@app.route('/api/shopier/osb', methods=['POST'])
def shopier_osb_webhook():
    res = request.form.get('res') or request.values.get('res')
    sig = request.form.get('hash') or request.values.get('hash')
    if not res or not sig:
        return 'missing', 400

    if SHOPIER_OSB_USERNAME and SHOPIER_OSB_PASSWORD:
        expected = hmac.new(
            SHOPIER_OSB_PASSWORD.encode('utf-8'),
            (res + SHOPIER_OSB_USERNAME).encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            print('Shopier OSB: imza geçersiz')
            return 'unauthorized', 401

    try:
        payload = json.loads(base64.b64decode(res).decode('utf-8'))
    except Exception as e:
        print(f'Shopier OSB decode hata: {e}')
        return 'bad data', 400

    if str(payload.get('istest', '0')) == '1':
        print('Shopier OSB: test siparişi atlandı')
        return 'success', 200

    email    = payload.get('email')
    order_id = payload.get('orderid') or payload.get('order_id')
    if email:
        activate_premium_from_payment(email=email, shopier_order_id=order_id)
    return 'success', 200


@app.route('/api/shopier/webhook', methods=['POST'])
def shopier_rest_webhook():
    body_raw = request.get_data()
    sig = request.headers.get('Shopier-Signature', '')

    if SHOPIER_WEBHOOK_TOKEN and sig:
        expected = hmac.new(
            SHOPIER_WEBHOOK_TOKEN.encode('utf-8'),
            body_raw,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            print('Shopier webhook: imza geçersiz')
            return '', 401

    data = request.get_json(silent=True) or {}
    order_id = (
        data.get('id')
        or data.get('orderId')
        or (data.get('data') or {}).get('id')
        or (data.get('order') or {}).get('id')
    )
    if order_id:
        _process_shopier_order_via_api(order_id)
    return '', 200


@app.route('/api/payment/callback', methods=['POST'])
def shopier_callback():
    params = request.form
    if not _verify_shopier_callback(params):
        return 'ERROR', 400

    order_id = params.get('platform_order_id')
    status   = params.get('status')

    if status == 'success' and order_id:
        try:
            user_id = int(order_id.split('-')[1])
            activate_premium_from_payment(user_id=user_id, shopier_order_id=order_id)
            return 'OK', 200
        except Exception:
            pass
    return 'ERROR', 400


# ====================== BAŞLAT ======================

init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
