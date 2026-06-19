#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Веб-интерфейс для download_images.py (stdlib-only, без внешних веб-зависимостей).

Идея: редактор открывает внутреннюю страницу, вставляет ссылки на статьи,
жмёт кнопку — сервер сам качает картинки и отдаёт zip-архив. Такой режим
убирает ручную установку Python/браузеров на рабочих машинах и централизует
сетевое окружение для источников с антибот-защитой.

Особенности:
- Только стандартная библиотека для веб-части (нужны лишь curl_cffi/bs4/pillow,
  которые и так требует download_images.py).
- Один воркер + очередь: все загрузки выполняются последовательно, с паузами
  исходного скрипта, чтобы не создавать залп параллельных запросов к источникам.
- Простой вход по паролю (переменная окружения IMG_WEB_PASSWORD).
- Прогресс через опрос статуса задачи, на выходе — zip с картинками.

Запуск:
    IMG_WEB_PASSWORD="секрет" python3 web_app.py
Слушает 0.0.0.0:8787 (порт меняется через IMG_WEB_PORT).
"""

import io
import json
import os
import queue
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from download_images import ArticleImageDownloader

# ---------- КОНФИГ ----------
HOST = os.environ.get("IMG_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("IMG_WEB_PORT", "8787"))
PASSWORD = os.environ.get("IMG_WEB_PASSWORD", "").strip()
MAX_URLS_PER_JOB = int(os.environ.get("IMG_WEB_MAX_URLS", "30"))
JOB_TTL_SECONDS = int(os.environ.get("IMG_WEB_JOB_TTL", str(6 * 3600)))
SESSION_TTL_SECONDS = int(os.environ.get("IMG_WEB_SESSION_TTL", str(7 * 24 * 3600)))

JOBS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_web_jobs")
os.makedirs(JOBS_ROOT, exist_ok=True)

# ---------- СОСТОЯНИЕ ----------
_sessions = {}           # token -> expiry_ts
_sessions_lock = threading.Lock()

_jobs = {}               # job_id -> dict
_jobs_lock = threading.Lock()
_job_queue: "queue.Queue[str]" = queue.Queue()


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def _session_valid(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            _sessions.pop(token, None)
            return False
        return True


def _sweep_old_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items()
                 if j["status"] in ("done", "error") and now - j["finished_at"] > JOB_TTL_SECONDS]
        for jid in stale:
            job = _jobs.pop(jid)
            shutil.rmtree(job["dir"], ignore_errors=True)


def _create_job(urls):
    job_id = secrets.token_hex(8)
    job_dir = os.path.join(JOBS_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    job = {
        "id": job_id,
        "urls": urls,
        "status": "queued",          # queued | running | done | error
        "total": len(urls),
        "current": 0,                # сколько статей обработано
        "current_url": "",
        "images": 0,                 # всего скачано картинок
        "results": [],               # [{url, count}]
        "error": "",
        "zip_name": "",
        "dir": job_dir,
        "created_at": time.time(),
        "finished_at": 0.0,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    _job_queue.put(job_id)
    return job


def _queue_position(job_id: str) -> int:
    """Сколько задач стоит в очереди перед этой (грубая оценка)."""
    with _jobs_lock:
        ahead = sum(1 for j in _jobs.values()
                    if j["status"] == "queued" and j["created_at"] < _jobs[job_id]["created_at"])
        running = any(j["status"] == "running" for j in _jobs.values())
    return ahead + (1 if running else 0)


def _worker_loop():
    """Единственный воркер: обрабатывает задачи строго по очереди."""
    while True:
        job_id = _job_queue.get()
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            _job_queue.task_done()
            continue
        try:
            _run_job(job)
        except Exception as exc:  # noqa: BLE001 — воркер не должен падать
            job["status"] = "error"
            job["error"] = f"Внутренняя ошибка: {exc}"
            job["finished_at"] = time.time()
        finally:
            _job_queue.task_done()


def _run_job(job):
    job["status"] = "running"
    images_dir = os.path.join(job["dir"], "images")
    os.makedirs(images_dir, exist_ok=True)

    downloader = ArticleImageDownloader(
        download_dir=images_dir,
        debug=False,
        pause_between_downloads=0.5,
    )

    total_images = 0
    for url in job["urls"]:
        job["current_url"] = url
        try:
            downloaded = downloader.process_article(url)
        except Exception as exc:  # noqa: BLE001
            downloaded = []
            job["results"].append({"url": url, "count": 0, "error": str(exc)})
        else:
            job["results"].append({"url": url, "count": len(downloaded)})
        total_images += len(downloaded)
        job["images"] = total_images
        job["current"] += 1

    if total_images == 0:
        job["status"] = "error"
        job["error"] = ("Не удалось скачать ни одной картинки. Возможно, ссылки не "
                        "содержат изображений, либо сайт всё равно отдал антибот-проверку.")
        job["finished_at"] = time.time()
        return

    zip_name = f"images_{job['id']}.zip"
    zip_path = os.path.join(job["dir"], zip_name)
    _zip_dir(images_dir, zip_path)
    job["zip_name"] = zip_name
    job["status"] = "done"
    job["finished_at"] = time.time()


def _zip_dir(src_dir: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, src_dir)
                zf.write(full, arc)


# ---------- HTML ----------
LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход</title>
<style>
 body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;background:#0f1115;color:#e7e9ee;
   display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 .card{background:#1a1d24;padding:32px;border-radius:14px;width:320px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
 h1{font-size:18px;margin:0 0 16px}
 input{width:100%;box-sizing:border-box;padding:11px;border-radius:9px;border:1px solid #333;
   background:#0f1115;color:#e7e9ee;font-size:15px}
 button{width:100%;margin-top:14px;padding:11px;border:0;border-radius:9px;background:#3b82f6;
   color:#fff;font-size:15px;cursor:pointer}
 button:hover{background:#2f6fe0}
 .err{color:#f87171;font-size:13px;margin-top:10px;min-height:16px}
</style></head><body>
 <form class="card" method="post" action="/login">
   <h1>Скачивание картинок — вход</h1>
   <input type="password" name="password" placeholder="Пароль" autofocus required>
   <button type="submit">Войти</button>
   <div class="err">__ERROR__</div>
 </form>
</body></html>"""

APP_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Скачивание картинок из статей</title>
<style>
 body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;background:#0f1115;color:#e7e9ee;margin:0}
 .wrap{max-width:760px;margin:0 auto;padding:28px 18px 60px}
 h1{font-size:20px}
 p.hint{color:#9aa3b2;font-size:14px;line-height:1.5}
 textarea{width:100%;box-sizing:border-box;min-height:160px;padding:12px;border-radius:10px;
   border:1px solid #333;background:#1a1d24;color:#e7e9ee;font-size:14px;font-family:ui-monospace,Menlo,Consolas,monospace}
 .row{display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap}
 button{padding:11px 20px;border:0;border-radius:9px;background:#3b82f6;color:#fff;font-size:15px;cursor:pointer}
 button:disabled{background:#33394a;cursor:not-allowed}
 button:hover:not(:disabled){background:#2f6fe0}
 .logout{background:transparent;color:#9aa3b2;text-decoration:underline;padding:0;font-size:13px}
 #status{margin-top:22px;background:#1a1d24;border:1px solid #262b36;border-radius:12px;padding:18px;display:none}
 .bar{height:8px;background:#262b36;border-radius:6px;overflow:hidden;margin:12px 0}
 .bar>i{display:block;height:100%;width:0;background:#3b82f6;transition:width .3s}
 .ok{color:#34d399}.bad{color:#f87171}.muted{color:#9aa3b2;font-size:13px}
 a.dl{display:inline-block;margin-top:8px;padding:11px 20px;border-radius:9px;background:#34d399;
   color:#06281d;font-weight:600;text-decoration:none}
 ul.res{list-style:none;padding:0;margin:12px 0 0;font-size:13px}
 ul.res li{padding:4px 0;border-top:1px solid #262b36;word-break:break-all}
</style></head><body>
<div class="wrap">
 <div class="row" style="justify-content:space-between">
   <h1 style="margin:0">📷 Картинки из статей</h1>
   <form method="post" action="/logout"><button class="logout" type="submit">Выйти</button></form>
 </div>
 <p class="hint">Вставьте ссылки на статьи — по одной в строке. Сервер сам скачает картинки
   в централизованном окружении и соберёт их в zip-архив. Поддерживаются GSMArena, PhoneArena,
   ZDNet, Tom's Hardware и др.</p>
 <textarea id="urls" placeholder="https://www.gsmarena.com/..._review.php
https://www.phonearena.com/reviews/..."></textarea>
 <div class="row">
   <button id="go">Скачать картинки</button>
   <span class="muted" id="hint"></span>
 </div>

 <div id="status">
   <div id="phase">⏳ Готовлюсь…</div>
   <div class="bar"><i id="fill"></i></div>
   <div class="muted" id="detail"></div>
   <ul class="res" id="res"></ul>
   <div id="finish"></div>
 </div>
</div>
<script>
const $ = s => document.querySelector(s);
let timer = null;

$('#go').addEventListener('click', start);

async function start(){
  const text = $('#urls').value;
  const urls = text.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!urls.length){ $('#hint').textContent='Вставьте хотя бы одну ссылку.'; return; }
  $('#hint').textContent='';
  $('#go').disabled = true;
  $('#status').style.display='block';
  $('#res').innerHTML=''; $('#finish').innerHTML='';
  $('#phase').textContent='⏳ Отправляю задачу…';
  $('#fill').style.width='0';

  let r;
  try{
    r = await fetch('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({urls})});
  }catch(e){ fail('Сеть недоступна.'); return; }
  if(r.status===401){ location.href='/'; return; }
  const data = await r.json();
  if(!r.ok){ fail(data.error||'Ошибка сервера.'); return; }
  poll(data.job_id);
}

function poll(id){
  timer = setInterval(async ()=>{
    let r;
    try{ r = await fetch('/api/jobs/'+id); }catch(e){ return; }
    if(r.status===401){ location.href='/'; return; }
    const j = await r.json();
    render(j);
    if(j.status==='done' || j.status==='error'){
      clearInterval(timer); $('#go').disabled=false;
    }
  }, 1500);
}

function render(j){
  const pct = j.total ? Math.round(100*j.current/j.total) : 0;
  $('#fill').style.width = pct+'%';
  if(j.status==='queued'){
    $('#phase').textContent = '🕒 В очереди' + (j.queue_position?(' (перед вами: '+j.queue_position+')'):'');
    $('#detail').textContent='';
  }else if(j.status==='running'){
    $('#phase').textContent = '⬇️ Обрабатываю статью '+(j.current+1)+' из '+j.total;
    $('#detail').textContent = j.current_url || '';
  }
  renderResults(j);
  if(j.status==='done'){
    $('#phase').innerHTML = '<span class="ok">✅ Готово</span>';
    $('#detail').textContent = 'Скачано картинок: '+j.images;
    $('#finish').innerHTML = '<a class="dl" href="/api/jobs/'+j.id+'/download">📎 Скачать zip ('+j.images+' шт.)</a>';
  }else if(j.status==='error'){
    $('#phase').innerHTML = '<span class="bad">⚠️ Не получилось</span>';
    $('#detail').textContent = j.error||'';
  }
}

function renderResults(j){
  if(!j.results || !j.results.length){ return; }
  $('#res').innerHTML = j.results.map(rr=>{
    const cls = rr.count>0?'ok':'bad';
    const n = rr.count>0 ? (rr.count+' шт.') : (rr.error?('ошибка'):'0');
    return '<li><span class="'+cls+'">'+n+'</span> — '+escapeHtml(rr.url)+'</li>';
  }).join('');
}

function fail(msg){ $('#phase').innerHTML='<span class="bad">⚠️ '+escapeHtml(msg)+'</span>'; $('#go').disabled=false; }
function escapeHtml(s){ return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
</script>
</body></html>"""


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "ImgDownloader/1.0"

    # -- утилиты --
    def _token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("img_sess")
        return morsel.value if morsel else ""

    def _authed(self):
        return _session_valid(self._token())

    def _send_html(self, body: str, status=HTTPStatus.OK, extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, extra_headers=None):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    # -- маршрутизация --
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            if self._authed():
                self._send_html(APP_PAGE)
            else:
                self._send_html(LOGIN_PAGE.replace("__ERROR__", ""))
            return
        if path.startswith("/api/jobs/"):
            if not self._authed():
                self._send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
                return
            rest = path[len("/api/jobs/"):]
            if rest.endswith("/download"):
                self._handle_download(rest[:-len("/download")])
            else:
                self._handle_job_status(rest)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            self._handle_login()
            return
        if path == "/logout":
            with _sessions_lock:
                _sessions.pop(self._token(), None)
            self._redirect("/", extra_headers=[("Set-Cookie", "img_sess=; Path=/; Max-Age=0")])
            return
        if path == "/api/jobs":
            self._handle_create_job()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    # -- обработчики --
    def _handle_login(self):
        body = self._read_body().decode("utf-8", "replace")
        fields = parse_qs(body)
        supplied = (fields.get("password", [""])[0]).strip()
        if PASSWORD and secrets.compare_digest(supplied, PASSWORD):
            token = _new_session()
            cookie = (f"img_sess={token}; Path=/; HttpOnly; SameSite=Lax; "
                      f"Max-Age={SESSION_TTL_SECONDS}")
            self._redirect("/", extra_headers=[("Set-Cookie", cookie)])
        else:
            self._send_html(LOGIN_PAGE.replace("__ERROR__", "Неверный пароль"),
                            status=HTTPStatus.UNAUTHORIZED)

    def _handle_create_job(self):
        if not self._authed():
            self._send_json({"error": "auth"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = json.loads(self._read_body().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Некорректный запрос"}, HTTPStatus.BAD_REQUEST)
            return
        urls = [u.strip() for u in payload.get("urls", []) if isinstance(u, str) and u.strip()]
        urls = [u for u in urls if u.lower().startswith(("http://", "https://"))]
        if not urls:
            self._send_json({"error": "Не указаны корректные ссылки (http/https)"},
                            HTTPStatus.BAD_REQUEST)
            return
        if len(urls) > MAX_URLS_PER_JOB:
            self._send_json({"error": f"Слишком много ссылок (максимум {MAX_URLS_PER_JOB})"},
                            HTTPStatus.BAD_REQUEST)
            return
        _sweep_old_jobs()
        job = _create_job(urls)
        self._send_json({"job_id": job["id"]})

    def _handle_job_status(self, job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            self._send_json({"error": "Задача не найдена"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({
            "id": job["id"],
            "status": job["status"],
            "total": job["total"],
            "current": job["current"],
            "current_url": job["current_url"],
            "images": job["images"],
            "results": job["results"],
            "error": job["error"],
            "zip_name": job["zip_name"],
            "queue_position": _queue_position(job_id) if job["status"] == "queued" else 0,
        })

    def _handle_download(self, job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job or job["status"] != "done" or not job["zip_name"]:
            self.send_error(HTTPStatus.NOT_FOUND, "Архив не готов")
            return
        zip_path = os.path.join(job["dir"], job["zip_name"])
        if not os.path.exists(zip_path):
            self.send_error(HTTPStatus.NOT_FOUND, "Архив не найден")
            return
        size = os.path.getsize(zip_path)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{job["zip_name"]}"')
        self.end_headers()
        with open(zip_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def log_message(self, fmt, *args):  # компактные логи в journald
        print("[web] " + (fmt % args))


def main():
    if not PASSWORD:
        print("=" * 60)
        print("ВНИМАНИЕ: переменная IMG_WEB_PASSWORD не задана.")
        print("Вход в интерфейс будет невозможен. Запусти так:")
        print('  IMG_WEB_PASSWORD="придумай-пароль" python3 web_app.py')
        print("=" * 60)

    worker = threading.Thread(target=_worker_loop, daemon=True)
    worker.start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[web] Слушаю http://{HOST}:{PORT}  (воркер: 1, очередь включена)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] Останавливаюсь…")
        httpd.shutdown()


if __name__ == "__main__":
    main()
