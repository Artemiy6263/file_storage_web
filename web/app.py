# web/app.py - веб-интерфейс сканера файлов.
# Читает результаты из PostgreSQL (scans / paths / files), показывает отчёты и умеет запускать
# сканер (main.py) для выбранной в браузере папки.
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import reports

# Подключение к БД - то же, что в main.py (можно переопределить переменной окружения DB_DSN)
DB_DSN = os.environ.get(
    "DB_DSN", "host=localhost port=5432 dbname=file_storage user=postgres password=123"
)
# Если новых сканов нет дольше стольки часов - на главной покажем красное предупреждение
STALE_HOURS = float(os.environ.get("STALE_HOURS", "26"))

# --- запуск сканера из браузера ---
SCANNER_DIR = Path(os.environ.get("SCANNER_DIR") or Path(__file__).resolve().parent.parent)  # папка, где лежит main.py
SCANNER_SCRIPT = SCANNER_DIR / "main.py"
SCANNER_PYTHON = os.environ.get("SCANNER_PYTHON") or sys.executable  # каким Python запускать main.py (нужны его библиотеки)
# Сканер пишет в БД, а сайт может подключаться «только на чтение» (пользователь viewer) - поэтому
# для сканера можно задать отдельное подключение. Если не задано - берём DB_DSN.
SCANNER_DSN = os.environ.get("SCANNER_DB_DSN") or DB_DSN
# Ограничение: какие папки можно выбирать. Через «;» в Windows и «:» в Linux. Пусто = любые папки на компьютере.
ALLOWED_ROOTS = [os.path.normcase(os.path.realpath(p)) for p in
                 (os.environ.get("SCAN_ALLOWED_ROOTS") or "").split(os.pathsep) if p.strip()]

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


# ---------------------------------------------------------------- фильтры шаблонов
@app.template_filter("dt")
def format_dt(value):
    """Дата в местном времени: 26.10.2026 14:47"""
    return reports.fmt_dt(value)


@app.template_filter("size")
def format_size(n):
    return reports.human_size(n)


@app.template_filter("num")
def format_num(n):
    return reports.human_num(n)


STATUS_RU = {"new": "Новый", "unchanged": "Без изменений", "modified": "Изменён", "deleted": "Удалён"}


@app.template_filter("status_ru")
def status_ru(s):
    return STATUS_RU.get(s, s)


app.jinja_env.globals["full_file_path"] = reports.full_file_path


@app.errorhandler(psycopg2.Error)
def db_error(e):
    return render_template(
        "error.html", title="Нет связи с базой данных",
        text="Проверьте, что PostgreSQL запущен и что DB_DSN указывает на нужную базу.", details=str(e)), 500


# ---------------------------------------------------------------- страницы: сканирования и файлы
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


FILES_PER_PAGE = 100  # сколько файлов на одной странице поиска


@app.route("/files")
def files():
    """Поиск файлов по имени/пути и по тексту внутри файла (постранично)."""
    name = request.args.get("name", "").strip()
    text = request.args.get("text", "").strip()
    where = " WHERE f.status <> 'deleted'"
    params = []
    if name:
        where += " AND (f.name ILIKE %s OR p.full_path ILIKE %s)"
        params += [f"%{name}%"] * 2
    if text:
        where += " AND f.content ILIKE %s"
        params.append(f"%{text}%")
    src = " FROM files f JOIN paths p ON p.id = f.path_id" + where
    total = query("SELECT count(*) AS n" + src, tuple(params))[0]["n"]
    pages = max(1, -(-total // FILES_PER_PAGE))  # округление вверх
    try:
        page = min(max(int(request.args.get("page", 1)), 1), pages)
    except ValueError:
        page = 1
    rows = query(
        "SELECT f.id, f.name, f.size_bytes, f.modified_at, f.status, p.full_path" + src +
        " ORDER BY f.modified_at DESC, f.id LIMIT %s OFFSET %s",
        tuple(params) + (FILES_PER_PAGE, (page - 1) * FILES_PER_PAGE))
    return render_template("files.html", files=rows, name=name, text=text, total=total, page=page, pages=pages)


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


# ---------------------------------------------------------------- отчёты
@app.route("/reports")
def reports_index():
    return redirect(url_for("report_page", slug="summary"))


@app.route("/reports/<slug>")
def report_page(slug):
    if slug not in reports.BY_SLUG:
        abort(404)
    ctx = reports.build_context(slug, query, request.args)
    return render_template("report.html", reports=reports.REPORTS, slug=slug, **ctx)


@app.route("/reports/<slug>.csv")
def report_csv(slug):
    """Выгрузка одной таблицы отчёта в CSV (открывается в Excel): ?section=0,1,..."""
    if slug not in reports.BY_SLUG:
        abort(404)
    ctx = reports.build_context(slug, query, request.args)
    sections = ctx["result"]["sections"]
    try:
        sec = sections[int(request.args.get("section", 0))]
    except (ValueError, IndexError):
        abort(404)
    return Response(
        reports.section_to_csv(sec), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=report_{slug}_{request.args.get('section', 0)}.csv"})


# ---------------------------------------------------------------- выбор папки (обзор диска сервера)
# Браузер не может показать серверу путь к папке с компьютера пользователя, а сканируется папка на том
# компьютере, где работает сайт. Поэтому «Обзор» - это список папок, который отдаёт сервер.
def is_allowed(path: str) -> bool:
    if not ALLOWED_ROOTS:
        return True
    p = os.path.normcase(os.path.realpath(path))
    return any(p == r or p.startswith(r.rstrip("\\/") + os.sep) for r in ALLOWED_ROOTS)


def top_level_entries():
    """Откуда начинается обзор: разрешённые папки, диски Windows или корень Linux."""
    if ALLOWED_ROOTS:
        return [{"name": p, "path": p} for p in ALLOWED_ROOTS]
    if os.name == "nt":
        return [{"name": f"{c}:\\", "path": f"{c}:\\"} for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
    home = str(Path.home())
    return [{"name": "/ (корень)", "path": "/"}, {"name": f"Домашняя папка ({home})", "path": home}]


@app.route("/api/fs")
def api_fs():
    """Подпапки указанной папки (JSON). Пустой path - верхний уровень (диски)."""
    raw = request.args.get("path", "").strip()
    if not raw:
        return jsonify(ok=True, path="", parent=None, dirs=top_level_entries(), truncated=False)
    real = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(real):
        return jsonify(ok=False, error="Папка не найдена"), 404
    if not is_allowed(real):
        return jsonify(ok=False, error="Эта папка недоступна для сканирования (см. SCAN_ALLOWED_ROOTS)"), 403
    dirs, truncated = [], False
    try:
        with os.scandir(real) as it:
            for e in it:
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": os.path.join(real, e.name)})
                except OSError:
                    continue
                if len(dirs) >= 3000:
                    truncated = True
                    break
    except PermissionError:
        return jsonify(ok=False, error="Нет доступа к этой папке"), 403
    except OSError as e:
        return jsonify(ok=False, error=str(e)), 500
    dirs = [d for d in dirs if is_allowed(d["path"])]
    dirs.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(real)
    if parent == real or not is_allowed(parent):
        parent = ""  # выше некуда - вернёмся к списку дисков
    return jsonify(ok=True, path=real, parent=parent, dirs=dirs, truncated=truncated)


# ---------------------------------------------------------------- запуск сканирования
class ScanJob:
    """Один запуск main.py: процесс, его вывод и итог. Хранится в памяти сайта."""

    def __init__(self, path):
        self.path = path
        self.state = "running"  # running / done / failed / cancelled
        self.started = time.time()
        self.finished = None
        self.log = deque(maxlen=400)  # последние строки вывода сканера
        self.processed = 0  # сколько файлов уже обработано
        self.scan_id = None
        self.returncode = None
        self.cancelled = False
        self.proc = None


_job = None
_job_lock = threading.Lock()
_FILE_LINE = re.compile(r"^\[(НОВЫЙ|БЕЗ ИЗМЕНЕНИЙ|ИЗМЕНЁН|УДАЛЁН)\]")  # так сканер печатает каждый обработанный файл
_SCAN_ID_LINE = re.compile(r"^SCAN_ID=(\d+)")


def _watch(job):
    """Читает вывод сканера построчно, пока процесс не завершится (работает в отдельном потоке)."""
    for line in job.proc.stdout:
        line = line.rstrip()
        job.log.append(line)
        if _FILE_LINE.match(line):
            job.processed += 1
        m = _SCAN_ID_LINE.match(line)
        if m:
            job.scan_id = int(m.group(1))
    job.proc.wait()
    job.returncode = job.proc.returncode
    job.finished = time.time()
    job.state = "cancelled" if job.cancelled else ("done" if job.returncode == 0 else "failed")


def start_scan(path):
    """Запускает main.py <path>. Возвращает (job, ошибка)."""
    global _job
    with _job_lock:
        if _job and _job.state == "running":
            return None, "Сканирование уже идёт — дождитесь окончания или остановите его."
        if not SCANNER_SCRIPT.is_file():
            return None, f"Не найден сканер: {SCANNER_SCRIPT}. Задайте SCANNER_DIR."
        env = dict(os.environ, DB_DSN=SCANNER_DSN, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        job = ScanJob(path)
        try:
            job.proc = subprocess.Popen(
                [SCANNER_PYTHON, "-u", str(SCANNER_SCRIPT), path],
                cwd=str(SCANNER_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            return None, f"Не удалось запустить сканер: {e}"
        _job = job
        threading.Thread(target=_watch, args=(job,), daemon=True).start()
        return job, None


@app.route("/run")
def run_page():
    """Страница «Запуск скана»: выбор папки, кнопка, ход выполнения."""
    recent, db_error_text = [], None
    try:
        recent = query("""SELECT root_path, max(started_at) AS last_at, count(*) AS n
                            FROM scans GROUP BY root_path ORDER BY max(id) DESC LIMIT 8""")
    except psycopg2.Error as e:  # без БД страницу показываем, но сканировать не получится
        db_error_text = str(e)
    return render_template("run.html", recent=recent, db_error=db_error_text, allowed_roots=ALLOWED_ROOTS)


@app.route("/run/start", methods=["POST"])
def run_start():
    raw = (request.form.get("path") or "").strip()
    if not raw:
        return jsonify(ok=False, error="Укажите папку"), 400
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        return jsonify(ok=False, error=f"Папка не найдена: {path}"), 400
    if not is_allowed(path):
        return jsonify(ok=False, error="Эта папка недоступна для сканирования (см. SCAN_ALLOWED_ROOTS)"), 403
    job, err = start_scan(path)
    if err:
        return jsonify(ok=False, error=err), 409
    return jsonify(ok=True, path=path)


@app.route("/run/cancel", methods=["POST"])
def run_cancel():
    """Останавливает сканирование. Запись в БД не сохраняется (транзакция откатывается), снимок не меняется."""
    job = _job
    if job and job.state == "running" and job.proc:
        job.cancelled = True
        job.proc.terminate()
    return jsonify(ok=True)


@app.route("/run/status")
def run_status():
    job = _job
    if not job:
        return jsonify(state="idle")
    end = job.finished or time.time()
    return jsonify(
        state=job.state, path=job.path, elapsed=int(end - job.started), processed=job.processed,
        scan_id=job.scan_id, returncode=job.returncode, log="\n".join(list(job.log)[-200:]),
        scan_url=url_for("scan_page", scan_id=job.scan_id) if job.scan_id else None)


if __name__ == "__main__":
    # 0.0.0.0 = принимать подключения и с других компьютеров сети (не только с этого)
    app.run(host=os.environ.get("WEB_HOST", "0.0.0.0"), port=int(os.environ.get("WEB_PORT", "8000")))
