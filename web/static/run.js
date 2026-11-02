// web/static/run.js - страница «Запуск скана»: обзор папок сервера, запуск и ход сканирования.
(function () {
  const $ = (id) => document.getElementById(id);
  const pathInput = $("path"), picker = $("picker"), dirsEl = $("dirs"), curEl = $("cur");
  let current = "", parent = "";  // папка, которую показывает обзор, и её родитель

  function showError(text) {
    const el = $("form-error");
    el.textContent = text || "";
    el.hidden = !text;
  }

  // ---------- обзор папок ----------
  async function load(path) {
    const r = await fetch("/api/fs?path=" + encodeURIComponent(path));
    const data = await r.json();
    if (!data.ok) {
      if (path) return load("");  // путь не открылся (например, не существует) - начинаем с дисков
      showError(data.error);
      return;
    }
    showError("");
    current = data.path;
    parent = data.parent;
    curEl.textContent = current || "Компьютер";
    $("up").disabled = !current;
    $("choose").disabled = !current;
    dirsEl.innerHTML = "";
    if (!data.dirs.length) {
      dirsEl.innerHTML = '<li class="dirs-empty">Вложенных папок нет</li>';
    }
    for (const d of data.dirs) {
      const li = document.createElement("li");
      li.textContent = "📁 " + d.name;
      li.onclick = () => load(d.path);
      dirsEl.appendChild(li);
    }
    if (data.truncated) {
      dirsEl.insertAdjacentHTML("beforeend", '<li class="dirs-empty">Показаны не все папки — введите путь вручную</li>');
    }
  }

  $("browse").onclick = () => {
    picker.hidden = false;
    load(pathInput.value.trim());
  };
  $("up").onclick = () => load(parent || "");
  $("close").onclick = () => { picker.hidden = true; };
  $("choose").onclick = () => {
    pathInput.value = current;
    picker.hidden = true;
  };
  document.querySelectorAll(".chip").forEach((c) => {
    c.onclick = () => { pathInput.value = c.dataset.path; showError(""); };
  });

  // ---------- запуск ----------
  $("start").onclick = async () => {
    showError("");
    const body = new URLSearchParams({ path: pathInput.value.trim() });
    const r = await fetch("/run/start", { method: "POST", body });
    const data = await r.json();
    if (!data.ok) { showError(data.error); return; }
    pathInput.value = data.path;
    poll();
  };

  $("cancel").onclick = async () => {
    await fetch("/run/cancel", { method: "POST" });
    poll();
  };

  // ---------- ход выполнения ----------
  let timer = null;
  function fmtTime(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + " мин " + String(s).padStart(2, "0") + " с";
  }
  function esc(s) {
    return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  async function poll() {
    clearTimeout(timer);
    const r = await fetch("/run/status");
    const j = await r.json();
    if (j.state === "idle") return;
    $("job").hidden = false;
    const banner = $("job-banner"), running = j.state === "running";
    $("start").disabled = running;
    $("cancel").hidden = !running;
    const info = esc(j.path) + " · обработано файлов: " + j.processed + " · " + fmtTime(j.elapsed);
    if (running) {
      banner.className = "banner";
      banner.innerHTML = "<b>Идёт сканирование…</b> " + info;
    } else if (j.state === "done") {
      banner.className = "banner ok";
      banner.innerHTML = "<b>Сканирование завершено.</b> " + info +
        (j.scan_url ? ' · <a href="' + j.scan_url + '">Открыть результат скана #' + j.scan_id + "</a>" : "") +
        ' · <a href="/reports">Отчёты</a>';
    } else if (j.state === "cancelled") {
      banner.className = "banner stale";
      banner.innerHTML = "<b>Сканирование остановлено.</b> В базу ничего не записано. " + info;
    } else {
      banner.className = "banner stale";
      banner.innerHTML = "<b>Сканирование завершилось с ошибкой</b> (код " + j.returncode + "). Подробности ниже. " + info;
    }
    const log = $("log"), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = j.log;
    if (atBottom) log.scrollTop = log.scrollHeight;
    if (running) timer = setTimeout(poll, 1500);
  }

  poll();  // если скан уже идёт (страницу обновили) - сразу показываем ход
})();
