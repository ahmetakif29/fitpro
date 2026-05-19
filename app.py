from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3, hashlib, os, json
import urllib.request
from datetime import date, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = 'fitpro-secret-key-2024'
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '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')
DB_PATH = os.path.join(os.path.dirname(__file__), 'fitpro.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, is_premium INTEGER DEFAULT 0,
            height REAL, goal_weight REAL, age INTEGER, gender TEXT,
            activity_level TEXT DEFAULT 'moderate',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, weight REAL NOT NULL, date TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL, duration INTEGER,
            calories_burned INTEGER, workout_type TEXT, date TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, amount REAL NOT NULL, plan TEXT,
            status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    # Eski DB'ye eksik kolonları ekle (migration)
    try: conn.execute("ALTER TABLE users ADD COLUMN activity_level TEXT DEFAULT 'moderate'")
    except: pass
    try: conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
    except: pass
    try: conn.execute("ALTER TABLE measurements ADD COLUMN neck REAL")
    except: pass
    # Nutrition & measurements tables
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS nutrition_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL, meal TEXT,
            calories INTEGER DEFAULT 0, protein REAL DEFAULT 0,
            carbs REAL DEFAULT 0, fat REAL DEFAULT 0, date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, date TEXT NOT NULL,
            waist REAL, chest REAL, hip REAL, arm REAL, thigh REAL, neck REAL
        );
    ''')
    conn.commit()
    existing = conn.execute('SELECT id FROM users WHERE email=?', ('demo@fitpro.com',)).fetchone()
    if not existing:
        c = conn.cursor()
        c.execute('INSERT INTO users (username,email,password,height,goal_weight,age,gender) VALUES (?,?,?,?,?,?,?)',
                  ('demo_user','demo@fitpro.com',hash_pw('demo123'),175,75,28,'male'))
        uid = c.lastrowid
        today = date.today()
        for i in range(14):
            c.execute('INSERT INTO weight_logs (user_id,weight,date) VALUES (?,?,?)',
                      (uid, round(82-i*0.3,1), (today-timedelta(days=13-i)).isoformat()))
        for i,(nm,wt,dur,cal) in enumerate([('Göğüs Günü','strength',60,350),('Kardio','cardio',45,420),('Sırt Günü','strength',55,310),('Bacak Günü','strength',65,400),('HIIT','cardio',30,380)]):
            c.execute('INSERT INTO workout_logs (user_id,name,workout_type,duration,calories_burned,date) VALUES (?,?,?,?,?,?)',
                      (uid,nm,wt,dur,cal,(today-timedelta(days=i*2)).isoformat()))
        conn.commit()
    conn.close()

@app.route('/')
def index():
    if 'user_id' in session:
        conn = get_db()
        u = conn.execute('SELECT id FROM users WHERE id=?',(session['user_id'],)).fetchone()
        conn.close()
        if u:
            return redirect('/dashboard')
        else:
            session.clear()
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    d = request.json
    conn = get_db()
    try:
        c = conn.execute('INSERT INTO users (username,email,password,height,goal_weight,age,gender) VALUES (?,?,?,?,?,?,?)',
            (d['username'],d['email'],hash_pw(d['password']),d.get('height'),d.get('goal_weight'),d.get('age'),d.get('gender')))
        conn.commit()
        session['user_id'] = c.lastrowid
        return jsonify({'success':True,'user':{'id':c.lastrowid,'username':d['username'],'is_premium':False}})
    except sqlite3.IntegrityError:
        return jsonify({'error':'Email veya kullanıcı adı zaten kayıtlı'}),400
    finally: conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE email=?',(d['email'],)).fetchone()
    conn.close()
    if not u or u['password'] != hash_pw(d['password']):
        return jsonify({'error':'Email veya şifre hatalı'}),401
    session['user_id'] = u['id']
    return jsonify({'success':True,'user':{'id':u['id'],'username':u['username'],'is_premium':bool(u['is_premium'])}})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear(); return jsonify({'success':True})

@app.route('/api/me')
def me():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    conn.close()
    if not u:
        session.clear()
        return jsonify({'error':'Unauthorized'}),401
    return jsonify({'id':u['id'],'username':u['username'],'email':u['email'],'is_premium':bool(u['is_premium']),'height':u['height'],'goal_weight':u['goal_weight'],'age':u['age'],'gender':u['gender'],'activity_level':u['activity_level'] if 'activity_level' in u.keys() else 'moderate','created_at':u['created_at'] if 'created_at' in u.keys() else ''})

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect('/')
    conn = get_db()
    u = conn.execute('SELECT id FROM users WHERE id=?',(session['user_id'],)).fetchone()
    conn.close()
    if not u:
        session.clear()
        return redirect('/')
    return render_template('dashboard.html')

@app.route('/api/weight', methods=['GET'])
def get_weights():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    rows = conn.execute('SELECT * FROM weight_logs WHERE user_id=? ORDER BY date',(session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{'id':r['id'],'weight':r['weight'],'date':r['date'],'notes':r['notes']} for r in rows])

@app.route('/api/weight', methods=['POST'])
def add_weight():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    c = conn.execute('INSERT INTO weight_logs (user_id,weight,date,notes) VALUES (?,?,?,?)',
        (session['user_id'],d['weight'],d.get('date',date.today().isoformat()),d.get('notes','')))
    conn.commit(); lid = c.lastrowid; conn.close()
    return jsonify({'success':True,'id':lid})

@app.route('/api/weight/<int:lid>', methods=['DELETE'])
def delete_weight(lid):
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    conn.execute('DELETE FROM weight_logs WHERE id=? AND user_id=?',(lid,session['user_id']))
    conn.commit(); conn.close(); return jsonify({'success':True})

@app.route('/api/workouts', methods=['GET'])
def get_workouts():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    rows = conn.execute('SELECT * FROM workout_logs WHERE user_id=? ORDER BY date DESC LIMIT 50',(session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{'id':r['id'],'name':r['name'],'duration':r['duration'],'calories_burned':r['calories_burned'],'workout_type':r['workout_type'],'date':r['date'],'notes':r['notes']} for r in rows])

@app.route('/api/workouts', methods=['POST'])
def add_workout():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    c = conn.execute('INSERT INTO workout_logs (user_id,name,workout_type,date,duration,calories_burned,notes) VALUES (?,?,?,?,?,?,?)',
        (session['user_id'],d['name'],d.get('workout_type','other'),d.get('date',date.today().isoformat()),d.get('duration',0),d.get('calories_burned',0),d.get('notes','')))
    conn.commit(); lid = c.lastrowid; conn.close()
    return jsonify({'success':True,'id':lid})

@app.route('/api/workouts/<int:lid>', methods=['DELETE'])
def delete_workout(lid):
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    conn.execute('DELETE FROM workout_logs WHERE id=? AND user_id=?',(lid,session['user_id']))
    conn.commit(); conn.close(); return jsonify({'success':True})

@app.route('/api/stats')
def get_stats():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    uid = session['user_id']
    conn = get_db()
    weights = conn.execute('SELECT weight FROM weight_logs WHERE user_id=? ORDER BY date',(uid,)).fetchall()
    week_ago = (date.today()-timedelta(days=7)).isoformat()
    ww = conn.execute('SELECT duration,calories_burned FROM workout_logs WHERE user_id=? AND date>=?',(uid,week_ago)).fetchall()
    conn.close()
    cur = weights[-1]['weight'] if weights else None
    st = weights[0]['weight'] if weights else None
    return jsonify({'current_weight':cur,'weight_change':round(cur-st,1) if cur and st else 0,'workouts_this_week':len(ww),'calories_this_week':sum(w['calories_burned'] or 0 for w in ww),'duration_this_week':sum(w['duration'] or 0 for w in ww),'total_logs':len(weights)})

@app.route('/api/payment/create', methods=['POST'])
def create_payment():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json; plan = d.get('plan','monthly')
    amount = 99.0 if plan == 'monthly' else 799.0
    conn = get_db()
    c = conn.execute('INSERT INTO payments (user_id,amount,plan) VALUES (?,?,?)',(session['user_id'],amount,plan))
    conn.commit(); pid = c.lastrowid; conn.close()
    return jsonify({'payment_id':pid,'amount':amount,'plan':plan})

@app.route('/api/payment/confirm', methods=['POST'])
def confirm_payment():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json; conn = get_db()
    conn.execute('UPDATE payments SET status=? WHERE id=? AND user_id=?',('completed',d['payment_id'],session['user_id']))
    conn.execute('UPDATE users SET is_premium=1 WHERE id=?',(session['user_id'],))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/nutrition', methods=['GET'])
def get_nutrition():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    rows = conn.execute('SELECT * FROM nutrition_logs WHERE user_id=? ORDER BY date DESC LIMIT 100',(session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{'id':r['id'],'name':r['name'],'meal':r['meal'],'calories':r['calories'],'protein':r['protein'],'carbs':r['carbs'],'fat':r['fat'],'date':r['date']} for r in rows])

@app.route('/api/nutrition', methods=['POST'])
def add_nutrition():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    c = conn.execute('INSERT INTO nutrition_logs (user_id,name,meal,calories,protein,carbs,fat,date) VALUES (?,?,?,?,?,?,?,?)',
        (session['user_id'],d['name'],d.get('meal','other'),d.get('calories',0),d.get('protein',0),d.get('carbs',0),d.get('fat',0),d.get('date',date.today().isoformat())))
    conn.commit(); lid=c.lastrowid; conn.close()
    return jsonify({'success':True,'id':lid})

@app.route('/api/nutrition/<int:lid>', methods=['DELETE'])
def delete_nutrition(lid):
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    conn.execute('DELETE FROM nutrition_logs WHERE id=? AND user_id=?',(lid,session['user_id']))
    conn.commit(); conn.close(); return jsonify({'success':True})

@app.route('/api/measurements', methods=['GET'])
def get_measurements():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    rows = conn.execute('SELECT * FROM measurements WHERE user_id=? ORDER BY date',(session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{'id':r['id'],'date':r['date'],'waist':r['waist'],'chest':r['chest'],'hip':r['hip'],'arm':r['arm'],'thigh':r['thigh'],'neck':r['neck'] if 'neck' in r.keys() else None} for r in rows])

@app.route('/api/measurements', methods=['POST'])
def add_measurement():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    c = conn.execute('INSERT INTO measurements (user_id,date,waist,chest,hip,arm,thigh,neck) VALUES (?,?,?,?,?,?,?,?)',
        (session['user_id'],d.get('date',date.today().isoformat()),d.get('waist'),d.get('chest'),d.get('hip'),d.get('arm'),d.get('thigh'),d.get('neck')))
    conn.commit(); lid=c.lastrowid; conn.close()
    return jsonify({'success':True,'id':lid})

@app.route('/api/measurements/<int:lid>', methods=['DELETE'])
def delete_measurement(lid):
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    conn.execute('DELETE FROM measurements WHERE id=? AND user_id=?',(lid,session['user_id']))
    conn.commit(); conn.close(); return jsonify({'success':True})


@app.route('/api/profile', methods=['POST'])
def update_profile():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    conn.execute('''UPDATE users SET height=?, goal_weight=?, age=?, gender=?, activity_level=? WHERE id=?''',
        (d.get('height'), d.get('goal_weight'), d.get('age'), d.get('gender'), d.get('activity_level','moderate'), session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    d = request.json
    conn = get_db()
    u = conn.execute('SELECT password FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if u['password'] != hash_pw(d['old_password']):
        conn.close(); return jsonify({'error':'Mevcut şifre yanlış'}),400
    conn.execute('UPDATE users SET password=? WHERE id=?',(hash_pw(d['new_password']), session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True})


@app.route('/api/ai/custom-program', methods=['POST'])
def ai_custom_program():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    weights = conn.execute('SELECT weight FROM weight_logs WHERE user_id=? ORDER BY date DESC LIMIT 1',(session['user_id'],)).fetchone()
    conn.close()
    if not u['is_premium']: return jsonify({'error':'Premium özellik'}),403

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
1. Program tipine kesinlikle uy. PPL ise Push/Pull/Legs günleri olsun. Brosplit ise günde tek kas grubu.
2. Kardio tercihine göre hareket et. "Aynı gün HIIT" ise antrenman sonuna ekle. "Ayrı günler" ise ayrı kardio günü oluştur. "Yok" ise hiç koyma.
3. Sadece seçilen ekipmana uygun egzersizler kullan.
4. Fiziksel kısıtlama varsa o hareketi atla, alternatif ver.
5. Seviyeye göre set/tekrar ayarla. Başlangıç: 3x10-12, İleri: 4-5x6-8 + periodizasyon.
6. Her egzersizde kısa teknik not olsun.

SADECE JSON döndür, başka hiçbir şey yazma:
{{
  "program_adi": "...",
  "aciklama": "...",
  "haftalik_ozet": "...",
  "gunler": [
    {{
      "gun": "Pazartesi",
      "tip": "Push",
      "odak": "Göğüs, Omuz, Triceps",
      "isitma": ["5 dk hafif koşu", "..."],
      "egzersizler": [
        {{"ad": "Barbell Bench Press", "set": 4, "tekrar": "6-8", "dinlenme": "90sn", "not": "Kürek kemiklerini sıkıştır"}}
      ],
      "kardio": "20 dk orta tempo koşu (kalp atışı 130-150)",
      "soguma": ["Göğüs germe 30sn", "..."]
    }}
  ],
  "beslenme_ipuclari": ["...", "..."],
  "progressiyon": "Her hafta ağırlığı 2.5kg artır...",
  "onemli_notlar": ["...", "..."]
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
                'User-Agent': 'FitPro/1.0'
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


def ai_analyze():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    u = conn.execute('SELECT is_premium FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not u['is_premium']: conn.close(); return jsonify({'error':'Premium özellik'}),403
    weights = conn.execute('SELECT weight FROM weight_logs WHERE user_id=? ORDER BY date',(session['user_id'],)).fetchall()
    workouts = conn.execute('SELECT duration,workout_type FROM workout_logs WHERE user_id=? ORDER BY date DESC LIMIT 20',(session['user_id'],)).fetchall()
    conn.close()
    ws = [w['weight'] for w in weights]
    if len(ws) < 2:
        return jsonify({'analysis':{'summary':'Daha fazla kilo kaydı girerek detaylı analiz alın.','trend':'neutral','suggestions':['Her gün kilo kaydı girin','Antrenman loglarınızı ekleyin'],'progress_score':10}})
    change = ws[-1]-ws[0]; weekly = change/max(len(ws)/7,1)
    total_w = len(workouts); avg_dur = sum(w['duration'] or 0 for w in workouts)/max(total_w,1)
    if change<0: trend,summary='down',f'Harika! {len(ws)} kayıtta toplam {abs(round(change,1))} kg verdiniz. Haftalık ort. {abs(round(weekly,2))} kg düşüş.'
    elif change>0: trend,summary='up',f'{len(ws)} kayıtta {round(change,1)} kg aldınız. Antrenman yoğunluğunu artırın.'
    else: trend,summary='neutral','Kilonuz stabil. Hedefe göre diyet veya antrenman yoğunluğunu ayarlayın.'
    suggs=[]
    if total_w<3: suggs.append('Haftada en az 3 antrenman yapmanızı öneririz')
    if avg_dur<30: suggs.append('Antrenman sürenizi 45-60 dakikaya çıkarmayı deneyin')
    if len(ws)<7: suggs.append('Daha tutarlı takip için her gün ölçüm yapın')
    if not suggs: suggs.append('Harika gidiyorsunuz, bu rutini koruyun!')
    return jsonify({'analysis':{'summary':summary,'trend':trend,'suggestions':suggs,'progress_score':min(100,total_w*10+len(ws)*3)}})

@app.route('/api/ai/program', methods=['POST'])
def ai_program():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'}),401
    conn = get_db()
    u = conn.execute('SELECT is_premium FROM users WHERE id=?',(session['user_id'],)).fetchone()
    conn.close()
    if not u['is_premium']: return jsonify({'error':'Premium özellik'}),403
    goal = request.json.get('goal','general')
    programs = {
        'fat_loss':{'name':'Yağ Yakma Programı','description':'HIIT ve güç antrenmanı kombinasyonu','days':[
            {'day':'Pazartesi','focus':'HIIT + Üst Vücut','exercises':[{'name':'Burpee','sets':3,'reps':'12','rest':'30sn'},{'name':'Push-up','sets':4,'reps':'15','rest':'45sn'},{'name':'Dumbbell Row','sets':3,'reps':'12','rest':'60sn'},{'name':'Mountain Climbers','sets':3,'reps':'30sn','rest':'30sn'}]},
            {'day':'Çarşamba','focus':'Alt Vücut + Kardio','exercises':[{'name':'Squat','sets':4,'reps':'15','rest':'60sn'},{'name':'Lunges','sets':3,'reps':'12/taraf','rest':'45sn'},{'name':'Jump Rope','sets':1,'reps':'10dk','rest':'0'},{'name':'Plank','sets':3,'reps':'45sn','rest':'30sn'}]},
            {'day':'Cuma','focus':'Full Body + Core','exercises':[{'name':'Deadlift','sets':3,'reps':'10','rest':'90sn'},{'name':'Pull-up','sets':3,'reps':'8','rest':'60sn'},{'name':'Russian Twist','sets':3,'reps':'20','rest':'30sn'},{'name':'Box Jump','sets':3,'reps':'10','rest':'60sn'}]}]},
        'muscle_gain':{'name':'Kas Geliştirme Programı','description':'Hypertrophy odaklı split program','days':[
            {'day':'Pazartesi','focus':'Göğüs + Triceps','exercises':[{'name':'Bench Press','sets':4,'reps':'8-10','rest':'90sn'},{'name':'Incline Dumbbell Press','sets':3,'reps':'10-12','rest':'75sn'},{'name':'Cable Fly','sets':3,'reps':'12-15','rest':'60sn'},{'name':'Tricep Pushdown','sets':3,'reps':'12','rest':'60sn'}]},
            {'day':'Çarşamba','focus':'Sırt + Biceps','exercises':[{'name':'Deadlift','sets':4,'reps':'6-8','rest':'120sn'},{'name':'Pull-up','sets':4,'reps':'Max','rest':'90sn'},{'name':'Barbell Row','sets':3,'reps':'10','rest':'75sn'},{'name':'Barbell Curl','sets':3,'reps':'12','rest':'60sn'}]},
            {'day':'Cuma','focus':'Bacak + Omuz','exercises':[{'name':'Squat','sets':4,'reps':'8-10','rest':'120sn'},{'name':'Leg Press','sets':3,'reps':'12','rest':'90sn'},{'name':'Military Press','sets':3,'reps':'10','rest':'75sn'},{'name':'Lateral Raise','sets':3,'reps':'15','rest':'45sn'}]}]},
        'general':{'name':'Genel Fitness Programı','description':'Dengeli kuvvet ve kondisyon programı','days':[
            {'day':'Pazartesi','focus':'Full Body A','exercises':[{'name':'Squat','sets':3,'reps':'10','rest':'75sn'},{'name':'Push-up','sets':3,'reps':'12','rest':'60sn'},{'name':'Dumbbell Row','sets':3,'reps':'10','rest':'60sn'},{'name':'Plank','sets':3,'reps':'30sn','rest':'30sn'}]},
            {'day':'Çarşamba','focus':'Kardio + Core','exercises':[{'name':'Koşu/Bisiklet','sets':1,'reps':'20-30dk','rest':'0'},{'name':'Crunch','sets':3,'reps':'20','rest':'30sn'},{'name':'Leg Raise','sets':3,'reps':'15','rest':'30sn'}]},
            {'day':'Cuma','focus':'Full Body B','exercises':[{'name':'Deadlift','sets':3,'reps':'8','rest':'90sn'},{'name':'Shoulder Press','sets':3,'reps':'10','rest':'60sn'},{'name':'Bicep Curl','sets':3,'reps':'12','rest':'45sn'},{'name':'Tricep Dip','sets':3,'reps':'12','rest':'45sn'}]}]}
    }
    return jsonify({'program':programs.get(goal,programs['general'])})

init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
