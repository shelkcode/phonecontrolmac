"""
Управление Mac с телефона через браузер.
Установка:
    pip install flask pillow pyautogui psutil opencv-python
    brew install brightness        # опционально, для яркости

Запуск:
    python phone_control_mac.py

Телефон и Mac в одной Wi-Fi сети.
PIN: golda7433 (всегда)

При первом запуске macOS откроет панели прав — разреши:
  • Универсальный доступ (Accessibility)
  • Запись экрана (Screen Recording)
  • Управление компьютером (Automation)
  • Камера и Микрофон (если нужна камера)
После выдачи прав перезапусти скрипт.
"""
import io, os, re, time, secrets, socket, subprocess, tempfile, threading, uuid, webbrowser, platform
from functools import lru_cache

try:
    import psutil
except ImportError:
    psutil = None
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
except Exception:
    pyautogui = None

from PIL import Image, ImageDraw
from flask import Flask, request, jsonify, Response, make_response, send_file
from werkzeug.utils import secure_filename

IS_MAC = platform.system() == "Darwin"

PORT = 35535
PIN = "golda7433"
TOKEN = secrets.token_hex(16)
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "phone_control")
os.makedirs(UPLOAD_DIR, exist_ok=True)

for _f in os.listdir(UPLOAD_DIR):
    _p = os.path.join(UPLOAD_DIR, _f)
    try:
        if time.time() - os.path.getmtime(_p) > 86400:
            os.remove(_p)
    except Exception:
        pass

app = Flask(__name__)
INPUT_LOCK = threading.Lock()

# ============================================================
#   ПРАВА macOS — открываем ОДИН раз при старте
# ============================================================
PERMISSIONS_OPENED = {"done": False}
PERMISSIONS_LOCK = threading.Lock()

PRIVACY_URLS = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
]


def open_privacy_panels(force=False):
    if not IS_MAC:
        return False
    with PERMISSIONS_LOCK:
        if PERMISSIONS_OPENED["done"] and not force:
            return False
        for u in PRIVACY_URLS:
            try:
                subprocess.Popen(["open", u])
                time.sleep(0.4)
            except Exception:
                pass
        PERMISSIONS_OPENED["done"] = True
    return True


def has_accessibility() -> bool:
    if not IS_MAC:
        return True
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process'],
            capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def has_screen_recording() -> bool:
    if not IS_MAC:
        return True
    try:
        tmp = os.path.join(tempfile.gettempdir(), "scr_test.png")
        r = subprocess.run(["screencapture", "-x", "-t", "png", tmp],
                           capture_output=True, timeout=3)
        ok = r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100
        if os.path.exists(tmp):
            os.remove(tmp)
        return ok
    except Exception:
        return False


# ============================================================
#   AppleScript-хелперы
# ============================================================
def osa(script: str, timeout=10):
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(args=[], returncode=1,
                                           stdout=b"", stderr=str(e).encode())


def osa_text(script: str, timeout=10) -> str:
    r = osa(script, timeout)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""


# ============================================================
#   Список окон
# ============================================================
def list_windows():
    if not IS_MAC:
        return []
    script = r'''
tell application "System Events"
    set out to ""
    repeat with p in (every process whose background only is false)
        try
            set pname to name of p
            repeat with w in (every window of p)
                try
                    set wname to name of w
                    set out to out & pname & "\t" & wname & "\n"
                end try
            end repeat
        end try
    end repeat
    return out
end tell
'''
    raw = osa_text(script, timeout=8)
    res = []
    for line in raw.splitlines():
        if "\t" in line:
            pname, wname = line.split("\t", 1)
            if wname.strip():
                res.append({"id": f"{pname}||{wname}", "title": f"{wname} — {pname}"})
    return res


def activate_app(app_name: str):
    safe = app_name.replace('"', '\\"')
    osa(f'tell application "{safe}" to activate')


# ============================================================
#   Ввод: мышь + клавиатура
# ============================================================
def _require_pyautogui():
    if pyautogui is None:
        raise RuntimeError("pyautogui не установлен. Выполни: pip install pyautogui")
    return pyautogui


def type_text(text: str):
    if not text:
        return
    esc = (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", '\\n')
                .replace("\r", '\\r'))
    osa(f'tell application "System Events" to keystroke "{esc}"', timeout=20)


KEY_CODES = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51,
    "delete": 117, "esc": 53, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
MOD_MAP = {"ctrl": "control down", "alt": "option down", "shift": "shift down",
           "cmd": "command down", "win": "command down"}


def send_key_combo(combo):
    mods = [MOD_MAP[m] for m in combo[:-1] if m in MOD_MAP]
    key = combo[-1].lower()
    if key in KEY_CODES:
        code = KEY_CODES[key]
        mod_str = "{" + ", ".join(mods) + "}" if mods else "{}"
        osa(f'tell application "System Events" to key code {code} using {mod_str}')
        return
    if len(key) == 1:
        esc = key.replace('"', '\\"')
        mod_str = "{" + ", ".join(mods) + "}" if mods else "{}"
        osa(f'tell application "System Events" to keystroke "{esc}" using {mod_str}')
        return
    raise ValueError(f"Неизвестная клавиша: {key}")


def volume_up(step=10):
    osa(f'''
set curVol to output volume of (get volume settings)
set newVol to curVol + {step}
if newVol > 100 then set newVol to 100
set volume output volume newVol
''', timeout=3)
    return True


def volume_down(step=10):
    osa(f'''
set curVol to output volume of (get volume settings)
set newVol to curVol - {step}
if newVol < 0 then set newVol to 0
set volume output volume newVol
''', timeout=3)
    return True


def volume_mute():
    osa('''
set isMuted to output muted of (get volume settings)
set volume output muted (not isMuted)
''', timeout=3)
    return True


# ============================================================
#   Защита PIN
# ============================================================
@app.before_request
def guard():
    if request.path in ("/login", "/", "/manifest.json", "/api/permissions/status",
                        "/api/permissions/open"):
        return
    if request.cookies.get("auth") != TOKEN:
        return Response("Нет доступа", 401)


@app.post("/login")
def login():
    if (request.json or {}).get("pin") == PIN:
        r = make_response(jsonify(ok=True))
        r.set_cookie("auth", TOKEN, httponly=True, samesite="Strict")
        return r
    return jsonify(ok=False), 403


@app.get("/api/ping")
def ping():
    return jsonify(ok=True)


# ============================================================
#   Права
# ============================================================
@app.get("/api/permissions/status")
def permissions_status():
    return jsonify(
        is_mac=IS_MAC,
        accessibility=has_accessibility() if IS_MAC else True,
        screen_recording=has_screen_recording() if IS_MAC else True,
        opened=PERMISSIONS_OPENED["done"],
    )


@app.post("/api/permissions/open")
def permissions_open():
    if not IS_MAC:
        return jsonify(ok=False, msg="Только для macOS")
    PERMISSIONS_OPENED["done"] = False
    open_privacy_panels(force=True)
    return jsonify(ok=True, msg="Панели прав открыты. Выдай доступ и перезапусти скрипт.")


# ============================================================
#   Скриншот
# ============================================================
def grab_screen_mac():
    tmp = os.path.join(tempfile.gettempdir(), f"scr_{uuid.uuid4().hex}.png")
    try:
        r = subprocess.run(["screencapture", "-x", "-t", "png", tmp],
                           capture_output=True, timeout=8)
        if r.returncode == 0 and os.path.exists(tmp):
            img = Image.open(tmp).convert("RGB")
            return img, {"left": 0, "top": 0, "width": img.width, "height": img.height}
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200])
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def grab_screen():
    if IS_MAC:
        return grab_screen_mac()
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), mon


def _int(name, default, lo, hi):
    try:
        return min(max(int(request.args.get(name, default)), lo), hi)
    except ValueError:
        return default


@app.get("/screen.jpg")
def screen():
    try:
        img, mon = grab_screen()
    except Exception as e:
        return Response(f"Ошибка скриншота: {e}", 500)
    x, y = (pyautogui.position() if pyautogui else (0, 0))
    w = _int("w", 1000, 320, 1920)
    q = _int("q", 55, 20, 90)
    k = w / img.width
    img = img.resize((w, int(img.height * k)), Image.BILINEAR)
    d = ImageDraw.Draw(img)
    try:
        cx, cy = (x - mon["left"]) * k, (y - mon["top"]) * k
        d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), outline="red", width=3)
    except Exception:
        pass
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    r = Response(buf.getvalue(), mimetype="image/jpeg")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/screenshot.png")
def screenshot():
    try:
        img, _ = grab_screen()
    except Exception as e:
        return Response(f"Ошибка: {e}", 500)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = Response(buf.getvalue(), mimetype="image/png")
    r.headers["Content-Disposition"] = "attachment; filename=screenshot.png"
    return r


# ============================================================
#   Мышь
# ============================================================
@app.post("/api/mouse")
def mouse():
    pg = _require_pyautogui()
    d = request.json or {}
    a = d.get("action")
    btn = d.get("button", "left")
    if btn not in ("left", "right", "middle"):
        btn = "left"
    if a == "move":
        g = min(max(float(d.get("gain", 2)), 0.3), 8)
        pg.moveRel(float(d["dx"]) * g, float(d["dy"]) * g)
    elif a == "click":
        pg.click(button=btn)
    elif a == "double":
        pg.doubleClick()
    elif a == "down":
        pg.mouseDown()
    elif a == "up":
        pg.mouseUp()
    elif a == "scroll":
        notches = min(max(float(d.get("amount", 0)), -60), 60)
        pg.scroll(int(notches))
    return jsonify(ok=True)


# ============================================================
#   Клавиатура
# ============================================================
NAMED = {"enter", "return", "backspace", "esc", "escape", "tab", "space",
         "left", "right", "up", "down", "delete", "home", "end",
         "pageup", "pagedown"} | {f"f{i}" for i in range(1, 13)}
MODS = {"ctrl", "alt", "shift", "win", "cmd"}


def key_ok(k):
    return k in NAMED or (len(k) == 1 and k.isascii() and (k.isalnum() or k in "[];',./-=`\\"))


@app.post("/api/key")
def key():
    d = request.json or {}
    with INPUT_LOCK:
        if d.get("bs"):
            pg = _require_pyautogui()
            for _ in range(min(int(d["bs"]), 200)):
                pg.press("backspace")
        if d.get("text"):
            type_text(str(d["text"])[:2000])
        if d.get("combo"):
            c = [str(x).lower() for x in d["combo"]][:5]
            if c and all(x in MODS or key_ok(x) for x in c) and c[-1] not in MODS:
                try:
                    send_key_combo(c)
                except Exception as e:
                    return jsonify(ok=False, msg=str(e)[:100])
        if d.get("volume"):
            action = d["volume"]
            if action == "up":
                volume_up()
            elif action == "down":
                volume_down()
            elif action == "mute":
                volume_mute()
        k = d.get("key")
        if k and key_ok(str(k)):
            for _ in range(min(int(d.get("n", 1)), 100)):
                try:
                    send_key_combo([str(k).lower()])
                except Exception:
                    pass
    return jsonify(ok=True)


# ============================================================
#   Управление окнами
# ============================================================
def target_app():
    if not IS_MAC:
        return None
    script = '''
tell application "System Events"
    try
        set frontApp to first application process whose frontmost is true
        return name of frontApp
    end try
end tell
'''
    name = osa_text(script, timeout=3)
    if name and name not in ("Terminal", "iTerm2", "Python", "python3", "phone_control"):
        return name
    for w in list_windows():
        app = w["id"].split("||", 1)[0]
        if app not in ("Terminal", "iTerm2", "Finder"):
            return app
    return None


@app.post("/api/win/<act>")
def win(act):
    app = target_app()
    if not app:
        return jsonify(ok=False, msg="Нет подходящего окна")
    safe = app.replace('"', '\\"')
    if act == "close":
        osa(f'tell application "{safe}" to close front window', timeout=5)
        time.sleep(0.4)
        return jsonify(ok=True, closed=True)
    if act == "min":
        osa(f'tell application "{safe}" to set miniaturized of front window to true')
    elif act == "max":
        osa(f'''
tell application "{safe}"
    try
        set bounds of front window to {{0, 0, 1920, 1080}}
    end try
end tell
''')
    elif act == "restore":
        osa(f'tell application "{safe}" to set miniaturized of front window to false')
    return jsonify(ok=True)


@app.get("/api/windows")
def windows():
    return jsonify(list_windows())


@app.post("/api/windows/<act>")
def windows_act(act):
    hid = (request.json or {}).get("id", "")
    if "||" not in hid:
        return jsonify(ok=False), 404
    app_name, win_name = hid.split("||", 1)
    safe_app = app_name.replace('"', '\\"')
    safe_win = win_name.replace('"', '\\"')
    if act == "close":
        osa(f'''
tell application "{safe_app}"
    try
        close (first window whose name is "{safe_win}")
    end try
end tell
''')
    elif act == "focus":
        activate_app(app_name)
    return jsonify(ok=True)


# ============================================================
#   Камера
# ============================================================
CAM = {"cap": None, "last": 0.0}
CAM_LOCK = threading.Lock()


def cam_release():
    with CAM_LOCK:
        if CAM["cap"] is not None:
            CAM["cap"].release()
            CAM["cap"] = None


def cam_watchdog():
    while True:
        time.sleep(1)
        if CAM["cap"] is not None and time.time() - CAM["last"] > 6:
            cam_release()


threading.Thread(target=cam_watchdog, daemon=True).start()


@app.post("/api/camera/start")
def camera_start():
    try:
        import cv2
    except ImportError:
        return jsonify(ok=False, msg="Установи: pip install opencv-python")
    try:
        idx = min(max(int((request.json or {}).get("index", 0)), 0), 5)
    except (TypeError, ValueError):
        idx = 0
    cam_release()
    with CAM_LOCK:
        cap = None
        for backend in ((cv2.CAP_AVFOUNDATION,) if IS_MAC else (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)):
            c = cv2.VideoCapture(idx, *backend) if isinstance(backend, tuple) else cv2.VideoCapture(idx, backend)
            if c.isOpened():
                for _ in range(10):
                    ok, _f = c.read()
                    if ok:
                        break
                    time.sleep(0.1)
                if ok:
                    cap = c
                    break
            c.release()
        if cap is None:
            return jsonify(ok=False, msg=f"Не удалось открыть камеру {idx} (разреши доступ в Настройках)")
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        for _ in range(15):
            ok, fr = cap.read()
            if ok and fr is not None and fr.mean() > 8:
                break
        CAM["cap"], CAM["last"] = cap, time.time()
    return jsonify(ok=True)


def cam_grab(maxw=960, q=60):
    import cv2
    with CAM_LOCK:
        cap = CAM["cap"]
        if cap is None:
            return "off"
        frame = None
        for _ in range(3):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
            time.sleep(0.05)
        CAM["last"] = time.time()
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if w > maxw:
        frame = cv2.resize(frame, (maxw, int(h * maxw / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes() if ok else None


@app.get("/camera.jpg")
def camera_frame():
    data = cam_grab()
    if data == "off":
        return Response("off", 409)
    if data is None:
        return Response("no frame", 503)
    r = Response(data, mimetype="image/jpeg")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/api/camera/snapshot")
def camera_snapshot():
    data = cam_grab(3000, 92)
    if data == "off" or data is None:
        return Response("нет кадра", 409)
    r = Response(data, mimetype="image/jpeg")
    r.headers["Content-Disposition"] = "attachment; filename=camera.jpg"
    return r


@app.post("/api/camera/stop")
def camera_stop():
    cam_release()
    return jsonify(ok=True)


# ============================================================
#   Система, яркость, процессы
# ============================================================
def get_brightness():
    if not IS_MAC:
        return None
    try:
        r = subprocess.run(["brightness", "-l"], capture_output=True, timeout=2)
        if r.returncode == 0:
            txt = r.stdout.decode("utf-8", "replace")
            m = re.search(r"brightness\s+([0-9.]+)", txt)
            if m:
                return int(float(m.group(1)) * 100)
    except Exception:
        pass
    return None


def set_brightness(value: int):
    if not IS_MAC:
        return False
    try:
        r = subprocess.run(["brightness", str(value / 100.0)],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


@app.get("/api/sysinfo")
def sysinfo():
    if not psutil:
        return jsonify(ok=False, msg="Установи: pip install psutil")
    b = psutil.sensors_battery()
    disks = []
    for part in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"name": part.device, "used": u.percent, "free_gb": round(u.free / 1e9, 1)})
        except Exception:
            pass
    vm = psutil.virtual_memory()
    return jsonify(ok=True, cpu=psutil.cpu_percent(interval=0.3), ram=vm.percent,
                   ram_used=round(vm.used / 1e9, 1), ram_total=round(vm.total / 1e9, 1),
                   battery=None if b is None else {"percent": round(b.percent), "plugged": bool(b.power_plugged)},
                   uptime=int(time.time() - psutil.boot_time()), disks=disks, brightness=get_brightness())


@app.post("/api/brightness")
def brightness():
    try:
        v = min(max(int((request.json or {}).get("value", 50)), 5), 100)
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="Ошибка")
    if set_brightness(v):
        return jsonify(ok=True)
    return jsonify(ok=False, msg="Поставь `brew install brightness` и перезапусти скрипт")


PROTECTED = {"kernel_task", "launchd", "WindowServer", "loginwindow", "Dock",
             "Finder", "SystemUIServer", "python", "python3", "Terminal", "iTerm2"}


def my_name(p):
    return (p.info.get("username") or "").lower() == os.environ.get("USER", "").lower()


@app.get("/api/procs")
def procs():
    if not psutil:
        return jsonify(ok=False, msg="Установи: pip install psutil")
    res = {}
    for p in psutil.process_iter(["pid", "name", "memory_info", "username"]):
        try:
            if not my_name(p) or p.info["pid"] == os.getpid():
                continue
            if p.info["name"].lower() in {x.lower() for x in PROTECTED}:
                continue
            e = res.setdefault(p.info["name"], {"name": p.info["name"], "n": 0, "mb": 0})
            e["n"] += 1
            e["mb"] += p.info["memory_info"].rss / 1e6
        except Exception:
            pass
    top = sorted(res.values(), key=lambda x: -x["mb"])[:20]
    for t in top:
        t["mb"] = round(t["mb"])
    return jsonify(ok=True, procs=top)


@app.post("/api/procs/kill")
def procs_kill():
    name = str((request.json or {}).get("name", ""))
    if not psutil or not name or name in PROTECTED:
        return jsonify(ok=False, msg="Нельзя завершить")
    n = 0
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            if p.info["name"] == name and p.info["pid"] != os.getpid() and my_name(p):
                p.terminate()
                n += 1
        except Exception:
            pass
    return jsonify(ok=n > 0, msg="Не удалось завершить" if n == 0 else None)


# ============================================================
#   Файлы
# ============================================================
HOME = os.path.expanduser("~")
ROOTS = {"Загрузки": os.path.join(HOME, "Downloads"),
         "Рабочий стол": os.path.join(HOME, "Desktop"),
         "Документы": os.path.join(HOME, "Documents"),
         "Картинки": os.path.join(HOME, "Pictures"),
         "Видео": os.path.join(HOME, "Movies"),
         "Музыка": os.path.join(HOME, "Music")}
PHONE_DIR = os.path.join(HOME, "Downloads", "PhoneUploads")
DANGEROUS = {".exe", ".bat", ".cmd", ".msi", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".lnk",
             ".scr", ".com", ".reg", ".hta", ".jar", ".dll", ".app", ".command", ".sh", ".scpt"}


def safe_path(p):
    if not p:
        return None
    p = os.path.realpath(p)
    for r in ROOTS.values():
        r = os.path.realpath(r)
        if p == r or p.startswith(r + os.sep):
            return p
    return None


@app.get("/api/files")
def files_list():
    path = request.args.get("path", "")
    if not path:
        return jsonify(ok=True, path="", parent="",
                       items=[{"name": k, "dir": True, "path": v, "size": 0}
                              for k, v in ROOTS.items() if os.path.isdir(v)])
    p = safe_path(path)
    if not p or not os.path.isdir(p):
        return jsonify(ok=False, msg="Нет доступа")
    items = []
    try:
        for e in os.scandir(p):
            try:
                items.append({"name": e.name, "dir": e.is_dir(), "path": e.path,
                              "size": 0 if e.is_dir() else e.stat().st_size})
            except OSError:
                pass
    except OSError:
        return jsonify(ok=False, msg="Не удалось открыть папку")
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    is_root = any(os.path.realpath(r) == p for r in ROOTS.values())
    return jsonify(ok=True, path=p, parent="" if is_root else os.path.dirname(p), items=items[:500])


@app.get("/api/download")
def download():
    p = safe_path(request.args.get("path", ""))
    if not p or not os.path.isfile(p):
        return Response("Нет файла", 404)
    return send_file(p, as_attachment=True)


@app.post("/api/files/open")
def files_open():
    p = safe_path((request.json or {}).get("path", ""))
    if not p or not os.path.isfile(p):
        return jsonify(ok=False, msg="Нет файла")
    if os.path.splitext(p)[1].lower() in DANGEROUS:
        return jsonify(ok=False, msg="Программы и скрипты так не запускаем")
    subprocess.Popen(["open", p])
    return jsonify(ok=True)


@app.post("/api/upload")
def upload_any():
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False), 400
    os.makedirs(PHONE_DIR, exist_ok=True)
    base, ext = os.path.splitext(f.filename or "")
    base = secure_filename(base) or "file"
    ext = re.sub(r"[^.\w]", "", ext)[:10]
    path, i = os.path.join(PHONE_DIR, base + ext), 1
    while os.path.exists(path):
        path = os.path.join(PHONE_DIR, f"{base}_{i}{ext}")
        i += 1
    f.save(path)
    return jsonify(ok=True)


# ============================================================
#   Steam (macOS)
# ============================================================
STEAM_DIRS = [
    os.path.expanduser("~/Library/Application Support/Steam"),
    "/Applications/Steam.app/Contents/MacOS",
]


def find_steam():
    for d in STEAM_DIRS:
        if os.path.isdir(d):
            return d
    return None


def steam_games():
    base = find_steam()
    if not base:
        return []
    apps_dir = os.path.join(base, "steamapps")
    if not os.path.isdir(apps_dir):
        return []
    games = []
    for fn in os.listdir(apps_dir):
        if not fn.startswith("appmanifest_") or not fn.endswith(".acf"):
            continue
        try:
            txt = open(os.path.join(apps_dir, fn), encoding="utf-8", errors="ignore").read()
            aid = re.search(r'"appid"\s*"(\d+)"', txt)
            name = re.search(r'"name"\s*"([^"]+)"', txt)
            if aid and name:
                games.append({"id": aid.group(1), "name": name.group(1)})
        except Exception:
            pass
    return sorted(games, key=lambda x: x["name"].lower())


@app.get("/api/steam/games")
def steam_games_list():
    if not IS_MAC:
        return jsonify(ok=False, msg="Только для macOS")
    base = find_steam()
    if not base:
        return jsonify(ok=False, msg="Steam не найден. Установи Steam.")
    games = steam_games()
    if not games:
        return jsonify(ok=False, msg="Игры не найдены (библиотека пуста)")
    return jsonify(ok=True, games=games)


@app.post("/api/steam/run")
def steam_run():
    if not IS_MAC:
        return jsonify(ok=False, msg="Только для macOS")
    appid = re.sub(r"\D", "", str((request.json or {}).get("id", "")))
    if not appid:
        return jsonify(ok=False, msg="Нет ID игры")
    try:
        subprocess.Popen(["open", f"steam://rungameid/{appid}"])
    except Exception as e:
        return jsonify(ok=False, msg=str(e)[:100])
    return jsonify(ok=True)


# ============================================================
#   Запуск программ
# ============================================================
APPS = {
    "TextEdit": ["open", "-a", "TextEdit"],
    "Калькулятор": ["open", "-a", "Calculator"],
    "Просмотр": ["open", "-a", "Preview"],
    "Finder": ["open", "-a", "Finder"],
    "Терминал": ["open", "-a", "Terminal"],
    "Настройки": ["open", "x-apple.systempreferences:"],
    "Safari": ["open", "-a", "Safari"],
    "Chrome": ["open", "-a", "Google Chrome"],
    "Скриншот": ["screencapture", "-i", os.path.join(HOME, "Desktop", "screenshot.png")],
    "Браузер": "https://www.google.com",
}


@app.get("/api/apps")
def apps_list():
    return jsonify(list(APPS))


@app.post("/api/apps/run")
def apps_run():
    a = APPS.get(str((request.json or {}).get("name", "")))
    if not a:
        return jsonify(ok=False, msg="Нет такой программы")
    try:
        if isinstance(a, str):
            webbrowser.open(a)
        else:
            subprocess.Popen(a)
    except Exception as e:
        return jsonify(ok=False, msg="Не запустилось: " + str(e)[:80])
    return jsonify(ok=True)


@app.post("/api/notify")
def notify():
    d = request.json or {}
    text = str(d.get("text", "")).strip()[:300] or "Тебя зовут!"
    beep = bool(d.get("beep"))

    def run():
        if beep:
            try:
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=5)
            except Exception:
                pass
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        osa(f'display dialog "{safe}" with title "Сообщение с телефона" buttons {{"OK"}} default button 1 with icon note')

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/cmd/run")
def cmd_run():
    code = (request.json or {}).get("code", "").strip()
    if not code:
        return jsonify(ok=False, msg="Пусто")
    try:
        r = subprocess.run(["/bin/zsh", "-c", code], shell=False,
                           capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, msg="Время вышло (20 секунд)")
    out = (r.stdout + r.stderr).decode("utf-8", "replace")[-8000:]
    return jsonify(ok=True, out=out)


@app.post("/api/cmd")
def cmd():
    code = (request.json or {}).get("code", "").strip()
    safe = code.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Terminal"
    activate
    do script "{safe}"
end tell
'''
    r = osa(script, timeout=10)
    if r.returncode != 0:
        return jsonify(ok=False, msg=r.stderr.decode("utf-8", "replace")[:200])
    return jsonify(ok=True)


# ============================================================
#   Заморозка экрана
# ============================================================
FREEZE = {"stop": None}
FREEZE_LOCK = threading.Lock()


def freeze_worker(stop, timeout, info, ready):
    root = photo = lbl = None
    try:
        import tkinter as tk
        from PIL import ImageTk
        img, mon = grab_screen()
        root = tk.Tk()
        root.overrideredirect(True)
        root.geometry(f"{img.width}x{img.height}+{mon['left']}+{mon['top']}")
        root.attributes("-topmost", True)
        root.configure(bg="black", cursor="none")
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(root, image=photo, borderwidth=0, cursor="none")
        lbl.place(x=0, y=0, width=img.width, height=img.height)
        root.update()
        info["hidden"] = False
        presses = []

        def on_esc(_e):
            now = time.time()
            presses[:] = [t for t in presses if now - t < 2] + [now]
            if len(presses) >= 3:
                stop.set()

        root.bind("<Escape>", on_esc)
        root.focus_force()
        t_end = time.time() + timeout

        def tick():
            if stop.is_set() or time.time() > t_end:
                root.quit()
                return
            root.attributes("-topmost", True)
            root.after(300, tick)

        root.after(300, tick)
        info["ok"] = True
        ready.set()
        root.mainloop()
    except Exception as e:
        info.setdefault("err", "Не удалось заморозить: " + str(e)[:100])
    finally:
        lbl = photo = None
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        stop.set()
        with FREEZE_LOCK:
            if FREEZE["stop"] is stop:
                FREEZE["stop"] = None
        import gc
        gc.collect()
        ready.set()


@app.post("/api/freeze/start")
def freeze_start():
    d = request.json or {}
    if d.get("unlimited"):
        seconds = 12 * 3600 * 100
    else:
        try:
            minutes = min(max(float(d.get("minutes", 10)), 0.1), 24 * 60)
        except (TypeError, ValueError):
            minutes = 10
        seconds = minutes * 60
    with FREEZE_LOCK:
        cur = FREEZE["stop"]
        if cur is not None and not cur.is_set():
            return jsonify(ok=True, already=True)
        stop, info, ready = threading.Event(), {}, threading.Event()
        FREEZE["stop"] = stop
    threading.Thread(target=freeze_worker, args=(stop, seconds, info, ready), daemon=True).start()
    ready.wait(8)
    if not info.get("ok"):
        stop.set()
        return jsonify(ok=False, msg=info.get("err", "Не удалось заморозить"))
    return jsonify(ok=True, hidden=info.get("hidden", False))


@app.post("/api/freeze/stop")
def freeze_stop():
    with FREEZE_LOCK:
        cur = FREEZE["stop"]
    if cur is not None:
        cur.set()
    return jsonify(ok=True)


@app.get("/api/freeze/status")
def freeze_status():
    cur = FREEZE["stop"]
    return jsonify(on=cur is not None and not cur.is_set())


# ============================================================
#   Картинки, буфер, URL
# ============================================================
@app.post("/api/image")
def image():
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False), 400
    ext = os.path.splitext(f.filename or "")[1].lower()
    if not re.fullmatch(r"\.(jpg|jpeg|png|gif|bmp|webp)", ext):
        return jsonify(ok=False, error="Только картинки"), 400
    path = os.path.join(UPLOAD_DIR, uuid.uuid4().hex + ext)
    f.save(path)
    subprocess.Popen(["open", path])
    return jsonify(ok=True)


@app.post("/api/clip/set")
def clip_set():
    text = str((request.json or {}).get("text", ""))[:100000]
    if text:
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        osa(f'set the clipboard to "{safe}"')
    return jsonify(ok=True)


@app.get("/api/clip/get")
def clip_get():
    r = osa('the clipboard as text', timeout=5)
    text = r.stdout.decode("utf-8", "replace").rstrip("\r\n") if r.returncode == 0 else ""
    return jsonify(text=text[:100000])


@app.post("/api/url")
def open_url():
    u = str((request.json or {}).get("url", "")).strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    if re.match(r"^https?://", u, re.I):
        subprocess.Popen(["open", u])
        return jsonify(ok=True)
    return jsonify(ok=False), 400


@app.post("/api/open/<name>")
def open_app(name):
    if name == "explorer":
        subprocess.Popen(["open", "-a", "Finder"])
        return jsonify(ok=True)
    return jsonify(ok=False), 404


# ============================================================
#   Фронтенд
# ============================================================
PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<title>Mac</title><link rel="manifest" href="/manifest.json"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#111"><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif;overscroll-behavior:none}
.wrap{padding:16px;max-width:600px;margin:auto}
button,.btnlink{background:#2b6cff;color:#fff;border:0;border-radius:10px;padding:12px 6px;font-size:15px;text-align:center;text-decoration:none;display:block;user-select:none;touch-action:manipulation}
button:active,.btnlink:active{filter:brightness(1.4)}
button.g{background:#333}button.r{background:#c92c2c}button.on{background:#2b6cff;outline:2px solid #fff}
.menu button{width:100%;margin:12px 0;padding:22px;font-size:19px}
input,textarea,select{width:100%;padding:11px;font-size:16px;border-radius:10px;border:1px solid #444;background:#222;color:#fff;margin:6px 0}
.sec{display:none}.sec.on{display:block}
#remote.on{display:flex;flex-direction:column;height:100dvh;background:#000}
#topbar{display:flex;gap:6px;padding:6px;background:#181818}#topbar button{padding:8px 14px}#topbar span{flex:1}
#stage{flex:1;min-height:0;display:flex;background:#000}
#imgbox{flex:1;min-width:0;display:flex;align-items:center;justify-content:center;touch-action:none}
#scr{max-width:100%;max-height:100%;user-select:none;-webkit-user-drag:none;pointer-events:none}
#strip{width:46px;background:#1b1b1b;border-left:1px solid #333;display:flex;align-items:center;justify-content:center;color:#888;font-size:22px;touch-action:none;text-align:center}
#tabs{display:flex;background:#181818}#tabs button{flex:1;border-radius:0;background:#222;padding:12px 2px;font-size:14px}
#tabs button.on{background:#2b6cff;outline:0}
#panel{height:40dvh;overflow:auto;background:#161616;padding:8px;padding-bottom:calc(8px + env(safe-area-inset-bottom))}
.pane{display:none}h4{margin:12px 0 4px;color:#9ab}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.grid.c2{grid-template-columns:repeat(2,1fr)}.grid.c4{grid-template-columns:repeat(4,1fr)}
.g2{display:flex;gap:6px}.g2>*{flex:1}.g2 input{margin:0}
.krow{display:flex;gap:4px;margin:4px 0}.krow button{flex:1;padding:12px 0;font-size:17px;min-width:0;background:#333}
.krow button.on{background:#2b6cff}
.scrollx{display:flex;gap:4px;overflow-x:auto;margin:4px 0}.scrollx button{flex:0 0 auto;min-width:48px;padding:11px 8px;background:#333}
.wrow{display:flex;gap:6px;align-items:center;margin:4px 0}.wrow span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}.wrow button{padding:8px 12px}
label{display:block;margin:8px 0;font-size:14px;color:#bbb}input[type=range]{padding:0}
#fab{display:none;position:fixed;top:8px;right:56px;z-index:7;gap:6px}#fab button{padding:8px 14px;background:rgba(40,40,40,.85);border:1px solid #555}
#remote.imm #topbar{display:none}#remote.imm #fab{display:flex}
#remote.imm #tabs,#remote.imm #panel{display:none!important}
#remote.imm.ov #tabs{display:flex!important;position:fixed;left:0;right:0;bottom:46dvh;z-index:6}
#remote.imm.ov #panel{display:block!important;position:fixed;left:0;right:0;bottom:0;height:46dvh;z-index:6}
#toast{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#333;padding:10px 18px;border-radius:20px;display:none;z-index:9}
#permbar{background:#4a3800;border:1px solid #6a5000;color:#ffd966;padding:10px;border-radius:10px;margin:10px 0;font-size:14px;display:none}
#permbar.on{display:block}#permbar button{padding:8px 12px;margin-top:8px;font-size:14px;background:#ffd966;color:#000}
</style></head><body>
<div id="toast"></div>

<div class="wrap sec on" id="login"><h2>Введи PIN</h2>
<input id="pin" type="password" autocomplete="off" placeholder="PIN"><button onclick="login()" style="width:100%">Войти</button></div>

<div class="wrap sec" id="menu"><h2>Управление Mac</h2>
<div id="permbar"><b>⚠ Нужны права macOS</b><div id="permtxt" style="margin-top:6px"></div>
<button onclick="openPerms()">Открыть Настройки → Права</button></div>
<div class="menu">
<button onclick="show('remote')">🖥 Дистанционное управление</button>
<button onclick="show('remote');fs(true)">⛶ Дистанционка на весь экран</button>
<button onclick="show('cmd')">⌨ Открыть Терминал</button>
<button onclick="show('img')">🖼 Открыть картинку</button>
<button onclick="show('cam')">📷 Камера</button>
<button onclick="show('sys')">📊 Состояние Mac</button>
<button onclick="show('files')">📁 Файлы</button>
<button onclick="show('apps')">🚀 Запуск программ</button>
<button onclick="show('steam')">🎮 Игры в Steam</button>
<button onclick="show('notify')">📢 Сообщение на Mac</button>
<button onclick="show('freeze')">🧊 Заморозка экрана</button>
<button class="r" onclick="win('close')">✖ Закрыть активное окно</button>
</div></div>

<div class="sec" id="remote">
 <div id="topbar"><button class="g" onclick="show('menu')">← Меню</button><span></span><button class="g" onclick="fs()">⛶ На весь экран</button></div>
 <div id="fab"><button onclick="toggleOv()">☰ Меню</button><button class="r" onclick="fs(false)">✕</button></div>
 <div id="stage"><div id="imgbox"><img id="scr" alt=""></div><div id="strip">⇅<br>⇅<br>⇅</div></div>
 <div id="tabs"><button data-t="mouse">🖱 Мышь</button><button data-t="kb">⌨ Клава</button><button data-t="pc">💻 Mac</button><button data-t="more">✨ Ещё</button></div>
 <div id="panel">
  <div class="pane" id="p_mouse">
   <div class="grid c4" id="mbtns"></div>
   <h4>Прокрутка</h4><div class="grid c4" id="sbtns"></div>
   <p style="color:#888;font-size:13px">Палец по экрану — двигает курсор. Тап — клик. Долгое нажатие — ПКМ. Два пальца — прокрутка.</p>
  </div>
  <div class="pane" id="p_kb">
   <input id="live" placeholder="Печатай здесь — символы сразу летят на Mac" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false">
   <div class="g2"><input id="msg" placeholder="Или целый текст"><button onclick="sendMsg()">Отпр.</button></div>
   <div class="krow" id="mods" style="margin-top:8px"></div>
   <div id="vk"></div>
   <div class="krow"><button id="lay" onclick="toggleLay()">EN/RU</button><button style="flex:3" onclick="sendKey({key:'space'})">Пробел</button><button onclick="sendKey({key:'backspace'})">⌫</button><button onclick="sendKey({key:'enter'})">⏎</button></div>
   <div class="scrollx" id="fkeys"></div>
   <div class="scrollx" id="nkeys"></div>
  </div>
  <div class="pane" id="p_pc">
   <h4>Активное окно</h4><div class="grid c4" id="wbtns"></div>
   <h4>Система</h4><div class="grid" id="cbtns"></div>
   <h4>Звук</h4><div class="grid c3" id="vbtns" style="grid-template-columns:repeat(3,1fr)"></div>
   <h4>Открытые окна <button class="g" style="display:inline-block;padding:4px 10px" onclick="loadWins()">⟳</button></h4><div id="wins"></div>
  </div>
  <div class="pane" id="p_more">
   <h4>Буфер обмена</h4><textarea id="clip" rows="2" placeholder="Текст"></textarea>
   <div class="g2"><button onclick="clipSet()">📤 На Mac</button><button class="g" onclick="clipGet()">📥 С Mac</button></div>
   <h4>Открыть сайт на Mac</h4><div class="g2"><input id="url" placeholder="example.com"><button onclick="openUrl()">Открыть</button></div>
   <h4>Скриншот</h4><a class="btnlink g" style="background:#333" href="/screenshot.png" download="screenshot.png">📷 Скачать скриншот</a>
   <h4>Настройки</h4>
   <label>Качество картинки<select id="qual"><option value="640,40">Лёгкое</option><option value="1000,55">Среднее</option><option value="1400,70">Чёткое</option><option value="1920,80">Максимум</option></select></label>
   <label>Частота<select id="spd"><option value="200">Экономно</option><option value="60">Нормально</option><option value="20">Максимум</option></select></label>
   <label>Скорость мыши <input type="range" id="gain" min="0.5" max="6" step="0.1"></label>
   <label>Сила прокрутки <input type="range" id="scrs" min="1" max="10" step="1"></label>
  </div>
 </div>
</div>

<div class="wrap sec" id="cam"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Камера Mac</h3>
<p id="camst" style="color:#9ab">Камера выключена. Разреши доступ к камере в Настройках.</p>
<div style="overflow:hidden;border-radius:10px;background:#000"><img id="camimg" alt="" style="width:100%;display:none"></div>
<div class="g2" style="margin-top:8px"><select id="camidx" style="margin:0" onchange="camChange()"><option value="0">Камера 0</option><option value="1">Камера 1</option><option value="2">Камера 2</option></select><button id="cambtn" onclick="camToggle()">▶ Включить</button></div>
<div class="grid" style="margin-top:8px"><button class="g" onclick="camMirror()">↔ Зеркало</button><button class="g" onclick="camFlip()">🔄 Перевернуть</button><button class="g" onclick="camSnap()">📸 Фото</button></div></div>

<div class="wrap sec" id="sys"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Состояние Mac</h3><div id="sysbox">Загрузка…</div>
<h4>Яркость экрана <span style="color:#ffd966;font-size:12px">(нужен `brew install brightness`)</span></h4><input type="range" id="bri" min="5" max="100" step="5"><div id="brit" style="color:#888;font-size:13px"></div>
<h4>Программы по памяти <button class="g" style="display:inline-block;padding:4px 10px" onclick="procLoad()">⟳</button></h4><div id="procs"></div></div>

<div class="wrap sec" id="files"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Файлы</h3><div id="fpath" style="color:#9ab;font-size:13px;word-break:break-all"></div><div id="flist"></div>
<h4>Отправить файлы на Mac</h4><input type="file" id="ufile" multiple><button style="width:100%" onclick="uploadFiles()">Отправить</button></div>

<div class="wrap sec" id="apps"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Запуск программ</h3><div class="grid c2" id="applist"></div></div>

<div class="wrap sec" id="steam"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Игры в Steam <button class="g" style="display:inline-block;padding:4px 10px" onclick="steamLoad(true)">⟳</button></h3>
<div id="steambox">Загрузка…</div></div>

<div class="wrap sec" id="notify"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Сообщение на Mac</h3><textarea id="ntext" rows="3" placeholder="Текст"></textarea>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="nbeep" checked style="width:auto;margin:0"> Со звуком</label>
<button style="width:100%" onclick="act('/api/notify',{text:val('ntext'),beep:$('nbeep').checked},'Показано')">📢 Показать на Mac</button></div>

<div class="wrap sec" id="freeze"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Заморозка экрана</h3>
<p style="color:#9ab">Поверх экрана Mac показывается заставка. На macOS нельзя скрыть её от показа экрана — на телефоне она будет видна.</p>
<label>На сколько<select id="fzmin"><option value="1">1 минута</option><option value="5">5 минут</option><option value="10" selected>10 минут</option><option value="30">30 минут</option><option value="60">60 минут</option><option value="0">Без ограничения</option></select></label>
<button id="fzbtn" style="width:100%;padding:22px;font-size:19px" onclick="fzToggle()">🧊 Заморозить</button>
<p id="fzst" style="color:#9ab"></p>
<p style="color:#888;font-size:13px">Разморозить — кнопкой, по таймеру, или Esc 3 раза на самом Mac.</p></div>

<div class="wrap sec" id="cmd"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Терминал</h3><textarea id="code" rows="3" placeholder="Команда"></textarea>
<button style="width:100%" onclick="post('/api/cmd',{code:val('code')},'Терминал открыт')">Открыть окно Терминала</button>
<button class="g" style="width:100%;margin-top:8px" onclick="cmdRun()">▶ Выполнить и показать здесь</button>
<div id="cchips" class="scrollx"></div>
<pre id="cout" style="white-space:pre-wrap;background:#000;padding:10px;border-radius:10px;font-size:12px;max-height:50vh;overflow:auto;display:none"></pre></div>

<div class="wrap sec" id="img"><button class="g" onclick="show('menu')">← Меню</button>
<h3>Открыть картинку</h3><input type="file" id="file" accept="image/*">
<button style="width:100%" onclick="upload()">Отправить и открыть</button></div>

<script>
const $=id=>document.getElementById(id),val=id=>$(id).value;
const cfg=Object.assign({gain:2,scroll:4,w:1000,q:55,delay:60},JSON.parse(localStorage.getItem('cfg')||'{}'));
const saveCfg=()=>{try{localStorage.setItem('cfg',JSON.stringify(cfg))}catch(e){}};
function toast(t){const e=$('toast');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',1700)}
async function post(u,b,msg,ms){const c=new AbortController(),to=setTimeout(()=>c.abort(),ms||6000);
 try{const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{}),signal:c.signal});
 if(r.status===401)toast('Сессия истекла — обнови страницу');else if(!r.ok)toast('Ошибка '+r.status);else if(msg)toast(msg);return r}
 catch(e){toast('Нет связи')}finally{clearTimeout(to)}}
let kq=Promise.resolve();
function sendKey(b){kq=kq.then(async()=>{const c=new AbortController(),to=setTimeout(()=>c.abort(),5000);
 try{const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),signal:c.signal});
  if(r.status===401)toast('Сессия истекла')}catch(e){}finally{clearTimeout(to)}})}
function grid(id,items){const c=$(id);for(const [l,f,cls] of items){const b=document.createElement('button');b.textContent=l;if(cls)b.className=cls;b.onclick=f;c.appendChild(b)}}

let live=false,curTab='mouse';
function show(n){onShow(n);if(n!=='cam'&&camOn)camStop();if(n!=='remote'&&$('remote').classList.contains('imm'))fs(false);
 for(const s of document.querySelectorAll('.sec'))s.classList.remove('on');$(n).classList.add('on');
 live=(n==='remote');if(live){frame();tab(curTab,true)}}
async function login(){const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:val('pin')})});
 if(r.ok)show('menu');else toast('Неверный PIN')}
fetch('/api/ping').then(r=>{if(r.ok)show('menu')}).catch(()=>{});
let fsApi=false;
function toggleOv(){$('remote').classList.toggle('ov')}
function fs(on){const r=$('remote'),d=document,el=d.documentElement;
 const want=(on===undefined)?!r.classList.contains('imm'):on;
 r.classList.toggle('imm',want);r.classList.remove('ov');
 try{if(want){const f=el.requestFullscreen||el.webkitRequestFullscreen;
   if(f){fsApi=true;const p=f.call(el);if(p&&p.then)p.then(()=>{try{screen.orientation.lock('landscape').catch(()=>{})}catch(e){}}).catch(()=>{})}}
  else{fsApi=false;if(d.fullscreenElement||d.webkitFullscreenElement)(d.exitFullscreen||d.webkitExitFullscreen).call(d);
   try{screen.orientation.unlock()}catch(e){}}}catch(e){}}
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&fsApi)fs(false)});

let tp=null;
document.addEventListener('touchstart',e=>{const b=e.target.closest&&e.target.closest('#panel button');
 tp=b?{b,x:e.touches[0].clientX,y:e.touches[0].clientY,t:Date.now(),moved:e.touches.length>1}:null},{passive:true});
document.addEventListener('touchmove',e=>{if(tp&&(Math.abs(e.touches[0].clientX-tp.x)>12||Math.abs(e.touches[0].clientY-tp.y)>12))tp.moved=true},{passive:true});
document.addEventListener('touchend',e=>{if(tp&&!tp.moved&&Date.now()-tp.t<700&&e.cancelable){e.preventDefault();tp.b.click()}tp=null},{passive:false});

const scr=$('scr');let busy=false;
function frame(){if(!live||busy)return;busy=true;scr.src=`/screen.jpg?w=${cfg.w}&q=${cfg.q}&t=${Date.now()}`}
scr.onload=()=>{busy=false;setTimeout(frame,cfg.delay)};
scr.onerror=()=>{busy=false;setTimeout(frame,700)};

function tab(n,keep){const same=(n===curTab);curTab=n;const p=$('panel');
 for(const b of document.querySelectorAll('#tabs button'))b.classList.toggle('on',b.dataset.t===n);
 for(const x of document.querySelectorAll('.pane'))x.style.display='none';$('p_'+n).style.display='block';
 if(n==='pc'){loadWins();fzStatus()}}
for(const b of document.querySelectorAll('#tabs button'))b.onclick=()=>{
 const p=$('panel');if(b.dataset.t===curTab&&p.style.display!=='none'){p.style.display='none';return}
 p.style.display='block';tab(b.dataset.t)};

function m(a,button,amount){post('/api/mouse',{action:a,button:button||'left',amount:amount||0})}
let pm={x:0,y:0},mt=null;
function mvAdd(dx,dy){pm.x+=dx;pm.y+=dy;if(!mt)mt=setTimeout(()=>{post('/api/mouse',{action:'move',dx:pm.x,dy:pm.y,gain:cfg.gain});pm={x:0,y:0};mt=null},25)}
let ps_=0,st=null;
function scAdd(n){ps_+=n;if(!st)st=setTimeout(()=>{st=null;const k=Math.trunc(ps_);if(k){ps_-=k;m('scroll',null,k)}},40)}
const avgY=t=>(t[0].clientY+t[1].clientY)/2;
let T={};const box=$('imgbox');
box.addEventListener('touchstart',e=>{e.preventDefault();const t=e.touches;
 if(t.length===1){clearTimeout(T.lp);T={mode:1,x:t[0].clientX,y:t[0].clientY,t0:Date.now(),moved:0,done:false};
  T.lp=setTimeout(()=>{if(T.mode===1&&T.moved<10){m('click','right');T.done=true;navigator.vibrate&&navigator.vibrate(30)}},550)}
 else if(t.length===2){clearTimeout(T.lp);T={mode:2,y:avgY(t),t0:Date.now(),moved:0}}},{passive:false});
box.addEventListener('touchmove',e=>{e.preventDefault();const t=e.touches;
 if(T.mode===1&&t.length===1){const dx=t[0].clientX-T.x,dy=t[0].clientY-T.y;T.x=t[0].clientX;T.y=t[0].clientY;T.moved+=Math.abs(dx)+Math.abs(dy);
  if(T.moved>=10)clearTimeout(T.lp);mvAdd(dx,dy)}
 else if(T.mode===2&&t.length===2){const y=avgY(t);T.moved+=Math.abs(y-T.y);scAdd((y-T.y)*cfg.scroll/25);T.y=y}},{passive:false});
box.addEventListener('touchend',e=>{e.preventDefault();clearTimeout(T.lp);
 if(e.touches.length===0){if(Date.now()-T.t0<300&&T.moved<10&&!T.done){if(T.mode===1)m('click');else if(T.mode===2)m('click','right')}T={}}},{passive:false});
let sy=null;const strip=$('strip');
strip.addEventListener('touchstart',e=>{e.preventDefault();sy=e.touches[0].clientY},{passive:false});
strip.addEventListener('touchmove',e=>{e.preventDefault();const y=e.touches[0].clientY;scAdd((y-sy)*cfg.scroll/12);sy=y},{passive:false});

let drag=false;
function toggleDrag(){drag=!drag;m(drag?'down':'up');const b=$('dragb');b.classList.toggle('on',drag);b.textContent=drag?'Отпустить':'Зажать'}
grid('mbtns',[['ЛКМ',()=>m('click'),'g'],['ПКМ',()=>m('click','right'),'g'],['Двойной',()=>m('double'),'g'],['СКМ',()=>m('click','middle'),'g']]);
{const b=document.createElement('button');b.id='dragb';b.className='g';b.textContent='Зажать';b.onclick=toggleDrag;b.style.gridColumn='span 4';$('mbtns').appendChild(b)}
const sc=n=>m('scroll',null,n*cfg.scroll/4);
grid('sbtns',[['⏫',()=>sc(25)],['🔼',()=>sc(6)],['🔽',()=>sc(-6)],['⏬',()=>sc(-25)]]);

const LAY={en:['1234567890','qwertyuiop','asdfghjkl','zxcvbnm'],ru:['1234567890','йцукенгшщзхъ','фывапролджэ','ячсмитьбю']};
const RU='йцукенгшщзхъфывапролджэячсмитьбю',EN="qwertyuiop[]asdfghjkl;'zxcvbnm,.";const R2E={};[...RU].forEach((c,i)=>R2E[c]=EN[i]);
const SYM=['.',',','!','?','-','_','=','+','/','\\','(',')','@','#','$','%','&','*','"',"'",':',';','<','>','[',']','{','}','~','`','^','|'];
let lay='en';const M={ctrl:0,alt:0,shift:0,win:0};
function pressChar(ch){const act=Object.keys(M).filter(k=>M[k]);
 if(!act.length)sendKey({text:ch});
 else if(act.length===1&&act[0]==='shift')sendKey({text:ch.toUpperCase()});
 else sendKey({combo:[...act,(R2E[ch]||ch).toLowerCase()]});
 if(act.length){for(const k in M)M[k]=0;drawMods()}}
function drawMods(){const c=$('mods');c.innerHTML='';for(const [k,l] of [['ctrl','Ctrl'],['alt','Alt'],['shift','Shift'],['win','⌘ Cmd']]){
 const b=document.createElement('button');b.textContent=l;b.className=M[k]?'on':'';b.onclick=()=>{M[k]=M[k]?0:1;drawMods()};c.appendChild(b)}}
function drawVk(){const c=$('vk');c.innerHTML='';
 for(const row of LAY[lay]){const d=document.createElement('div');d.className='krow';
  for(const ch of row){const b=document.createElement('button');b.textContent=ch;b.onclick=()=>pressChar(ch);d.appendChild(b)}c.appendChild(d)}
 const s=document.createElement('div');s.className='scrollx';
 for(const ch of SYM){const b=document.createElement('button');b.textContent=ch;b.onclick=()=>pressChar(ch);s.appendChild(b)}c.appendChild(s);
 $('lay').textContent=lay==='en'?'EN → RU':'RU → EN'}
function toggleLay(){lay=lay==='en'?'ru':'en';drawVk()}
drawMods();drawVk();
for(let i=1;i<=12;i++){const b=document.createElement('button');b.textContent='F'+i;b.onclick=()=>sendKey({key:'f'+i});$('fkeys').appendChild(b)}
for(const [l,k] of [['Esc','esc'],['Tab','tab'],['←','left'],['↑','up'],['↓','down'],['→','right'],['Home','home'],['End','end'],['PgUp','pageup'],['PgDn','pagedown'],['Del','delete']]){
 const b=document.createElement('button');b.textContent=l;b.onclick=()=>sendKey({key:k});$('nkeys').appendChild(b)}
let prev='';const live_=$('live');
live_.addEventListener('input',()=>{const cur=live_.value;let i=0;while(i<prev.length&&i<cur.length&&prev[i]===cur[i])i++;
 const del=prev.length-i,add=cur.slice(i);if(del||add)sendKey({bs:del,text:add});prev=cur;if(cur.length>300){live_.value='';prev=''}});
live_.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();sendKey({key:'enter'})}});
function sendMsg(){const t=val('msg');if(t){sendKey({text:t});$('msg').value=''}}

async function win(a){const r=await post('/api/win/'+a,{});if(!r||!r.ok)return;
 try{const j=await r.json();if(!j.ok)toast(j.msg||'Ошибка');else if(a==='close')toast('Закрыто')}catch(e){}}
const combo=c=>()=>sendKey({combo:c}),key=k=>()=>sendKey({key:k});
grid('wbtns',[['✖ Закрыть',()=>win('close'),'r'],['➖ Свернуть',()=>win('min'),'g'],['⬜ Развернуть',()=>win('max'),'g'],['↩ Вернуть',()=>win('restore'),'g']]);
grid('cbtns',[
 ['🗂 Mission Control',combo(['ctrl','up']),'g'],
 ['📁 Finder',()=>post('/api/open/explorer',{}),'g'],
 ['🔍 Spotlight',combo(['win','space']),'g'],
 ['✂ Скриншот',combo(['win','shift','3']),'g'],
 ['🔄 Обновить',combo(['win','r']),'g'],
 ['🧊 Заморозка',()=>fzToggle(),'g']
]);
grid('vbtns',[
 ['🔉',()=>post('/api/key',{volume:'down'}),'g'],
 ['🔇',()=>post('/api/key',{volume:'mute'}),'g'],
 ['🔊',()=>post('/api/key',{volume:'up'}),'g']
]);
async function loadWins(){try{const a=await (await fetch('/api/windows')).json();const c=$('wins');c.innerHTML='';
 for(const w of a){const d=document.createElement('div');d.className='wrow';const s=document.createElement('span');s.textContent=w.title;d.appendChild(s);
  const b1=document.createElement('button');b1.textContent='↗';b1.className='g';b1.onclick=()=>post('/api/windows/focus',{id:w.id});
  const b2=document.createElement('button');b2.textContent='✖';b2.className='r';b2.onclick=async()=>{await post('/api/windows/close',{id:w.id});setTimeout(loadWins,500)};
  d.append(b1,b2);c.appendChild(d)}}catch(e){}}

async function clipSet(){const t=val('clip');if(t)post('/api/clip/set',{text:t},'Скопировано')}
async function clipGet(){try{const r=await (await fetch('/api/clip/get')).json();$('clip').value=r.text;toast('Получено')}catch(e){toast('Ошибка')}}
function openUrl(){post('/api/url',{url:val('url')},'Открываю')}
$('qual').value=cfg.w+','+cfg.q;if(![...$('qual').options].some(o=>o.value===$('qual').value))$('qual').value='1000,55';
$('spd').value=String(cfg.delay);if($('spd').value!==String(cfg.delay))$('spd').value='60';
$('gain').value=cfg.gain;$('scrs').value=cfg.scroll;
$('qual').onchange=e=>{[cfg.w,cfg.q]=e.target.value.split(',').map(Number);saveCfg()};
$('spd').onchange=e=>{cfg.delay=+e.target.value;saveCfg()};
$('gain').oninput=e=>{cfg.gain=+e.target.value;saveCfg()};
$('scrs').oninput=e=>{cfg.scroll=+e.target.value;saveCfg()};
tab('mouse');

async function act(u,b,okmsg){const r=await post(u,b);if(!r||!r.ok)return;
 try{const j=await r.json();toast(j.ok?(okmsg||'Готово'):(j.msg||'Ошибка'))}catch(e){}}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize=b=>b>1e9?(b/1e9).toFixed(1)+' ГБ':b>1e6?(b/1e6).toFixed(1)+' МБ':b>1e3?Math.round(b/1e3)+' КБ':b+' Б';
const fmtUp=t=>{const d=Math.floor(t/86400),h=Math.floor(t%86400/3600),mi=Math.floor(t%3600/60);return (d?d+' д ':'')+h+' ч '+mi+' мин'};
let sysT=null,briDrag=false;
function onShow(n){clearInterval(sysT);
 if(n==='sys'){sysLoad();procLoad();sysT=setInterval(sysLoad,3000)}
 if(n==='files')fl('');
 if(n==='apps')appsLoad();
 if(n==='steam')steamLoad();
 if(n==='freeze')fzStatus()}

async function checkPerms(){try{const j=await (await fetch('/api/permissions/status')).json();
 if(!j.is_mac)return;const miss=[];
 if(!j.accessibility)miss.push('Универсальный доступ');
 if(!j.screen_recording)miss.push('Запись экрана');
 const bar=$('permbar');if(miss.length){bar.classList.add('on');$('permtxt').textContent='Не выдано: '+miss.join(', ')+'. Открой Настройки, добавь Terminal/Python и включи галочки, затем перезапусти скрипт.'}
 else{bar.classList.remove('on')}}catch(e){}}
async function openPerms(){await post('/api/permissions/open',{});toast('Панели прав открыты на Mac')}
setTimeout(checkPerms,1500);

let camOn=false,camBusy=false,camGen=0,camUrl=null,camMir=1,camRot=0,camN=0;const camimg=$('camimg');
function camTf(){camimg.style.transform='scaleX('+camMir+') rotate('+camRot+'deg)'}
function camMirror(){camMir=-camMir;camTf()}
function camFlip(){camRot=camRot?0:180;camTf()}
async function camLoop(g){let fails=0;
 while(camOn&&g===camGen){
  const c=new AbortController(),to=setTimeout(()=>c.abort(),5000);
  try{const r=await fetch('/camera.jpg?t='+Date.now(),{cache:'no-store',signal:c.signal});
   if(r.status===409){camStop(true);toast('Камера выключилась');return}
   if(r.ok){const u=URL.createObjectURL(await r.blob());camimg.src=u;if(camUrl)URL.revokeObjectURL(camUrl);camUrl=u;fails=0;camN++;
    if(camN===1)camimg.style.display='block';$('camst').textContent='🔴 Камера включена · кадров: '+camN}
   else{if(++fails>40){camStop();toast('Камера не отдаёт кадры');return}await sleep(300)}}
  catch(e){if(++fails>40){camStop();toast('Нет связи с камерой');return}await sleep(500)}
  finally{clearTimeout(to)}
  await sleep(40)}}
async function camStart(){if(camBusy)return;camBusy=true;
 try{$('camst').textContent='Запускаю камеру…';
  const r=await post('/api/camera/start',{index:+val('camidx')},null,20000);
  if(!r||!r.ok){$('camst').textContent='Камера выключена.';return}
  const j=await r.json();if(!j.ok){toast(j.msg||'Ошибка');$('camst').textContent='Камера выключена.';return}
  camGen++;camOn=true;camN=0;camTf();$('cambtn').textContent='⏹ Выключить';$('cambtn').className='r';
  $('camst').textContent='🔴 Камера включена, жду кадр…';camLoop(camGen)}
 finally{camBusy=false}}
function camStop(silent){const was=camOn;camOn=false;camGen++;camimg.style.display='none';
 if(camUrl){URL.revokeObjectURL(camUrl);camUrl=null}camimg.removeAttribute('src');
 $('cambtn').textContent='▶ Включить';$('cambtn').className='';$('camst').textContent='Камера выключена.';
 if(was&&!silent)post('/api/camera/stop',{})}
function camToggle(){camOn?camStop():camStart()}
function camChange(){if(camOn)camStart()}
async function camSnap(){if(!camOn){toast('Сначала включи камеру');return}
 try{const r=await fetch('/api/camera/snapshot');if(!r.ok){toast('Не получилось');return}
  const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download='camera.jpg';
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),5000)}catch(e){toast('Ошибка')}}
addEventListener('pagehide',()=>{if(camOn)navigator.sendBeacon('/api/camera/stop')});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&camOn)camStop()});

async function sysLoad(){try{const j=await (await fetch('/api/sysinfo')).json();
 if(!j.ok){$('sysbox').textContent=j.msg||'Ошибка';return}
 const bar=(l,p,t)=>'<div style="margin:8px 0"><div style="display:flex;justify-content:space-between;font-size:14px"><span>'+esc(l)+'</span><span>'+esc(t)+'</span></div><div style="background:#333;border-radius:6px;height:10px"><div style="background:'+(p>85?'#d33':'#2b6cff')+';width:'+p+'%;height:10px;border-radius:6px"></div></div></div>';
 let h=bar('Процессор',j.cpu,j.cpu+'%')+bar('Память',j.ram,j.ram_used+' / '+j.ram_total+' ГБ');
 if(j.battery)h+=bar('Батарея'+(j.battery.plugged?' ⚡':''),j.battery.percent,j.battery.percent+'%');
 for(const d of j.disks)h+=bar('Диск '+d.name,d.used,'свободно '+d.free_gb+' ГБ');
 h+='<div style="color:#888;font-size:13px;margin-top:8px">Mac работает: '+fmtUp(j.uptime)+'</div>';
 $('sysbox').innerHTML=h;
 if(j.brightness!=null&&!briDrag){$('bri').value=j.brightness;$('brit').textContent=j.brightness+'%'}}catch(e){}}
$('bri').addEventListener('input',()=>{briDrag=true;$('brit').textContent=$('bri').value+'%'});
$('bri').addEventListener('change',()=>{briDrag=false;act('/api/brightness',{value:+$('bri').value},'Яркость '+$('bri').value+'%')});
async function procLoad(){try{const j=await (await fetch('/api/procs')).json();const c=$('procs');c.innerHTML='';
 if(!j.ok){c.textContent=j.msg||'Ошибка';return}
 for(const p of j.procs){const d=document.createElement('div');d.className='wrow';const s=document.createElement('span');
  s.textContent=p.name+(p.n>1?' ×'+p.n:'')+' · '+p.mb+' МБ';
  const b=document.createElement('button');b.className='r';b.textContent='✖';
  b.onclick=async()=>{if(confirm('Завершить '+p.name+'?')){await act('/api/procs/kill',{name:p.name},'Завершено');setTimeout(procLoad,700)}};
  d.append(s,b);c.appendChild(d)}}catch(e){}}

async function fl(path){try{const j=await (await fetch('/api/files?path='+encodeURIComponent(path||''))).json();
 if(!j.ok){toast(j.msg||'Ошибка');return}
 $('fpath').textContent=j.path||'Выбери папку';const c=$('flist');c.innerHTML='';
 const mk=(t,cls,f)=>{const b=document.createElement('button');b.textContent=t;b.className=cls;b.onclick=f;return b};
 const row=(label,onTap,btns)=>{const d=document.createElement('div');d.className='wrow';const s=document.createElement('span');
  s.textContent=label;s.style.padding='10px 0';s.onclick=onTap;d.appendChild(s);for(const b of btns)d.appendChild(b);c.appendChild(d)};
 if(j.path)row('⬆ Назад',()=>fl(j.parent),[]);
 for(const it of j.items){
  if(it.dir)row('📁 '+it.name,()=>fl(it.path),[]);
  else row('📄 '+it.name+' · '+fmtSize(it.size),()=>{},[mk('⬇','g',()=>{location.href='/api/download?path='+encodeURIComponent(it.path)}),
   mk('↗','g',()=>act('/api/files/open',{path:it.path},'Открыто'))])}
 }catch(e){toast('Ошибка')}}
async function uploadFiles(){const list=$('ufile').files;if(!list.length){toast('Выбери файлы');return}
 let n=0;for(const f of list){const fd=new FormData();fd.append('file',f);try{const r=await fetch('/api/upload',{method:'POST',body:fd});if(r.ok)n++}catch(e){}}
 toast('Отправлено: '+n+' из '+list.length);$('ufile').value=''}

async function appsLoad(){const c=$('applist');if(c.childElementCount)return;
 try{const a=await (await fetch('/api/apps')).json();for(const n of a){const b=document.createElement('button');b.className='g';b.style.padding='18px 6px';
  b.textContent=n;b.onclick=()=>act('/api/apps/run',{name:n},'Запускаю: '+n);c.appendChild(b)}}catch(e){}}

let fzOn=false,fzHidden=true;
function fzDraw(){const b=$('fzbtn');b.textContent=fzOn?'☀ Разморозить':'🧊 Заморозить';b.className=fzOn?'r':'';
 $('fzst').textContent=fzOn?'❄ Экран заморожен':'Экран не заморожен.'}
async function fzStatus(){try{const j=await (await fetch('/api/freeze/status')).json();fzOn=j.on;fzDraw()}catch(e){}}
async function fzToggle(){
 if(fzOn){const r=await post('/api/freeze/stop',{});if(r&&r.ok){fzOn=false;fzDraw();toast('Разморожено')}return}
 const sel=val('fzmin');const body=sel==='0'?{unlimited:true}:{minutes:+sel};
 toast('Замораживаю…');
 const r=await post('/api/freeze/start',body,null,12000);if(!r||!r.ok)return;
 try{const j=await r.json();if(!j.ok){toast(j.msg||'Ошибка');return}
  fzOn=true;if(j.hidden!==undefined)fzHidden=j.hidden;fzDraw();toast('Экран заморожен')}catch(e){}}

let steamLoaded=false;
async function steamLoad(force){if(steamLoaded&&!force)return;const c=$('steambox');c.textContent='Загрузка…';
 try{const j=await (await fetch('/api/steam/games')).json();
  if(!j.ok){c.textContent=j.msg||'Ошибка';return}
  c.innerHTML='';for(const g of j.games){const b=document.createElement('button');b.className='g';b.style.cssText='width:100%;margin:6px 0;text-align:left;padding:14px';
   b.textContent='▶ '+g.name;b.onclick=()=>act('/api/steam/run',{id:g.id},'Запускаю: '+g.name);c.appendChild(b)}
  steamLoaded=true}catch(e){c.textContent='Ошибка'}}

for(const c of ['uname -a','ifconfig | grep inet','df -h','ps aux | head -20','whoami','sw_vers']){
 const b=document.createElement('button');b.textContent=c;b.onclick=()=>{$('code').value=c};$('cchips').appendChild(b)}
async function cmdRun(){const code=val('code').trim();if(!code){toast('Введи команду');return}
 const o=$('cout');o.style.display='block';o.textContent='Выполняю…';
 const r=await post('/api/cmd/run',{code},null,30000);if(!r||!r.ok){o.textContent='Ошибка связи';return}
 try{const j=await r.json();o.textContent=j.ok?(j.out||'(пусто)'):(j.msg||'Ошибка')}catch(e){o.textContent='Ошибка'}}

async function upload(){const f=$('file').files[0];if(!f){toast('Выбери файл');return}
 const fd=new FormData();fd.append('file',f);const r=await fetch('/api/image',{method:'POST',body:fd});toast(r.ok?'Открыто':'Ошибка')}
</script></body></html>"""


@app.get("/")
def index():
    return PAGE


@app.get("/manifest.json")
def manifest():
    return jsonify(name="Mac", short_name="Mac", start_url="/", display="fullscreen",
                   background_color="#111111", theme_color="#111111")


# ============================================================
#   Запуск
# ============================================================
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    print(f"\n  Платформа: {platform.system()} {platform.release()}")
    print(f"  Открой на телефоне: http://{local_ip()}:{PORT}")
    print(f"  PIN (всегда): {PIN}\n")

    if IS_MAC:
        print("  Открываю Настройки → Конфиденциальность и безопасность…")
        print("  Разреши для Терминала/Python:")
        print("    • Универсальный доступ (Accessibility)")
        print("    • Запись экрана (Screen Recording)")
        print("    • Управление компьютером (Automation)")
        print("    • Камера и Микрофон (если нужна камера)")
        print("  После выдачи прав ПЕРЕЗАПУСТИ этот скрипт.\n")
        try:
            open_privacy_panels()
        except Exception as e:
            print("  Не удалось открыть панели прав автоматически:", e)

    app.run(host="0.0.0.0", port=PORT, threaded=True)