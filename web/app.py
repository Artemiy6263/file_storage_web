# web/app.py - минимальный веб-интерфейс: показывает результаты сканера из PostgreSQL.
# Только читает таблицы scans / paths / files, ничего не пишет.
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, abort, render_template, request

# Подключение к БД - то же, что в main.py (можно переопределить переменной окружения DB_DSN)
DB_DSN = os.environ.get(
    "DB_DSN", "host=localhost port=5432 dbname=file_storage user=postgres password=123"
)
# Если новых сканов нет дольше стольки часов - на главной покажем красное предупреждение
STALE_HOURS = float(os.environ.get("STALE_HOURS", "26"))

app = Flask(__name__)


def query(sql, params=()):
    """Выполняет SELECT и возвращает список строк (каждая строка - словарь)."""
    conn = psycopg2.connect(DB_DSN, connect_timeout=5)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@app.template_filter("dt")
def format_dt(value):
    """Дата в местном времени: 26.10.2026 14:47"""
    return value.astimezone().strftime("%d.%m.%Y %H:%M") if value else "—"


@app.template_filter("size")
def format_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


@app.route("/")
def index():
    """Главная: когда был последний скан + история запусков."""
    scans = query("SELECT * FROM scans ORDER BY id DESC LIMIT 30")
    hours_ago = None
    if scans:
        last = scans[0]["finished_at"] or scans[0]["started_at"]
        hours_ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return render_template("index.html", scans=scans, hours_ago=hours_ago, stale_hours=STALE_HOURS)


@app.route("/scans/<int:scan_id>")
def scan_page(scan_id):
    """Один скан: какие файлы в нём новые / изменённые / удалённые."""
    rows = query("SELECT * FROM scans WHERE id = %s", (scan_id,))
    if not rows:
        abort(404)
    status = request.args.get("status", "new")
    files = query(
        """
        SELECT f.id, f.name, f.size_bytes, f.modified_at, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE f.last_scan_id = %s AND f.status = %s
         ORDER BY p.full_path, f.name LIMIT 500
        """,
        (scan_id, status),
    )
    return render_template("scan.html", scan=rows[0], status=status, files=files)


@app.route("/files/<int:file_id>")
def file_page(file_id):
    """Карточка файла: свойства и извлечённый текст."""
    rows = query(
        """
        SELECT f.*, p.full_path FROM files f JOIN paths p ON p.id = f.path_id WHERE f.id = %s
        """,
        (file_id,),
    )
    if not rows:
        abort(404)
    return render_template("file.html", f=rows[0])


if __name__ == "__main__":
    # 0.0.0.0 = принимать подключения и с других компьютеров сети (не только с этого)
    app.run(host=os.environ.get("WEB_HOST", "0.0.0.0"), port=int(os.environ.get("WEB_PORT", "8000")))
