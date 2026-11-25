import os
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, redirect, render_template_string, request, session, url_for
import holidays
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin123"))

TR_ALPHABET = {
    "a": 1, "b": 2, "c": 3, "ç": 4, "d": 5, "e": 6, "f": 7, "g": 8, "ğ": 9,
    "h": 10, "ı": 11, "i": 12, "İ": 12, "j": 13, "k": 14, "l": 15, "m": 16,
    "n": 17, "o": 18, "ö": 19, "p": 20, "r": 21, "s": 22, "ş": 23, "t": 24,
    "u": 25, "ü": 26, "v": 27, "y": 28, "z": 29,
}


def tr_sort_key(name: str):
    if not name:
        return []
    key = []
    for ch in name.strip():
        lower_ch = "i" if ch in ("I", "İ") else ch.lower()
        key.append(TR_ALPHABET.get(lower_ch, 100 + ord(lower_ch)))
    key.append(name.lower())
    return key


@app.template_filter("tr_date")
def tr_date_filter(value):
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.strptime(value, "%Y-%m-%d").date()
    return value.strftime("%d/%m/%Y")


def get_tr_holidays(years: list[int]):
    return holidays.country_holidays("TR", years=years, language="tr")


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


def get_accrual_on_date(hire_date: date, check_date: date) -> int:
    if check_date <= hire_date:
        return 0
    years_diff = check_date.year - hire_date.year
    if years_diff >= 1 and add_years(hire_date, years_diff) == check_date:
        return 20 if years_diff >= 5 else 15
    return 0


def get_next_accrual_info(hire_date: date, as_of: date) -> dict:
    year = 1
    while True:
        anniversary = add_years(hire_date, year)
        if anniversary > as_of:
            days = 20 if year >= 5 else 15
            return {"date": anniversary, "days": days, "year_no": year}
        year += 1


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


def get_employee_summary(employee: dict, usage_map_for_emp: dict[date, str], as_of: date):
    hire_date = employee["hire_date"]
    carried = employee["carried_over_days"] or 0
    earned = calculate_earned_days(hire_date, carried, as_of)
    used_up_to_date = sum(1 for d in usage_map_for_emp.keys() if d <= as_of)
    remaining = earned - used_up_to_date
    next_accrual = get_next_accrual_info(hire_date, as_of)
    return {
        "earned": earned,
        "used": used_up_to_date,
        "remaining": remaining,
        "next_accrual": next_accrual,
    }


def get_daily_ledger(employee: dict, usage_map_for_emp: dict[date, str], start_date: date, end_date: date):
    years = list(range(start_date.year, end_date.year + 1))
    tr_holidays = get_tr_holidays(years)

    rows = []
    cur = start_date
    carried = employee["carried_over_days"] or 0
    used_dates_set = set(usage_map_for_emp.keys())

    while cur <= end_date:
        earned = calculate_earned_days(employee["hire_date"], carried, cur)
        used_so_far = sum(1 for d in used_dates_set if d <= cur)
        is_used = cur in used_dates_set
        leave_note = usage_map_for_emp.get(cur, "")
        accrual_today = get_accrual_on_date(employee["hire_date"], cur)
        holiday_name = tr_holidays.get(cur)
        is_sunday = cur.weekday() == 6
        balance = earned - used_so_far

        rows.append({
            "date": cur,
            "day_name": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"][cur.weekday()],
            "is_used": is_used,
            "leave_note": leave_note,
            "accrual_today": accrual_today,
            "holiday_name": holiday_name,
            "is_sunday": is_sunday,
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
    th, td { border: 1px solid #e0e0e0; padding: 8px 10px; text-align: left; font-size: 14px; vertical-align: middle; }
    th { background: #eef3f8; }
    input, select, button { padding: 7px 10px; border: 1px solid #ccc; border-radius: 4px; }
    button { background: #1f4e78; color: #fff; border: none; cursor: pointer; font-size: 13px; }
    button:hover { opacity: 0.92; }
    button.secondary { background: #555; }
    button.danger { background: #c62828; }
    button.small { padding: 4px 8px; font-size: 12px; }
    .row-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
    .badge-used { background: #ffe5e5; color: #900; }
    .badge-accrual { background: #e8f5e9; color: #1b5e20; }
    .badge-holiday { background: #e3f2fd; color: #0d47a1; }
    .tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 12px; margin-right: 4px; margin-bottom: 2px; }
    .tag-red { background: #ffcdd2; color: #b71c1c; font-weight: bold; }
    .tag-green { background: #c8e6c9; color: #1b5e20; font-weight: bold; }
    .tag-blue { background: #bbdefb; color: #0d47a1; font-weight: bold; }
    .tag-gray { background: #eeeeee; color: #555; }
    .inline-form { display: inline-flex; gap: 5px; align-items: center; flex-wrap: wrap; margin-top: 4px; }
    .note-text { font-size: 12px; color: #444; font-style: italic; }
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
      <div><label>Devreden İzin</label><br><input type="number" name="carried_over_days" value="0" style="width:95px;"></div>
      <button type="submit">Personel Kaydet</button>
    </form>
  </div>

  <div class="card">
    <h3>İzin Kullanımı Gir (Tarih Aralığı)</h3>
    <form method="post" action="/add-leave" class="row-form">
      <div>
        <label>Personel (A-Z)</label><br>
        <select name="employee_id" required>
          {% for emp in employees %}
            <option value="{{ emp.id }}" {% if selected_emp and selected_emp.id == emp.id %}selected{% endif %}>
              {{ emp.full_name }} (Giriş: {{ emp.hire_date | tr_date }})
            </option>
          {% endfor %}
        </select>
      </div>
      <div><label>Başlangıç Tarihi</label><br><input type="date" name="start_date" required></div>
      <div><label>Bitiş Tarihi</label><br><input type="date" name="end_date" required></div>
      <div>
        <label>Pazar & Resmi Tatil</label><br>
        <select name="skip_sunday">
          <option value="1" selected>Pazar ve Resmi Tatiller Sayılmasın</option>
          <option value="0">Pazar Dahil (Resmi Tatiller Hariç)</option>
        </select>
      </div>
      <div><label>Not</label><br><input type="text" name="note" placeholder="Örn: Yıllık izin"></div>
      <button type="submit">İzni İşle</button>
    </form>
  </div>

  <div class="card">
    <h3>Personel İzin Durumu ({{ as_of | tr_date }} itibarıyla — Alfabetik)</h3>
    <table>
      <thead>
        <tr>
          <th>Personel</th>
          <th>İşe Giriş</th>
          <th>Sonraki İzin Hak Ediş Tarihi</th>
          <th>Hak Edilen</th>
          <th>Kullanılan</th>
          <th>Kalan İzin</th>
          <th>İşlemler</th>
        </tr>
      </thead>
      <tbody>
        {% for item in summaries %}
        <tr>
          <td><strong>{{ item.emp.full_name }}</strong></td>
          <td>{{ item.emp.hire_date | tr_date }}</td>
          <td><strong>{{ item.next_accrual.date | tr_date }}</strong> (+{{ item.next_accrual.days }} gün / {{ item.next_accrual.year_no }}. yıl)</td>
          <td>{{ item.earned }}</td>
          <td>{{ item.used }}</td>
          <td><strong>{{ item.remaining }}</strong></td>
          <td>
            <a href="/?emp_id={{ item.emp.id }}&as_of={{ as_of_str }}">Dökümü Gör</a>
            &nbsp;|&nbsp;
            <a href="/?emp_id={{ item.emp.id }}&edit_emp=1&as_of={{ as_of_str }}">Düzenle</a>
            &nbsp;|&nbsp;
            <form method="post" action="/delete-employee/{{ item.emp.id }}" style="display:inline;" onsubmit="return confirm('{{ item.emp.full_name }} adlı personeli ve tüm izin kayıtlarını silmek istediğine emin misin?');">
              <input type="hidden" name="as_of" value="{{ as_of_str }}">
              <button type="submit" class="danger small">Sil</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {% if selected_emp and edit_emp %}
  <div class="card" style="border-left: 4px solid #1f4e78;">
    <h3>Personel Bilgilerini Düzenle: {{ selected_emp.full_name }}</h3>
    <form method="post" action="/edit-employee/{{ selected_emp.id }}" class="row-form">
      <input type="hidden" name="as_of" value="{{ as_of_str }}">
      <div><label>Ad Soyad</label><br><input type="text" name="full_name" value="{{ selected_emp.full_name }}" required></div>
      <div><label>İşe Giriş Tarihi</label><br><input type="date" name="hire_date" value="{{ selected_emp.hire_date.isoformat() }}" required></div>
      <div><label>Devreden İzin</label><br><input type="number" name="carried_over_days" value="{{ selected_emp.carried_over_days or 0 }}" style="width:95px;"></div>
      <button type="submit">Değişiklikleri Kaydet</button>
      <a href="/?emp_id={{ selected_emp.id }}&as_of={{ as_of_str }}" style="margin-left:8px;">İptal</a>
    </form>
  </div>
  {% endif %}

  {% if selected_emp %}
  <div class="card">
    <h3>Günlük Bakiye Akışı: {{ selected_emp.full_name }} (İşe Giriş: {{ selected_emp.hire_date | tr_date }})</h3>

    <form method="get" class="row-form" style="margin-bottom:12px;">
      <input type="hidden" name="emp_id" value="{{ selected_emp.id }}">
      <input type="hidden" name="as_of" value="{{ as_of_str }}">
      <div><label>Başlangıç</label><br><input type="date" name="view_start" value="{{ view_start_str }}"></div>
      <div><label>Bitiş</label><br><input type="date" name="view_end" value="{{ view_end_str }}"></div>
      <button type="submit" class="secondary">Aralığı Göster</button>
    </form>

    <form method="post" action="/delete-leave-range" class="row-form" style="margin-bottom:14px; padding:10px; background:#fff5f5; border-radius:6px;" onsubmit="return confirm('Seçili tarih aralığındaki izin kayıtları silinsin mi?');">
      <input type="hidden" name="employee_id" value="{{ selected_emp.id }}">
      <input type="hidden" name="as_of" value="{{ as_of_str }}">
      <input type="hidden" name="view_start" value="{{ view_start_str }}">
      <input type="hidden" name="view_end" value="{{ view_end_str }}">
      <div><label>Toplu İzin Sil — Başlangıç</label><br><input type="date" name="del_start" required></div>
      <div><label>Bitiş</label><br><input type="date" name="del_end" required></div>
      <button type="submit" class="danger">Aralıktaki İzinleri Sil</button>
    </form>

    <table>
      <thead>
        <tr>
          <th>Tarih</th>
          <th>Gün</th>
          <th>Durum / Açıklama</th>
          <th>O Güne Kadar Hak Ediş</th>
          <th>O Güne Kadar Kullanılan</th>
          <th>Kalan İzin</th>
          <th>İşlem</th>
        </tr>
      </thead>
      <tbody>
        {% for r in ledger %}
        <tr class="{% if r.accrual_today > 0 %}badge-accrual{% elif r.is_used %}badge-used{% elif r.holiday_name %}badge-holiday{% endif %}">
          <td>{{ r.date | tr_date }}</td>
          <td>{{ r.day_name }}</td>
          <td>
            {% if r.accrual_today > 0 %}
              <span class="tag tag-green">Yıllık İzin Hak Edişi (+{{ r.accrual_today }})</span>
            {% endif %}
            {% if r.is_used %}
              <span class="tag tag-red">İzin Kullanıldı (-1)</span>
              {% if r.leave_note %}<span class="note-text">({{ r.leave_note }})</span>{% endif %}
            {% endif %}
            {% if r.holiday_name %}
              <span class="tag tag-blue">Resmi Tatil: {{ r.holiday_name }}</span>
            {% elif r.is_sunday and not r.is_used and r.accrual_today == 0 %}
              <span class="tag tag-gray">Hafta Tatili (Pazar)</span>
            {% endif %}
            {% if r.accrual_today == 0 and not r.is_used and not r.holiday_name and not r.is_sunday %}
              -
            {% endif %}
          </td>
          <td>{{ r.earned }}</td>
          <td>{{ r.used_so_far }}</td>
          <td><strong>{{ r.balance }}</strong></td>
          <td>
            {% if r.is_used %}
              <form method="post" action="/delete-leave" style="display:inline;" onsubmit="return confirm('{{ r.date | tr_date }} tarihli izin silinsin mi?');">
                <input type="hidden" name="employee_id" value="{{ selected_emp.id }}">
                <input type="hidden" name="leave_date" value="{{ r.date.isoformat() }}">
                <input type="hidden" name="as_of" value="{{ as_of_str }}">
                <input type="hidden" name="view_start" value="{{ view_start_str }}">
                <input type="hidden" name="view_end" value="{{ view_end_str }}">
                <button type="submit" class="danger small">İzni Sil</button>
              </form>
              <form method="post" action="/update-leave-note" class="inline-form">
                <input type="hidden" name="employee_id" value="{{ selected_emp.id }}">
                <input type="hidden" name="leave_date" value="{{ r.date.isoformat() }}">
                <input type="hidden" name="as_of" value="{{ as_of_str }}">
                <input type="hidden" name="view_start" value="{{ view_start_str }}">
                <input type="hidden" name="view_end" value="{{ view_end_str }}">
                <input type="text" name="note" value="{{ r.leave_note }}" placeholder="Not" style="width:110px; padding:3px 6px; font-size:12px;">
                <button type="submit" class="secondary small">Notu Kaydet</button>
              </form>
            {% else %}
              -
            {% endif %}
          </td>
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
            cur.execute("SELECT * FROM employees WHERE is_active = TRUE;")
            employees_raw = cur.fetchall()

            cur.execute("SELECT employee_id, leave_date, note FROM leave_usages;")
            usages_raw = cur.fetchall()

    employees = sorted(employees_raw, key=lambda e: tr_sort_key(e["full_name"]))

    usage_map: dict[int, dict[date, str]] = {e["id"]: {} for e in employees}
    for u in usages_raw:
        if u["employee_id"] in usage_map:
            usage_map[u["employee_id"]][u["leave_date"]] = u["note"] or ""

    summaries = []
    for emp in employees:
        s = get_employee_summary(emp, usage_map.get(emp["id"], {}), as_of)
        summaries.append({"emp": emp, **s})

    emp_id_arg = request.args.get("emp_id")
    edit_emp = request.args.get("edit_emp") == "1"
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
                ledger = get_daily_ledger(selected_emp, usage_map.get(emp["id"], {}), v_start, v_end)
                break

    message = request.args.get("msg")
    return render_template_string(
        DASHBOARD_HTML,
        employees=employees,
        summaries=summaries,
        as_of=as_of,
        as_of_str=as_of_str,
        selected_emp=selected_emp,
        edit_emp=edit_emp,
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


@app.route("/edit-employee/<int:employee_id>", methods=["POST"])
@login_required
def edit_employee(employee_id):
    init_db()
    full_name = request.form["full_name"].strip()
    hire_date = request.form["hire_date"]
    carried_over_days = int(request.form.get("carried_over_days") or 0)
    as_of_str = request.form.get("as_of") or date.today().isoformat()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE employees
                SET full_name = %s, hire_date = %s, carried_over_days = %s
                WHERE id = %s;
                """,
                (full_name, hire_date, carried_over_days, employee_id),
            )
        conn.commit()
    return redirect(url_for("index", emp_id=employee_id, as_of=as_of_str, msg="Personel bilgileri güncellendi."))


@app.route("/delete-employee/<int:employee_id>", methods=["POST"])
@login_required
def delete_employee(employee_id):
    init_db()
    as_of_str = request.form.get("as_of") or date.today().isoformat()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM employees WHERE id = %s;", (employee_id,))
        conn.commit()
    return redirect(url_for("index", as_of=as_of_str, msg="Personel ve ilgili izin kayıtları silindi."))


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

    years = list(range(start_date.year, end_date.year + 1))
    tr_holidays = get_tr_holidays(years)

    cur_date = start_date
    added_count = 0
    skipped_holidays = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            while cur_date <= end_date:
                if cur_date in tr_holidays:
                    skipped_holidays += 1
                    cur_date += timedelta(days=1)
                    continue
                if skip_sunday and cur_date.weekday() == 6:
                    cur_date += timedelta(days=1)
                    continue

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

    msg = f"{added_count} gün izin işlendi."
    if skipped_holidays > 0:
        msg += f" ({skipped_holidays} gün resmi tatil olduğu için düşülmedi.)"
    return redirect(url_for("index", emp_id=employee_id, msg=msg))


@app.route("/delete-leave", methods=["POST"])
@login_required
def delete_leave():
    init_db()
    employee_id = int(request.form["employee_id"])
    leave_date = request.form["leave_date"]
    as_of_str = request.form.get("as_of") or date.today().isoformat()
    view_start = request.form.get("view_start", "")
    view_end = request.form.get("view_end", "")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM leave_usages WHERE employee_id = %s AND leave_date = %s;",
                (employee_id, leave_date),
            )
        conn.commit()

    formatted_d = datetime.strptime(leave_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    return redirect(url_for(
        "index",
        emp_id=employee_id,
        as_of=as_of_str,
        view_start=view_start,
        view_end=view_end,
        msg=f"{formatted_d} tarihli izin kaydı silindi.",
    ))


@app.route("/delete-leave-range", methods=["POST"])
@login_required
def delete_leave_range():
    init_db()
    employee_id = int(request.form["employee_id"])
    del_start = request.form["del_start"]
    del_end = request.form["del_end"]
    as_of_str = request.form.get("as_of") or date.today().isoformat()
    view_start = request.form.get("view_start", "")
    view_end = request.form.get("view_end", "")

    if del_end < del_start:
        return redirect(url_for(
            "index",
            emp_id=employee_id,
            as_of=as_of_str,
            view_start=view_start,
            view_end=view_end,
            msg="Silme bitiş tarihi başlangıçtan küçük olamaz.",
        ))

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM leave_usages
                WHERE employee_id = %s AND leave_date >= %s AND leave_date <= %s;
                """,
                (employee_id, del_start, del_end),
            )
            deleted_count = cur.rowcount
        conn.commit()

    return redirect(url_for(
        "index",
        emp_id=employee_id,
        as_of=as_of_str,
        view_start=view_start,
        view_end=view_end,
        msg=f"{deleted_count} adet izin kaydı silindi.",
    ))


@app.route("/update-leave-note", methods=["POST"])
@login_required
def update_leave_note():
    init_db()
    employee_id = int(request.form["employee_id"])
    leave_date = request.form["leave_date"]
    note = request.form.get("note", "").strip()
    as_of_str = request.form.get("as_of") or date.today().isoformat()
    view_start = request.form.get("view_start", "")
    view_end = request.form.get("view_end", "")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leave_usages
                SET note = %s
                WHERE employee_id = %s AND leave_date = %s;
                """,
                (note, employee_id, leave_date),
            )
        conn.commit()

    return redirect(url_for(
        "index",
        emp_id=employee_id,
        as_of=as_of_str,
        view_start=view_start,
        view_end=view_end,
        msg="İzin notu güncellendi.",
    ))
