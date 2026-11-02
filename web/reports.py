# web/reports.py - отчёты по данным сканера.
# Каждый отчёт - функция build_*(q, root, o): делает SELECT-ы и возвращает словарь
#   {"kpis": [...], "notes": [...], "sections": [...]}
# а шаблон report.html рисует его одинаково для всех отчётов (и CSV-выгрузка тоже работает для всех).
# Только читает БД. Параметры:
#   q    - функция query(sql, params) из app.py
#   root - папка скана для фильтра (строка) или None = все папки
#   o    - выбранные настройки отчёта (например, {"days": 365})
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone

MAX_CONTENT_SIZE = 5 * 1024 * 1024  # как в main.py: файлы больше этого размера сканер не читает
STALE_HOURS = float(os.environ.get("STALE_HOURS", "26"))  # как на главной странице

LIVE = "f.status <> 'deleted'"  # файл не помечен удалённым
NO_TEXT = "COALESCE(f.content, '') = ''"  # текст из файла не извлечён
EXT = "COALESCE(NULLIF(f.extension, ''), '(без расширения)')"  # расширение для группировки


# ---------------------------------------------------------------- форматирование
def num(x) -> int:
    """SUM() в PostgreSQL отдаёт Decimal или None - приводим к int."""
    return int(x or 0)


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_num(n) -> str:
    return f"{num(n):,}".replace(",", " ")


def pct(part, whole) -> float:
    return round(float(part) * 100 / float(whole), 1) if whole else 0.0


def full_file_path(folder: str, name: str) -> str:
    sep = "\\" if "\\" in folder else "/"
    return folder + ("" if folder.endswith(sep) else sep) + name


def fmt_dt(value) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M") if value else "—"


def fmt_age(value) -> str:
    """Сколько прошло с даты: «3 г. 2 мес.» / «45 дн.»."""
    if not value:
        return "—"
    days = (datetime.now(timezone.utc) - value).days
    if days >= 365:
        return f"{days // 365} г. {(days % 365) // 30} мес."
    if days >= 60:
        return f"{days // 30} мес."
    return f"{max(days, 0)} дн."


# ---------------------------------------------------------------- запросы с фильтром по папке
def scoped(q, sql: str, params: tuple, root):
    """
    Выполняет запрос. В sql на место {scope} подставляется условие «файл лежит в папке root
    или в любой её подпапке» (если root не выбран - пусто). Подпапки находятся рекурсивно по paths.parent_id.
    В запросе таблица files должна называться f.
    """
    if root:
        sql = (
            "WITH RECURSIVE sub AS ("
            " SELECT id FROM paths WHERE full_path = %s"
            " UNION ALL SELECT p.id FROM paths p JOIN sub s ON p.parent_id = s.id) "
            + sql.replace("{scope}", "AND f.path_id IN (SELECT id FROM sub)")
        )
        params = (root, *params)
    else:
        sql = sql.replace("{scope}", "")
    return q(sql, params)


def scan_roots(q) -> list[str]:
    """Папки, которые когда-либо сканировались (свежие сверху) - для выпадающего списка."""
    rows = q("SELECT root_path FROM scans GROUP BY root_path ORDER BY max(id) DESC")
    return [r["root_path"] for r in rows]


def totals(q, root) -> dict:
    """Общие итоги по живым файлам - нужны для долей (%)."""
    r = scoped(q, f"SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size "
                  f"FROM files f WHERE {LIVE} {{scope}}", (), root)[0]
    return {"files": num(r["files"]), "size": num(r["size"])}


def dup_totals(q, root) -> dict:
    r = scoped(q, f"""
        SELECT COALESCE(sum(w), 0) AS wasted, COALESCE(sum(c), 0) AS files, count(*) AS groups
          FROM (SELECT (count(*) - 1) * max(f.size_bytes) AS w, count(*) AS c
                  FROM files f
                 WHERE {LIVE} AND f.size_bytes > 0 AND f.md5_hash IS NOT NULL {{scope}}
                 GROUP BY f.md5_hash HAVING count(*) > 1) t
    """, (), root)[0]
    return {"wasted": num(r["wasted"]), "files": num(r["files"]), "groups": num(r["groups"])}


def kpi(label, value, hint=""):
    return {"label": label, "value": value, "hint": hint}


def note(level, text, link=None):
    return {"level": level, "text": text, "link": link}  # level: ok / warn / info


def section(title, columns, rows, note_text="", empty="Нет данных"):
    return {"title": title, "columns": columns, "rows": rows, "note": note_text, "empty": empty}


# ---------------------------------------------------------------- 1. Сводка
def build_summary(q, root, o):
    b = scoped(q, f"""
        SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size,
               count(DISTINCT f.path_id) AS folders,
               count(*) FILTER (WHERE {NO_TEXT}) AS no_text,
               count(*) FILTER (WHERE f.size_bytes = 0) AS empty
          FROM files f WHERE {LIVE} {{scope}}""", (), root)[0]
    files, size = num(b["files"]), num(b["size"])
    dup = dup_totals(q, root)
    old = scoped(q, f"""
        SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size
          FROM files f WHERE {LIVE} AND f.modified_at < now() - interval '365 days' {{scope}}""", (), root)[0]
    deleted = scoped(q, "SELECT count(*) AS n FROM files f WHERE f.status = 'deleted' {scope}", (), root)[0]["n"]
    if root:
        last = q("SELECT * FROM scans WHERE root_path = %s ORDER BY id DESC LIMIT 1", (root,))
        scans_n = q("SELECT count(*) AS n FROM scans WHERE root_path = %s", (root,))[0]["n"]
    else:
        last = q("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        scans_n = q("SELECT count(*) AS n FROM scans")[0]["n"]
    last = last[0] if last else None

    kpis = [
        kpi("Файлов", human_num(files)),
        kpi("Общий размер", human_size(size)),
        kpi("Папок с файлами", human_num(b["folders"])),
        kpi("Сканирований", human_num(scans_n), f"последнее: {fmt_dt(last['started_at'])}" if last else ""),
        kpi("Лишнее в дубликатах", human_size(dup["wasted"]), f"{pct(dup['wasted'], size)}% от объёма"),
        kpi("Без текста", human_num(b["no_text"]), f"{pct(b['no_text'], files)}% файлов"),
    ]

    notes = []
    if not files:
        notes.append(note("warn", "В базе нет файлов — запустите сканирование на вкладке «Запуск скана»."))
    else:
        if last:
            hours = (datetime.now(timezone.utc) - (last["finished_at"] or last["started_at"])).total_seconds() / 3600
            if hours > STALE_HOURS:
                notes.append(note("warn", f"Последний скан был {int(hours)} ч назад — данные могли устареть."))
        if size and dup["wasted"] / size >= 0.05:
            notes.append(note("warn", f"Дубликаты занимают {human_size(dup['wasted'])} ({pct(dup['wasted'], size)}% объёма) — их можно удалить.", "duplicates"))
        if files and num(b["no_text"]) / files >= 0.2:
            notes.append(note("info", f"У {pct(b['no_text'], files)}% файлов не извлечён текст — по содержимому они не ищутся.", "unindexed"))
        if size and num(old["size"]) / size >= 0.3:
            notes.append(note("info", f"{pct(old['size'], size)}% объёма ({human_size(old['size'])}) — файлы, которые не менялись больше года.", "stale"))
        if num(b["empty"]):
            notes.append(note("info", f"Найдено пустых файлов (0 байт): {human_num(b['empty'])}.", "unindexed"))
        if num(deleted):
            notes.append(note("info", f"Файлов, помеченных удалёнными: {human_num(deleted)}.", "changes"))
        if not notes:
            notes.append(note("ok", "Заметных проблем не найдено."))

    by_status = scoped(q, """
        SELECT f.status, count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size
          FROM files f WHERE true {scope} GROUP BY f.status ORDER BY count(*) DESC""", (), root)
    rows = [{"status": r["status"], "files": num(r["files"]), "size": num(r["size"])} for r in by_status]
    secs = [section("Файлы по статусам", [("status", "Статус", "status"), ("files", "Файлов", "int"), ("size", "Размер", "size")],
                    rows, "Статус — результат последнего скана, в котором файл встретился (удалённые хранятся с пометкой).")]
    return {"kpis": kpis, "notes": notes, "sections": secs}


# ---------------------------------------------------------------- 2. Дубликаты
def build_duplicates(q, root, o):
    limit = o["limit"]
    groups = scoped(q, f"""
        SELECT f.md5_hash, count(*) AS copies, max(f.size_bytes) AS size,
               (count(*) - 1) * max(f.size_bytes) AS wasted
          FROM files f
         WHERE {LIVE} AND f.size_bytes > 0 AND f.md5_hash IS NOT NULL {{scope}}
         GROUP BY f.md5_hash HAVING count(*) > 1
         ORDER BY wasted DESC, f.md5_hash LIMIT %s""", (limit,), root)
    members = {}
    if groups:
        for m in scoped(q, f"""
            SELECT f.id, f.name, f.size_bytes, f.modified_at, f.md5_hash, p.full_path
              FROM files f JOIN paths p ON p.id = f.path_id
             WHERE {LIVE} AND f.md5_hash = ANY(%s) {{scope}}
             ORDER BY f.md5_hash, p.full_path, f.name""", ([g["md5_hash"] for g in groups],), root):
            members.setdefault(m["md5_hash"], []).append(m)
    rows = []
    for i, g in enumerate(groups, 1):
        for j, m in enumerate(members.get(g["md5_hash"], [])):
            rows.append({
                "group": i, "first": j == 0,
                "no": f"#{i}" if j == 0 else "",
                "copies": num(g["copies"]) if j == 0 else None,
                "wasted": num(g["wasted"]) if j == 0 else None,
                "id": m["id"], "name": m["name"], "full_path": m["full_path"],
                "size_bytes": num(m["size_bytes"]), "modified_at": m["modified_at"],
                "md5": m["md5_hash"] if j == 0 else "",
            })
    t = dup_totals(q, root)
    tot = totals(q, root)
    kpis = [kpi("Групп дубликатов", human_num(t["groups"])),
            kpi("Файлов в дубликатах", human_num(t["files"])),
            kpi("Можно освободить", human_size(t["wasted"]), f"{pct(t['wasted'], tot['size'])}% общего объёма")]
    notes = []
    if t["groups"] > len(groups):
        notes.append(note("info", f"Показаны {len(groups)} самых «тяжёлых» групп из {human_num(t['groups'])}. Увеличьте число групп в настройках отчёта."))
    cols = [("no", "Группа", "text"), ("copies", "Копий", "int"), ("wasted", "Лишнее место", "size"),
            ("file", "Файл", "file"), ("size_bytes", "Размер", "size"), ("modified_at", "Изменён", "date"),
            ("md5", "MD5", "mono")]
    return {"kpis": kpis, "notes": notes, "sections": [section(
        "Группы одинаковых файлов", cols, rows,
        "Группы отсортированы по лишнему месту: размер × (копий − 1). Оставьте одну копию, остальные можно удалить.",
        "Дубликатов не найдено.")]}


# ---------------------------------------------------------------- 3. Самые большие файлы
def build_largest(q, root, o):
    n = o["n"]
    rows = scoped(q, f"""
        SELECT f.id, f.name, f.extension, f.size_bytes, f.modified_at, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE {LIVE} {{scope}} ORDER BY f.size_bytes DESC, f.id LIMIT %s""", (n,), root)
    tot = totals(q, root)
    top = sum(num(r["size_bytes"]) for r in rows)
    out = [{**r, "size_bytes": num(r["size_bytes"]), "share": pct(r["size_bytes"], tot["size"]),
            "ext": r["extension"] or "—"} for r in rows]
    kpis = [kpi("Показано файлов", human_num(len(out))),
            kpi("Занимают", human_size(top), f"{pct(top, tot['size'])}% общего объёма"),
            kpi("Всего в хранилище", human_size(tot["size"]), f"{human_num(tot['files'])} файлов")]
    cols = [("file", "Файл", "file"), ("ext", "Тип", "text"), ("size_bytes", "Размер", "size"),
            ("share", "Доля от всего объёма", "pct"), ("modified_at", "Изменён", "date")]
    return {"kpis": kpis, "notes": [], "sections": [section(f"Топ-{n} файлов по размеру", cols, out, empty="Файлов нет.")]}


# ---------------------------------------------------------------- 4. По типам файлов
def build_types(q, root, o):
    rows = scoped(q, f"""
        SELECT {EXT} AS ext, count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size,
               COALESCE(avg(f.size_bytes), 0)::bigint AS avg_size
          FROM files f WHERE {LIVE} {{scope}} GROUP BY 1 ORDER BY sum(f.size_bytes) DESC NULLS LAST, 1 LIMIT 60""", (), root)
    tot = totals(q, root)
    kinds = scoped(q, f"SELECT count(DISTINCT {EXT}) AS n FROM files f WHERE {LIVE} {{scope}}", (), root)[0]["n"]
    out = [{"ext": r["ext"], "files": num(r["files"]), "files_pct": pct(r["files"], tot["files"]),
            "size": num(r["size"]), "size_pct": pct(r["size"], tot["size"]), "avg": num(r["avg_size"])} for r in rows]
    cols = [("ext", "Тип", "mono"), ("files", "Файлов", "int"), ("files_pct", "Доля файлов", "pct"),
            ("size", "Размер", "size"), ("size_pct", "Доля объёма", "pct"), ("avg", "Средний размер", "size")]
    notes = []
    if num(kinds) > len(out):
        notes.append(note("info", f"Показаны 60 самых «тяжёлых» типов из {human_num(kinds)}."))
    kpis = [kpi("Разных типов", human_num(kinds)), kpi("Файлов", human_num(tot["files"])), kpi("Размер", human_size(tot["size"]))]
    return {"kpis": kpis, "notes": notes, "sections": [section("Типы файлов (по убыванию занятого места)", cols, out, empty="Файлов нет.")]}


# ---------------------------------------------------------------- 5. По папкам
def build_folders(q, root, o):
    if not root:  # без выбранной папки берём последнюю сканированную
        r = q("SELECT root_path FROM scans ORDER BY id DESC LIMIT 1")
        root = r[0]["root_path"] if r else None
    if not root:
        return {"kpis": [], "notes": [note("warn", "Сканирований ещё не было.")], "sections": []}
    rows = q("""
        WITH RECURSIVE tree AS (
            SELECT p.id AS top_id, p.id, p.name, p.full_path AS top_path
              FROM paths p WHERE p.parent_id = (SELECT id FROM paths WHERE full_path = %s)
            UNION ALL
            SELECT t.top_id, p.id, t.name, t.top_path FROM paths p JOIN tree t ON p.parent_id = t.id)
        SELECT t.name, t.top_path AS full_path, count(f.id) AS files,
               COALESCE(sum(f.size_bytes), 0) AS size, max(f.modified_at) AS last_modified
          FROM tree t JOIN files f ON f.path_id = t.id AND f.status <> 'deleted'
         GROUP BY t.top_id, t.name, t.top_path ORDER BY size DESC LIMIT 100""", (root,))
    direct = q("""
        SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size, max(f.modified_at) AS last_modified
          FROM files f WHERE f.status <> 'deleted' AND f.path_id = (SELECT id FROM paths WHERE full_path = %s)""", (root,))[0]
    out = [{"name": r["name"], "full_path": r["full_path"], "files": num(r["files"]), "size": num(r["size"]),
            "last_modified": r["last_modified"]} for r in rows]
    if num(direct["files"]):
        out.append({"name": "(файлы прямо в этой папке)", "full_path": root, "files": num(direct["files"]),
                    "size": num(direct["size"]), "last_modified": direct["last_modified"]})
    out.sort(key=lambda r: r["size"], reverse=True)
    total = sum(r["size"] for r in out)
    for r in out:
        r["share"] = pct(r["size"], total)
    cols = [("folder", "Папка", "folder"), ("files", "Файлов", "int"), ("size", "Размер", "size"),
            ("share", "Доля", "pct"), ("last_modified", "Последнее изменение", "date")]
    kpis = [kpi("Папка", root), kpi("Подпапок первого уровня", human_num(len(rows))), kpi("Размер", human_size(total))]
    return {"kpis": kpis, "notes": [], "sections": [section(
        "Размер папок первого уровня", cols, out,
        "Размер считается вместе со всеми вложенными папками.", "В выбранной папке нет файлов.")]}


# ---------------------------------------------------------------- 6. Изменения
def build_changes(q, root, o):
    days = o["days"]
    sc = q("SELECT * FROM scans WHERE root_path = %s ORDER BY id DESC LIMIT 30", (root,)) if root \
        else q("SELECT * FROM scans ORDER BY id DESC LIMIT 30")
    scan_rows = [{"id": s["id"], "started_at": s["started_at"], "root_path": s["root_path"], "total": s["files_total"],
                  "new": s["files_new"], "modified": s["files_modified"], "deleted": s["files_deleted"]} for s in sc]

    changed = scoped(q, f"""
        SELECT f.id, f.name, f.size_bytes, f.modified_at, f.change_count, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE {LIVE} AND f.modified_at >= now() - make_interval(days => %s) {{scope}}
         ORDER BY f.modified_at DESC LIMIT 200""", (days,), root)
    changed_n = scoped(q, f"SELECT count(*) AS n FROM files f WHERE {LIVE} AND f.modified_at >= now() - make_interval(days => %s) {{scope}}",
                       (days,), root)[0]["n"]
    hot = scoped(q, f"""
        SELECT f.id, f.name, f.size_bytes, f.modified_at, f.change_count, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE {LIVE} AND f.change_count > 0 {{scope}}
         ORDER BY f.change_count DESC, f.modified_at DESC LIMIT 20""", (), root)
    gone = scoped(q, """
        SELECT f.id, f.name, f.size_bytes, f.scanned_at, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE f.status = 'deleted' {scope} ORDER BY f.scanned_at DESC LIMIT 100""", (), root)
    gone_n = scoped(q, "SELECT count(*) AS n FROM files f WHERE f.status = 'deleted' {scope}", (), root)[0]["n"]

    fix = lambda rows: [{**r, "size_bytes": num(r["size_bytes"])} for r in rows]
    notes = []
    if num(changed_n) > 200:
        notes.append(note("info", f"За период изменено {human_num(changed_n)} файлов, показаны 200 последних."))
    kpis = [kpi(f"Изменено за {days} дн.", human_num(changed_n)), kpi("Помечено удалёнными", human_num(gone_n)),
            kpi("Сканирований в списке", human_num(len(scan_rows)))]
    secs = [
        section("Динамика по сканированиям", [("scan", "Скан", "scan"), ("started_at", "Дата", "date"), ("root_path", "Папка", "text"),
                                              ("total", "Всего", "int"), ("new", "Новых", "new"), ("modified", "Изменено", "mod"),
                                              ("deleted", "Удалено", "del")],
                scan_rows, "Числа точные для каждого скана. Резкий скачок «Удалено» или «Изменено» — повод разобраться.", "Сканирований ещё не было."),
        section(f"Файлы, изменённые за последние {days} дн.",
                [("file", "Файл", "file"), ("size_bytes", "Размер", "size"), ("modified_at", "Изменён", "date"), ("change_count", "Правок", "int")],
                fix(changed), "Считается по дате изменения файла на диске.", "За выбранный период изменений нет."),
        section("Чаще всего меняются", [("file", "Файл", "file"), ("change_count", "Правок", "int"), ("modified_at", "Последняя правка", "date"),
                                        ("size_bytes", "Размер", "size")],
                fix(hot), "«Правок» — сколько раз сканер видел новую дату изменения файла.", "Файлы пока не менялись."),
        section("Удалённые файлы", [("file", "Файл", "file"), ("size_bytes", "Размер", "size"), ("scanned_at", "Обнаружено удаление", "date")],
                fix(gone), "Сканер не стирает записи об удалённых файлах, а помечает их — так можно понять, что пропало и когда.", "Удалённых файлов нет."),
    ]
    return {"kpis": kpis, "notes": notes, "sections": secs}


# ---------------------------------------------------------------- 7. Устаревшие файлы
def build_stale(q, root, o):
    days = o["days"]
    rows = scoped(q, f"""
        SELECT f.id, f.name, f.size_bytes, f.modified_at, p.full_path
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE {LIVE} AND f.modified_at < now() - make_interval(days => %s) {{scope}}
         ORDER BY f.size_bytes DESC, f.id LIMIT 200""", (days,), root)
    s = scoped(q, f"""
        SELECT count(*) AS files, COALESCE(sum(f.size_bytes), 0) AS size
          FROM files f WHERE {LIVE} AND f.modified_at < now() - make_interval(days => %s) {{scope}}""", (days,), root)[0]
    tot = totals(q, root)
    out = [{**r, "size_bytes": num(r["size_bytes"]), "age": fmt_age(r["modified_at"])} for r in rows]
    notes = []
    if num(s["files"]) > len(out):
        notes.append(note("info", f"Показаны 200 самых больших из {human_num(s['files'])} файлов."))
    kpis = [kpi("Устаревших файлов", human_num(s["files"]), f"{pct(s['files'], tot['files'])}% файлов"),
            kpi("Занимают", human_size(s["size"]), f"{pct(s['size'], tot['size'])}% объёма")]
    cols = [("file", "Файл", "file"), ("size_bytes", "Размер", "size"), ("modified_at", "Последнее изменение", "date"), ("age", "Не менялся", "text")]
    return {"kpis": kpis, "notes": notes, "sections": [section(
        "Давно не менявшиеся файлы (самые большие сверху)", cols, out,
        "Дата — это дата изменения файла на диске, не дата последнего открытия.", "Таких файлов нет.")]}


# ---------------------------------------------------------------- 8. Файлы без текста
def build_unindexed(q, root, o):
    b = scoped(q, f"""
        SELECT count(*) FILTER (WHERE {NO_TEXT}) AS no_text,
               count(*) FILTER (WHERE {NO_TEXT} AND f.size_bytes = 0) AS empty,
               count(*) FILTER (WHERE {NO_TEXT} AND f.size_bytes > %s) AS big
          FROM files f WHERE {LIVE} {{scope}}""", (MAX_CONTENT_SIZE,), root)[0]
    tot = totals(q, root)
    by_ext = scoped(q, f"""
        SELECT {EXT} AS ext, count(*) AS files,
               count(*) FILTER (WHERE {NO_TEXT}) AS no_text,
               COALESCE(sum(f.size_bytes) FILTER (WHERE {NO_TEXT}), 0) AS size
          FROM files f WHERE {LIVE} {{scope}}
         GROUP BY 1 HAVING count(*) FILTER (WHERE {NO_TEXT}) > 0
         ORDER BY no_text DESC, 1 LIMIT 40""", (), root)
    biggest = scoped(q, f"""
        SELECT f.id, f.name, f.size_bytes, p.full_path,
               CASE WHEN f.size_bytes = 0 THEN 'пустой файл'
                    WHEN f.size_bytes > %s THEN 'больше {human_size(MAX_CONTENT_SIZE)} - текст не читается'
                    ELSE 'формат не поддерживается или файл не открылся' END AS reason
          FROM files f JOIN paths p ON p.id = f.path_id
         WHERE {LIVE} AND {NO_TEXT} {{scope}}
         ORDER BY f.size_bytes DESC, f.id LIMIT 50""", (MAX_CONTENT_SIZE,), root)
    no_text, empty, big = num(b["no_text"]), num(b["empty"]), num(b["big"])
    kpis = [kpi("Файлов без текста", human_num(no_text), f"{pct(no_text, tot['files'])}% всех файлов"),
            kpi("Пустых (0 байт)", human_num(empty)),
            kpi(f"Больше {human_size(MAX_CONTENT_SIZE)}", human_num(big), "сканер их не читает"),
            kpi("Не удалось прочитать", human_num(no_text - empty - big), "формат или ошибка")]
    ext_rows = [{"ext": r["ext"], "files": num(r["files"]), "no_text": num(r["no_text"]),
                 "share": pct(r["no_text"], r["files"]), "size": num(r["size"])} for r in by_ext]
    big_rows = [{**r, "size_bytes": num(r["size_bytes"])} for r in biggest]
    secs = [
        section("Какие типы не читаются", [("ext", "Тип", "mono"), ("files", "Всего файлов", "int"), ("no_text", "Без текста", "int"),
                                           ("share", "Доля без текста", "pct"), ("size", "Размер", "size")],
                ext_rows, "Если здесь важный формат (например, .dwg или .rtf) — поддержку можно добавить в read_text_content() в main.py.",
                "У всех файлов текст извлечён."),
        section("Самые большие файлы без текста", [("file", "Файл", "file"), ("size_bytes", "Размер", "size"), ("reason", "Причина", "text")],
                big_rows, "Такие файлы находятся поиском только по имени и пути.", "Нет таких файлов."),
    ]
    return {"kpis": kpis, "notes": [], "sections": secs}


# ---------------------------------------------------------------- реестр отчётов
def choices(*pairs):
    return [(v, l) for v, l in pairs]


REPORTS = [
    {"slug": "summary", "title": "Сводка", "builder": build_summary, "options": [],
     "lead": "Общая картина по всем файлам в базе: сколько их, сколько места занимают, есть ли дубликаты и насколько хорошо файлы «читаются» сканером.",
     "why": "Отправная точка: за минуту видно, есть ли проблемы, а подсказки ниже ведут в нужный отчёт."},
    {"slug": "duplicates", "title": "Дубликаты", "builder": build_duplicates,
     "options": [{"name": "limit", "label": "Групп в отчёте", "default": 100, "choices": choices((50, "50"), (100, "100"), (300, "300"))}],
     "lead": "Файлы с одинаковым MD5 — то есть с одинаковым содержимым, но лежащие в разных местах или под разными именами.",
     "why": "Самый быстрый способ освободить место: лишние копии можно удалить. Перед удалением решите, какая копия «главная»."},
    {"slug": "largest", "title": "Крупнейшие файлы", "builder": build_largest,
     "options": [{"name": "n", "label": "Показать", "default": 50, "choices": choices((50, "топ-50"), (100, "топ-100"), (300, "топ-300"))}],
     "lead": "Самые большие файлы и их доля от общего объёма хранилища.",
     "why": "Точечная очистка: обычно небольшое число файлов занимает основную часть диска — их и стоит проверить первыми."},
    {"slug": "types", "title": "По типам файлов", "builder": build_types, "options": [],
     "lead": "Сколько файлов и места занимает каждый тип (по расширению).",
     "why": "Показывает, из чего состоит хранилище: видео, архивы, документы. Помогает планировать диск и правила хранения; странные типы (.tmp, .bak, .exe) — повод разобраться."},
    {"slug": "folders", "title": "По папкам", "builder": build_folders, "options": [],
     "lead": "Размер папок первого уровня внутри выбранной папки скана (с учётом вложенных).",
     "why": "Показывает, какой отдел или проект занимает больше всего места — основа для квот и решений об архивировании."},
    {"slug": "changes", "title": "Изменения", "builder": build_changes,
     "options": [{"name": "days", "label": "Период", "default": 7, "choices": choices((1, "1 день"), (7, "7 дней"), (30, "30 дней"), (90, "90 дней"))}],
     "lead": "Что происходило с файлами: динамика по сканированиям, недавно изменённые, часто меняющиеся и удалённые файлы.",
     "why": "Контроль: не пропало ли нужное, какие документы правятся постоянно, нет ли массовых удалений. В базе хранится только последнее состояние файла, поэтому «изменённые» считаются по дате файла на диске."},
    {"slug": "stale", "title": "Устаревшие файлы", "builder": build_stale,
     "options": [{"name": "days", "label": "Не менялись больше", "default": 365,
                  "choices": choices((180, "полугода"), (365, "1 года"), (730, "2 лет"), (1095, "3 лет"))}],
     "lead": "Файлы, которые не менялись дольше выбранного срока, — самые большие сверху.",
     "why": "Кандидаты на архивирование или удаление: начиная с верха списка освобождается больше всего места."},
    {"slug": "unindexed", "title": "Файлы без текста", "builder": build_unindexed, "options": [],
     "lead": "Файлы, из которых сканер не смог достать текст: пустые, слишком большие и в неподдерживаемых форматах.",
     "why": "По содержимому такие файлы не ищутся. Отчёт показывает, какие форматы стоит добавить в сканер и что не попало в поиск."},
]
BY_SLUG = {r["slug"]: r for r in REPORTS}


def build_context(slug: str, q, args) -> dict:
    """Всё, что нужно шаблону: описание отчёта, фильтры, результат builder-а."""
    rep = BY_SLUG[slug]
    roots = scan_roots(q)
    root = args.get("root") or None
    if root and root not in roots:
        root = None  # неизвестную папку игнорируем
    selected = {}
    for opt in rep["options"]:  # значения настроек берём из адреса, но только допустимые
        allowed = [v for v, _ in opt["choices"]]
        try:
            val = int(args.get(opt["name"], opt["default"]))
        except (TypeError, ValueError):
            val = opt["default"]
        selected[opt["name"]] = val if val in allowed else opt["default"]
    result = rep["builder"](q, root, selected)
    return {"rep": rep, "roots": roots, "root": root, "selected": selected, "result": result}


def section_to_csv(sec: dict) -> bytes:
    """CSV для Excel: разделитель «;», UTF-8 с BOM (иначе кириллица в Excel «поедет»)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([label for _, label, _ in sec["columns"]])
    for r in sec["rows"]:
        line = []
        for key, _, kind in sec["columns"]:
            if kind == "file":
                v = full_file_path(r["full_path"], r["name"])
            elif kind == "folder":
                v = r["full_path"]
            elif kind == "scan":
                v = r["id"]
            elif kind == "date":
                v = fmt_dt(r.get(key)) if r.get(key) else ""
            else:
                v = r.get(key)
            line.append("" if v is None else v)
        w.writerow(line)
    return ("﻿" + buf.getvalue()).encode("utf-8")
