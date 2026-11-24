import os
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, redirect, render_template_string, request, session, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin123"))


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ortam değişkeni ayarlanmamış.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    id SERIAL PRIMARY KEY,
                    full_name VARCHAR(150) NOT NULL,
                    hire_date DATE NOT NULL,
                    carried_over_days INT DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leave_usages (
                    id SERIAL PRIMARY KEY,
                    employee_id INT REFERENCES employees(id) ON DELETE CASCADE,
                    leave_date DATE NOT NULL,
                    note VARCHAR(255),
                    UNIQUE(employee_id, leave_date)
                );
            """)
        conn.commit()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def calculate_earned_days(hire_date: date, carried_over: int, as_of: date) -> int:
    if as_of < hire_date:
        return carried_over

    completed_years = 0
    year = 1
    while True:
        anniversary = add_years(hire_date, year)
        if anniversary <= as_of:
            completed_years = year
            year += 1
        else:
            break

    earned = carried_over
    for y in range(1, completed_years + 1):
        earned += 20 if y >= 5 else 15
    return earned


def get_employee_summary(employee: dict, used_dates: set[date], as_of: date):
    hire_date = employee["hire_date"]
    carried = employee["carried_over_days"] or 0
    earned = calculate_earned_days(hire_date, carried, as_of)
    used_up_to_date = sum(1 for d in used_dates if d <= as_of)
    remaining = earned - used_up_to_date
    return {
        "earned": earned,
        "used": used_up_to_date,
        "remaining": remaining,
    }


def get_daily_ledger(employee: dict, used_dates: set[date], start_date: date, end_date: date):
    rows = []
    cur = start_date
    carried = employee["carried_over_days"] or 0
    while cur <= end_date:
        earned = calculate_earned_days(employee["hire_date"], carried, cur)
        used_so_far = sum(1 for d in used_dates if d <= cur)
        is_used = cur in used_dates
        balance = earned - used_so_far
        rows.append({
            "date": cur,
            "day_name": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"][cur.weekday()],
            "is_used": is_used,
            "earned": earned,
            "used_so_far": used_so_far,
            "balance": balance,
        })
        cur += timedelta(days=1)
    return rows


LOGIN_HTML = """
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <title>Admin Girişi - Yıllık İzin</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f4f6f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
    .box { background: #fff; padding: 28px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.08); width: 320px; }
    input { width: 100%; padding: 10px; margin: 8px 0 14px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
    button { width: 100%; padding: 10px; background: #1f4e78; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 15px; }
    .err { color: #b00020; margin-bottom: 10px; font-size: 14px; }
  </style>
</head>
<body>
  <div class="box">
    <h3>Admin Girişi</h3>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post">
      <label>Kullanıcı Adı</label>
      <input type="text" name="username" required>
      <label>Şifre</label>
      <input type="password" name="password" required>
      <button type="submit">Giriş Yap</button>
    </form>
  </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <title>Yıllık İzin Takip</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 20px; color: #222; }
    .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
    .card { background: #fff; padding: 18px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.06); margin-bottom: 20px; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { border: 1px solid #e0e0e0; padding: 8px 10px; text-align: left; font-size: 14px; }
    th { background: #eef3f8; }
    input, select, button { padding: 8px 10px; border: 1px solid #ccc; border-radius: 4px; }
    button { background: #1f4e78; color: #fff; border: none; cursor: pointer; }
    button.secondary { background: #555; }
    .row-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
    .badge-used { background: #ffe5e5; color: #900; font-weight: bold; }
  </style>
</head>
<body>
  <div class="top">
    <h2>Yıllık İzin Yönetim Paneli</h2>
    <div>
      <form method="get" style="display:inline;">
        <label>Referans Tarihi:</label>
        <input type="date" name="as_of" value="{{ as_of_str }}" onchange="this.form.submit()">
      </form>
      <a href="/logout" style="margin-left:12px;">Çıkış</a>
    </div>
  </div>

  {% if message %}<div class="card" style="background:#e8f5e9; color:#1b5e20;">{{ message }}</div>{% endif %}

  <div class="card">
    <h3>Yeni Personel Ekle</h3>
    <form method="post" action="/add-employee" class="row-form">
      <div><label>Ad Soyad</label><br><input type="text" name="full_name" required></div>
      <div><label>İşe Giriş Tarihi</label><br><input type="date" name="hire_date" required></div>
      <div><label>Devreden İzin</label><br><input type="number" name="carried_over_days" value="0" min="0" style="width:90px;"></div>
      <button type="submit">Personel Kaydet</button>
    </form>
  </div>

  <div class="card">
    <h3>İzin Kullanımı Gir (Tarih Aralığı)</h3>
    <form method="post" action="/add-leave" class="row-form">
      <div>
        <label>Personel</label><br>
        <select name="employee_id" required>
          {% for emp in employees %}
            <option value="{{ emp.id }}" {% if selected_emp and selected_emp.id == emp.id %}selected{% endif %}>
              {{ emp.full_name }} (Giriş: {{ emp.hire_date }})
            </option>
          {% endfor %}
        </select>
      </div>
      <div><label>Başlangıç Tarihi</label><br><input type="date" name="start_date" required></div>
      <div><label>Bitiş Tarihi</label><br><input type="date" name="end_date" required></div>
      <div>
        <label>Pazar Günleri</label><br>
        <select name="skip_sunday">
          <option value="1" selected>Pazar Sayılmasın (Cumartesi Dahil)</option>
          <option value="0">Pazar Dahil Tüm Günler</option>
        </select>
      </div>
      <div><label>Not</label><br><input type="text" name="note" placeholder="Örn: Yıllık izin"></div>
      <button type="submit">İzni İşle</button>
    </form>
  </div>

  <div class="card">
    <h3>Personel İzin Durumu ({{ as_of_str }} itibarıyla)</h3>
    <table>
      <thead>
        <tr>
          <th>Personel</th>
          <th>İşe Giriş</th>
          <th>Hak Edilen</th>
          <th>Kullanılan</th>
          <th>Kalan İzin</th>
          <th>Günlük Döküm</th>
        </tr>
      </thead>
      <tbody>
        {% for item in summaries %}
        <tr>
          <td>{{ item.emp.full_name }}</td>
          <td>{{ item.emp.hire_date }}</td>
          <td>{{ item.earned }}</td>
          <td>{{ item.used }}</td>
          <td><strong>{{ item.remaining }}</strong></td>
          <td><a href="/?emp_id={{ item.emp.id }}&as_of={{ as_of_str }}">Dökümü Gör</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {% if selected_emp %}
  <div class="card">
    <h3>Günlük Bakiye Akışı: {{ selected_emp.full_name }}</h3>
    <form method="get" class="row-form" style="margin-bottom:10px;">
      <input type="hidden" name="emp_id" value="{{ selected_emp.id }}">
      <input type="hidden" name="as_of" value="{{ as_of_str }}">
      <div><label>Başlangıç</label><br><input type="date" name="view_start" value="{{ view_start_str }}"></div>
      <div><label>Bitiş</label><br><input type="date" name="view_end" value="{{ view_end_str }}"></div>
      <button type="submit" class="secondary">Aralığı Göster</button>
    </form>
    <table>
      <thead>
        <tr>
          <th>Tarih</th>
          <th>Gün</th>
          <th>Durum</th>
          <th>O Güne Kadar Hak Ediş</th>
          <th>O Güne Kadar Kullanılan</th>
          <th>Kalan İzin</th>
        </tr>
      </thead>
      <tbody>
        {% for r in ledger %}
        <tr class="{% if r.is_used %}badge-used{% endif %}">
          <td>{{ r.date }}</td>
          <td>{{ r.day_name }}</td>
          <td>{% if r.is_used %}İzin Kullanıldı (-1){% else %}-{% endif %}</td>
          <td>{{ r.earned }}</td>
          <td>{{ r.used_so_far }}</td>
          <td><strong>{{ r.balance }}</strong></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Kullanıcı adı veya şifre hatalı."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    init_db()
    as_of_str = request.args.get("as_of") or date.today().isoformat()
    as_of = datetime.strptime(as_of_str, "%Y-%m-%d").date()

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM employees WHERE is_active = TRUE ORDER BY id;")
            employees = cur.fetchall()

            cur.execute("SELECT employee_id, leave_date FROM leave_usages;")
            usages_raw = cur.fetchall()

    usage_map: dict[int, set[date]] = {e["id"]: set() for e in employees}
    for u in usages_raw:
        if u["employee_id"] in usage_map:
            usage_map[u["employee_id"]].add(u["leave_date"])

    summaries = []
    for emp in employees:
        s = get_employee_summary(emp, usage_map.get(emp["id"], set()), as_of)
        summaries.append({"emp": emp, **s})

    emp_id_arg = request.args.get("emp_id")
    selected_emp = None
    ledger = []
    view_start_str = request.args.get("view_start") or date(as_of.year, 1, 1).isoformat()
    view_end_str = request.args.get("view_end") or date(as_of.year, 12, 31).isoformat()

    if emp_id_arg:
        for emp in employees:
            if str(emp["id"]) == str(emp_id_arg):
                selected_emp = emp
                v_start = datetime.strptime(view_start_str, "%Y-%m-%d").date()
                v_end = datetime.strptime(view_end_str, "%Y-%m-%d").date()
                ledger = get_daily_ledger(selected_emp, usage_map.get(emp["id"], set()), v_start, v_end)
                break

    message = request.args.get("msg")
    return render_template_string(
        DASHBOARD_HTML,
        employees=employees,
        summaries=summaries,
        as_of_str=as_of_str,
        selected_emp=selected_emp,
        ledger=ledger,
        view_start_str=view_start_str,
        view_end_str=view_end_str,
        message=message,
    )


@app.route("/add-employee", methods=["POST"])
@login_required
def add_employee():
    init_db()
    full_name = request.form["full_name"].strip()
    hire_date = request.form["hire_date"]
    carried_over_days = int(request.form.get("carried_over_days") or 0)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO employees (full_name, hire_date, carried_over_days) VALUES (%s, %s, %s);",
                (full_name, hire_date, carried_over_days),
            )
        conn.commit()
    return redirect(url_for("index", msg="Personel eklendi."))


@app.route("/add-leave", methods=["POST"])
@login_required
def add_leave():
    init_db()
    employee_id = int(request.form["employee_id"])
    start_date = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(request.form["end_date"], "%Y-%m-%d").date()
    skip_sunday = request.form.get("skip_sunday") == "1"
    note = request.form.get("note", "").strip()

    if end_date < start_date:
        return redirect(url_for("index", msg="Bitiş tarihi başlangıçtan küçük olamaz."))

    cur_date = start_date
    added_count = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            while cur_date <= end_date:
                if not (skip_sunday and cur_date.weekday() == 6):
                    cur.execute(
                        """
                        INSERT INTO leave_usages (employee_id, leave_date, note)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (employee_id, leave_date) DO NOTHING;
                        """,
                        (employee_id, cur_date, note),
                    )
                    added_count += 1
                cur_date += timedelta(days=1)
        conn.commit()

    return redirect(url_for("index", emp_id=employee_id, msg=f"{added_count} gün izin işlendi."))
