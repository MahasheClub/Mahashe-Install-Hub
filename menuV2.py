# Mahashe Install Hub — всплывающие уведомления + авто-запуск от имени администратора.
# Зависимости: customtkinter, requests, beautifulsoup4, pywin32 (для ярлыков, опционально)

import os
import sys
import ctypes
import threading
import subprocess
import shutil
import time
import zipfile
import re
import json
import traceback
from urllib.parse import urlparse, unquote, urljoin
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

try:
    import winreg
except ImportError:
    winreg = None

# PyInstaller: обеспечить доступ к Tcl/Tk при "onefile" до импорта customtkinter
if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ.setdefault("TCL_LIBRARY", os.path.join(base, "tcl", "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(base, "tcl", "tk8.6"))
else:
    base = None

import customtkinter as ctk

# ---------------- ShellExecuteEx для .exe и runas ----------------

SEE_MASK_NOCLOSEPROCESS = 0x00000040


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


ShellExecuteExW = ctypes.windll.shell32.ShellExecuteExW
WaitForSingleObject = ctypes.windll.kernel32.WaitForSingleObject
GetExitCodeProcess = ctypes.windll.kernel32.GetExitCodeProcess
CloseHandle = ctypes.windll.kernel32.CloseHandle
INFINITE = 0xFFFFFFFF


def _shell_execute_wait(exe_path: str, params: str = "", verb: str | None = None) -> int:
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = verb
    sei.lpFile = exe_path
    sei.lpParameters = params
    sei.lpDirectory = os.path.dirname(exe_path) or None
    sei.nShow = 1
    if not ShellExecuteExW(ctypes.byref(sei)):
        raise OSError("ShellExecuteExW failed")
    try:
        WaitForSingleObject(sei.hProcess, INFINITE)
        code = ctypes.c_ulong(0)
        GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        CloseHandle(sei.hProcess)


APP_TITLE = "Mahashe Install Hub"
CACHE_DIR = os.path.join(os.environ.get("TEMP", os.getcwd()), "Mahashe-InstallHubPY")
LOG_PATH = os.path.join(CACHE_DIR, "installhub.log")
ZAPRET_DIR = r"C:\Windows\_Zapret"

SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
else:
    BUNDLE_DIR = SCRIPT_DIR

SOURCE_DIR = os.path.join(CACHE_DIR, "Source")

SCRIPT_SOURCE_DIR = os.path.join(SCRIPT_DIR, "Source")
SCRIPT_SOURCE_OLD_DIR = os.path.join(SCRIPT_DIR, "Source_old")
BUNDLE_SOURCE_DIR = os.path.join(BUNDLE_DIR, "Source")

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ZAPRET_DIR, exist_ok=True)
os.makedirs(SOURCE_DIR, exist_ok=True)

USER_APPS_COUNT = 0
CONFIG_STATE = "missing"  # missing | ok | invalid_json | invalid_schema
CONFIG_ERROR = ""

_log_lock = threading.Lock()
_error_lock = threading.Lock()
ERROR_LOG_PATH = None


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line, flush=True)


def _ensure_error_log_file():
    global ERROR_LOG_PATH
    if ERROR_LOG_PATH is None:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ERROR_LOG_PATH = os.path.join(SCRIPT_DIR, f"AppErrors.{ts}.txt")
    return ERROR_LOG_PATH


def log_error(msg: str):
    log("[ERROR] " + msg)
    try:
        path = _ensure_error_log_file()
        with _error_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def exception_hook(exctype, value, tb):
    text = "".join(traceback.format_exception(exctype, value, tb))
    log_error(f"UNHANDLED EXCEPTION:\n{text}")


sys.excepthook = exception_hook


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def elevate_if_needed():
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    params = subprocess.list2cmdline(sys.argv[1:])
    exe = sys.executable
    log(f"elevate_if_needed: re-launching with admin, exe={exe}, params={params}")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, SCRIPT_DIR, 1)
    if isinstance(rc, int) and rc > 32:
        sys.exit(0)


def first_url_ext(urls):
    for u in urls or []:
        p = u.split("?", 1)[0]
        ext = os.path.splitext(p)[1].lower()
        if ext:
            return ext
    return ""


def file_signature(path):
    if not os.path.exists(path):
        return "unknown"
    with open(path, "rb") as f:
        buf = f.read(512)
    if len(buf) >= 2 and buf[0] == 0x4D and buf[1] == 0x5A:
        return "exe"
    if len(buf) >= 2 and buf[0] == 0x50 and buf[1] == 0x4B:
        return "zip"
    if len(buf) >= 6 and buf[0] == 0x37 and buf[1] == 0x7A:
        return "7z"
    text = buf.decode("ascii", errors="ignore").lstrip().lower()
    if text.startswith("<!doctype") or text.startswith("<html"):
        return "html"
    return "unknown"


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name or "").strip().replace("\x00", "")
    allowed = " -_.()[]{}!@#$%^&+,=~"
    clean = "".join(ch for ch in name if ch.isalnum() or ch in allowed)
    return clean or "download.bin"


def _filename_from_cd(cd: str):
    if not cd:
        return None
    m = re.search(r"filename\*=\s*(?:UTF-8'')?(\"?)([^\";]+)\1", cd, re.I)
    if m:
        return unquote(m.group(2))
    m = re.search(r'filename=\s*"?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1)
    return None


def _name_from_url(u: str):
    path = urlparse(u).path
    base = os.path.basename(path)
    return unquote(base) if base else None


def download(urls, dest_path, retry=2, timeout=90):
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    fallback_name = os.path.basename(dest_path)
    log(f"download: dest_dir={dest_dir}, fallback_name={fallback_name}, urls={urls}")

    for u in urls:
        for i in range(retry + 1):
            try:
                log(f"download: GET {u}, attempt {i + 1}")
                with requests.get(
                    u,
                    headers={"User-Agent": "InstallHubPY"},
                    stream=True,
                    timeout=timeout,
                    allow_redirects=True,
                ) as r:
                    r.raise_for_status()

                    cd = r.headers.get("content-disposition", "")
                    name = (
                        _filename_from_cd(cd)
                        or _name_from_url(r.url)
                        or _name_from_url(u)
                        or fallback_name
                    )
                    name = _sanitize_filename(name)

                    if not os.path.splitext(name)[1] and os.path.splitext(fallback_name)[1]:
                        name += os.path.splitext(fallback_name)[1]

                    final_path = os.path.join(dest_dir, name)
                    tmp = final_path + ".part"
                    if os.path.exists(tmp):
                        os.remove(tmp)

                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 20):
                            if chunk:
                                f.write(chunk)

                os.replace(tmp, final_path)
                log(f"download: saved -> {final_path}")
                return final_path

            except Exception as e:
                log_error(f"download error [{u}] attempt={i + 1}: {e}")
                time.sleep(2 * i + 1)

    raise RuntimeError("Не удалось скачать ни по одному URL")


# Статический список VC++ 2005–2022 (все версии)
VC_REDISTS_STATIC = [
    # 2005
    {
        "name": "Visual C++ 2005 SP1 x86",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x86_2005_SP1.exe",  # заменишь на свои
    },
    {
        "name": "Visual C++ 2005 SP1 x64",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x64_2005_SP1.exe",
    },
    # 2008
    {
        "name": "Visual C++ 2008 SP1 x86",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x86_2008_SP1.exe",
    },
    {
        "name": "Visual C++ 2008 SP1 x64",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x64_2008_SP1.exe",
    },
    # 2010
    {
        "name": "Visual C++ 2010 SP1 x86",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x86_2010_SP1.exe",
    },
    {
        "name": "Visual C++ 2010 SP1 x64",
        "url": "https://download.microsoft.com/download/1/1/1/vcredist_x64_2010_SP1.exe",
    },
    # 2012 Update 4
    {
        "name": "Visual C++ 2012 Update 4 x86",
        "url": "https://download.microsoft.com/download/1/6/B/16B06F60-3B20-4FF2-B699-5E9B7962F9AE/vcredist_x86.exe",
    },
    {
        "name": "Visual C++ 2012 Update 4 x64",
        "url": "https://download.microsoft.com/download/1/6/B/16B06F60-3B20-4FF2-B699-5E9B7962F9AE/vcredist_x64.exe",
    },
    # 2013
    {
        "name": "Visual C++ 2013 x86",
        "url": "https://aka.ms/vs/12/release/vcredist_x86.exe",
    },
    {
        "name": "Visual C++ 2013 x64",
        "url": "https://aka.ms/vs/12/release/vcredist_x64.exe",
    },
    # 2015–2022 (единый пакет)
    {
        "name": "Visual C++ 2015–2022 x86",
        "url": "https://aka.ms/vs/17/release/vc_redist.x86.exe",
    },
    {
        "name": "Visual C++ 2015–2022 x64",
        "url": "https://aka.ms/vs/17/release/vc_redist.x64.exe",
    },
]


def install_vc_redist_all(interactive: bool = True) -> bool:
    if not VC_REDISTS_STATIC:
        log_error("vc_redist_all error: Список VC++ redistributables пуст — не задан ни один URL")
        return False

    any_ok = False

    for item in VC_REDISTS_STATIC:
        name = item.get("name") or "VC++"
        url = item.get("url") or ""
        if not url:
            log_error(f"vc_redist_all: пропуск {name} — пустой URL")
            continue

        try:
            base_name = os.path.basename(url.split("?", 1)[0]) or name
            base_name = _sanitize_filename(base_name)
            if not base_name.lower().endswith(".exe"):
                base_name += ".exe"

            dest = os.path.join(CACHE_DIR, base_name)
            log(f"vc_redist_all: скачивание {name} из {url} -> {dest}")
            dl_path = download([url], dest)
            sig = file_signature(dl_path)
            log(f"vc_redist_all: {name} сигнатура={sig}, путь={dl_path}")

            if sig == "html":
                log_error(f"vc_redist_all: {name}: вместо EXE скачан HTML — пропуск")
                continue
            if sig not in ("exe", "unknown"):
                log_error(f"vc_redist_all: {name}: неожиданный тип файла {sig} — попытка запуска как EXE")

            try:
                install_exe(dl_path, silent_args="/quiet /norestart", interactive=interactive)
                any_ok = True
            except Exception as e:
                log_error(f"vc_redist_all: ошибка установки {name}: {e}")
        except Exception:
            log_error(f"vc_redist_all: фатальная ошибка для {name}:\n{traceback.format_exc()}")

    if not any_ok:
        log_error("vc_redist_all: не удалось корректно установить ни один VC++ пакет")
    else:
        log("vc_redist_all: цикл установки VC++ завершён")

    return any_ok


def expand_any_archive(archive_path, destination):
    os.makedirs(destination, exist_ok=True)
    sig = file_signature(archive_path)
    log(f"expand_any_archive: path={archive_path}, dest={destination}, sig={sig}")
    if sig == "zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(destination)
            log("expand_any_archive: ZIP extracted ok")
            return
        except Exception as e:
            log_error(f"ZIP expand error: {e}")
    seven = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "7-Zip", "7z.exe")
    if os.path.exists(seven):
        cp = subprocess.run(
            [seven, "x", archive_path, f"-o{destination}", "-y"],
            capture_output=True,
            text=True,
        )
        log(f"expand_any_archive: 7z exit={cp.returncode}")
        if cp.returncode != 0:
            log_error(f"7z error: {cp.returncode} {cp.stderr[:300]}")
    else:
        shutil.copy2(archive_path, os.path.join(destination, os.path.basename(archive_path)))
        log("expand_any_archive: no 7z, copied archive as-is")


def install_msi(path, silent_args="/qn /norestart", interactive=True):
    if interactive:
        args = ["msiexec.exe", "/i", path]
    else:
        args = ["msiexec.exe", "/i", path, *silent_args.split()]
    log("install_msi RUN: " + " ".join(args))
    cp = subprocess.run(args)
    log(f"install_msi: exit={cp.returncode}")
    if cp.returncode != 0:
        log_error(f"MSI exit {cp.returncode}")
        raise RuntimeError(f"MSI exit {cp.returncode}")


def install_exe(path, silent_args="/S", interactive=True):
    if interactive or not silent_args or not silent_args.strip():
        args = [path]
    else:
        args = [path, silent_args]
    log("install_exe RUN: " + " ".join(args))
    cp = subprocess.run(args)
    log(f"install_exe: first run exit={cp.returncode}")
    if cp.returncode != 0:
        log(f"install_exe: retry GUI: {path}")
        cp2 = subprocess.run([path])
        log(f"install_exe: second run exit={cp2.returncode}")
        if cp2.returncode != 0:
            log_error(f"EXE exit {cp2.returncode}: {path}")
            raise RuntimeError(f"EXE exit {cp2.returncode}")


def have_winget():
    try:
        cp = subprocess.run(["winget", "--version"], capture_output=True, text=True)
        log(f"have_winget: exit={cp.returncode}, out={cp.stdout.strip()!r}")
        return cp.returncode == 0
    except Exception as e:
        log_error(f"have_winget error: {e}")
        return False


def winget_install(ids, interactive=True):
    if not have_winget():
        log("winget_install: winget not available")
        return False
    for pkg in ids:
        args = [
            "winget",
            "install",
            "--id",
            pkg,
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ]
        args += ["--interactive"] if interactive else ["--silent"]
        log("winget_install RUN: " + " ".join(args))
        cp = subprocess.run(args)
        log(f"winget_install: {pkg} exit={cp.returncode}")
        if cp.returncode == 0:
            return True
        log_error(f"winget error {pkg}: exit {cp.returncode}")
    return False


def create_shortcut(lnk_path, target, working_dir=None, icon=None, args=""):
    try:
        import win32com.client
    except Exception:
        log("create_shortcut: pywin32 не установлен — пропуск .lnk")
        return
    try:
        wsh = win32com.client.Dispatch("WScript.Shell")
        lnk = wsh.CreateShortcut(lnk_path)
        lnk.TargetPath = target
        if working_dir:
            lnk.WorkingDirectory = working_dir
        if icon:
            lnk.IconLocation = icon
        if args:
            lnk.Arguments = args
        lnk.WindowStyle = 1
        lnk.Save()
        log(f"create_shortcut: -> {lnk_path}")
    except Exception as e:
        log_error(f"Shortcut error: {e}")


def deploy_zapret(archive_path):
    os.makedirs(ZAPRET_DIR, exist_ok=True)
    log(f"deploy_zapret: archive={archive_path}, dest={ZAPRET_DIR}")
    expand_any_archive(archive_path, ZAPRET_DIR)
    desktops = []
    if os.environ.get("USERPROFILE"):
        desktops.append(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    desktops.append(os.path.join(os.environ.get("Public", r"C:\Users\Public"), "Desktop"))
    desktops = [d for d in desktops if os.path.isdir(d)]
    for d in desktops:
        lnk = os.path.join(d, "Zapret service.lnk")
        create_shortcut(
            lnk,
            os.path.join(ZAPRET_DIR, "service.bat"),
            ZAPRET_DIR,
            r"%SystemRoot%\System32\imageres.dll,13",
        )
    log(f"deploy_zapret: done, desktops={desktops}")


def deploy_v2rayn(archive_path):
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    target_root = os.path.join(progdata, "v2rayn")
    log(f"deploy_v2rayn: archive={archive_path}, dest={target_root}")
    expand_any_archive(archive_path, target_root)

    try:
        entries = [os.path.join(target_root, n) for n in os.listdir(target_root)]
        dirs = [p for p in entries if os.path.isdir(p)]
        files = [p for p in entries if os.path.isfile(p)]
        if len(dirs) == 1 and not files:
            inner = dirs[0]
            log(f"deploy_v2rayn: flatten inner dir {inner}")
            for name in os.listdir(inner):
                src = os.path.join(inner, name)
                dst = os.path.join(target_root, name)
                if os.path.exists(dst):
                    try:
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    except Exception:
                        pass
                shutil.move(src, dst)
            try:
                os.rmdir(inner)
            except Exception as e:
                log_error(f"deploy_v2rayn: rmdir inner error: {e}")
    except Exception as e:
        log_error(f"deploy_v2rayn: flatten error: {e}")

    exe_candidates = []
    for name in ("v2rayN.exe", "V2rayN.exe", "v2rayn.exe"):
        p = os.path.join(target_root, name)
        if os.path.isfile(p):
            exe_candidates.append(p)
    exe_path = exe_candidates[0] if exe_candidates else None

    desktops = []
    if os.environ.get("USERPROFILE"):
        desktops.append(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    desktops.append(os.path.join(os.environ.get("Public", r"C:\Users\Public"), "Desktop"))
    desktops = [d for d in desktops if os.path.isdir(d)]

    if exe_path:
        for d in desktops:
            lnk = os.path.join(d, "V2RayN.lnk")
            create_shortcut(
                lnk,
                exe_path,
                target_root,
                icon=exe_path,
            )
        try:
            os.startfile(exe_path)
            log(f"deploy_v2rayn: launched {exe_path}")
        except Exception as e:
            log_error(f"deploy_v2rayn: launch error {exe_path}: {e}")
    else:
        log_error("deploy_v2rayn: exe not found after extract")


CATALOG = [
    {
        "key": "v2rayn",
        "name": "V2RayN (Win64-cont)",
        "type": "zip",
        "silent": "",
        "urls": [
            "https://github.com/2dust/v2rayN/releases/download/7.14.9/v2rayN-windows-64-SelfContained.zip"
        ],
        "winget": ["2dust.v2rayN"],
    },
    {
        "key": "zapret",
        "name": "Zapret",
        "type": "zip",
        "silent": "",
        "urls": [
            "https://github.com/Flowseal/zapret-discord-youtube/releases/download/1.8.3/zapret-discord-youtube-1.8.3.zip"
        ],
        "winget": [],
    },
    {
        "key": "chrome",
        "name": "Google Chrome",
        "type": "msi",
        "silent": "/qn /norestart",
        "urls": [
            "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi"
        ],
        "winget": ["Google.Chrome"],
    },
    {
        "key": "discord",
        "name": "Discord",
        "type": "exe",
        "silent": "",
        "urls": ["https://discord.com/api/download?platform=win"],
        "winget": ["Discord.Discord"],
    },
    {
        "key": "vencord",
        "name": "Vencord",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://github.com/Vencord/Installer/releases/latest/download/VencordInstaller.exe"],
        "winget": ["Vencord.Installer", "Vencord.Vesktop", "Vencord.Vencord"],
    },
    {
        "key": "7zip",
        "name": "7-Zip",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://www.7-zip.org/a/7z2409-x64.exe"],
        "winget": ["7zip.7zip"],
    },
    {
        "key": "3utools",
        "name": "3uTools",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://url2.3u.com/MNBBfyaa"],
        "winget": ["3u.3uTools"],
    },
    {
        "key": "imazing",
        "name": "iMazing",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://downloads.imazing.com/windows/iMazing/iMazing3forWindows.exe"],
        "winget": ["DigiDNA.iMazing"],
    },
    {
        "key": "everything",
        "name": "Everything",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://www.voidtools.com/Everything-1.4.1.1023.x64-Setup.exe"],
        "winget": ["voidtools.Everything"],
    },
    {
        "key": "autohotkey",
        "name": "AutoHotkey v2",
        "type": "exe",
        "silent": "/S",
        "urls": ["https://www.autohotkey.com/download/ahk-v2.exe"],
        "winget": ["AutoHotkey.AutoHotkey"],
    },
    {
        "key": "qbittorrent",
        "name": "qBittorrent",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://downloads.sourceforge.net/project/qbittorrent/qbittorrent-win32/qbittorrent-4.6.6/qbittorrent_4.6.6_x64_setup.exe"
        ],
        "winget": ["qBittorrent.qBittorrent"],
    },
    {
        "key": "qttabbar",
        "name": "QtTabBar Setup",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://mahashe.su/Files/QtTabBarSetup.exe"
        ],
        "winget": ["qttabbar.qttabbar"],
    },
    {
        "key": "Upscayl",
        "name": "Upscayl — апскейл фото",
        "type": "exe",
        "silent": "",
        "urls": [
            "https://github.com/upscayl/upscayl/releases/download/v2.15.0/upscayl-2.15.0-win.exe"
        ],
        "winget": [],
    },
    # WinRAR: установка по кастомному сценарию
    {
        "key": "winrarinstaller",
        "name": "WinRaR",
        "type": "custom",
        "silent": "",
        "urls": [],
        "winget": [],
    },
    {
        "key": "steam",
        "name": "Steam",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://cdn.cloudflare.steamstatic.com/client/installer/SteamSetup.exe"
        ],
        "winget": ["Valve.Steam"],
    },
    {
        "key": "rockstar",
        "name": "Rockstar Games Launcher",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://gamedownloads.rockstargames.com/public/installer/Rockstar-Games-Launcher.exe"
        ],
        "winget": ["RockstarGames.RockstarGamesLauncher"],
    },
    {
        "key": "crossout",
        "name": "Crossout Launcher",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://yupmaster.gaijinent.com/launcher/current.php?id=CrossoutLauncher"
        ],
        "winget": [],
    },
    {
        "key": "epic",
        "name": "Epic Games Launcher",
        "type": "msi",
        "silent": "/qn /norestart",
        "urls": [
            "https://launcher-public-service-prod06.ol.epicgames.com/launcher/api/installer/download/EpicGamesLauncherInstaller.msi"
        ],
        "winget": ["EpicGames.EpicGamesLauncher"],
    },
    {
        "key": "uplay",
        "name": "Ubisoft Connect",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://ubistatic3-a.akamaihd.net/orbit/launcher_installer/UbisoftConnectInstaller.exe"
        ],
        "winget": ["Ubisoft.Connect"],
    },
    {
        "key": "notepadpp",
        "name": "Notepad++",
        "type": "exe",
        "silent": "/S",
        "urls": [
            "https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.8.8/npp.8.8.8.Installer.x64.exe"
        ],
        "winget": ["Notepad++.Notepad++"],
    },
    {
        "key": "vscode",
        "name": "Visual Studio Code",
        "type": "exe",
        "silent": "/VERYSILENT /NORESTART /MERGETASKS=addcontextmenufiles,addcontextmenufolders,addtopath",
        "urls": [
            "https://code.visualstudio.com/sha/download?build=stable&os=win32-x64-user"
        ],
        "winget": ["Microsoft.VisualStudioCode"],
    },
    {
        "key": "wfdownloader",
        "name": "WF Downloader",
        "type": "exe",
        "silent": "",
        "urls": [
            "https://mahashe.su/Files/WFDownloaderApp-BETA-64bit.exe"
        ],
        "winget": [],
    },
    {
        "key": "coubarchiver",
        "name": "Coub Archiver",
        "type": "exe",
        "silent": "",
        "urls": [
            "https://mahashe.su/Files/coub_archiver_v23.exe"
        ],
        "winget": [],
    },
    {
        "key": "YouTubeToMP3",
        "name": "YouTube To MP3",
        "type": "exe",
        "silent": "",
        "urls": [
            "https://mahashe.su/Files/YouTubeToMP3-x64.exe"
        ],
        "winget": [],
    },
    {
        "key": "filezilla",
        "name": "FileZilla (open site)",
        "type": "link",
        "silent": "",
        "urls": ["https://filezilla-project.org/download.php?type=client"],
        "winget": [],
    },
    # Компоненты
    {
        "key": "vc_redist_all",
        "name": "VC++ 2005–2022 (все пакеты)",
        "type": "custom",
        "silent": "",
        "urls": [],
        "winget": [],
    },
    {
        "key": "webview2",
        "name": "Microsoft WebView2",
        "type": "exe",
        "silent": "/silent /install",
        "urls": ["https://go.microsoft.com/fwlink/p/?LinkId=2124703"],
        "winget": [],
    },
    {
        "key": "java_site",
        "name": "Java — открыть сайт",
        "type": "link",
        "silent": "",
        "urls": ["https://www.java.com/ru/download/"],
        "winget": [],
    },
    {
        "key": "hevc",
        "name": "Кодеки HEVC (APPX)",
        "type": "custom",
        "silent": "",
        "urls": [
            "https://mahashe.su/Files/Microsoft.HEVCVideoExtension_2.0.53348.0_x64__8wekyb3d8bbwe.Appx"
        ],
        "winget": [],
    },
    {
        "key": "dotnet_sdk_7_0_102",
        "name": ".NET SDK 7.0.102",
        "type": "exe",
        "silent": "/quiet /norestart",
        "urls": ["https://mahashe.su/Files/dotnet-sdk-7.0.102-win-x64.exe"],
        "winget": ["Microsoft.DotNet.SDK.7"],
    },
    {
        "key": "dotnet_runtime_6_0_13",
        "name": ".NET Runtime 6.0.13",
        "type": "exe",
        "silent": "/quiet /norestart",
        "urls": ["https://mahashe.su/Files/dotnet-runtime-6.0.13-win-x64.exe"],
        "winget": ["Microsoft.DotNet.Runtime.6"],
    },
    {
        "key": "netfx3_wif",
        "name": ".NET Framework 3.5 + WIF 3.5",
        "type": "custom",
        "silent": "",
        "urls": [],
        "winget": [],
    },
    {
        "key": "python_3_12_0",
        "name": "Python 3.12.0",
        "type": "exe",
        "silent": "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0",
        "urls": ["https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe"],
        "winget": [],
    },
    {
        "key": "py_modules",
        "name": "Python модули By Mahashe",
        "type": "custom",
        "silent": "",
        "urls": [],
        "winget": [],
    },
]

GROUPS = [
    {
        "title": "Программы",
        "keys": [
            "chrome",
            "discord",
            "vencord",
            "3utools",
            "imazing",
            "qbittorrent",
            "Upscayl",
            "notepadpp",
            "vscode",
        ],
    },
    {
        "title": "Лаунчеры",
        "keys": ["crossout", "steam", "rockstar", "epic", "uplay"],
    },
    {
        "title": "Утилиты",
        "keys": [
            "7zip",
            "v2rayn",
            "zapret",
            "qttabbar",
            "winrarinstaller",
            "everything",
            "autohotkey",
            "wfdownloader",
            "coubarchiver",
            "YouTubeToMP3",
        ],
    },
    {
        "title": "Компоненты",
        "keys": [
            "vc_redist_all",
            "webview2",
            "java_site",
            "hevc",
            "dotnet_sdk_7_0_102",
            "dotnet_runtime_6_0_13",
            "netfx3_wif",
            "python_3_12_0",
            "py_modules",
        ],
    },
    {"title": "Пользовательские", "keys": []},
]


def get_app_by_key(key):
    for a in CATALOG:
        if a["key"].lower() == str(key).lower():
            return a
    return None


SELECTED_PY_MODULES_FILE = None


def detect_py_modules_file():
    bases = []

    if os.path.isdir(BUNDLE_SOURCE_DIR):
        bases.append(BUNDLE_SOURCE_DIR)

    for b in (SCRIPT_SOURCE_DIR, SCRIPT_SOURCE_OLD_DIR, SOURCE_DIR):
        if os.path.isdir(b):
            bases.append(b)

    seen = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        path = os.path.join(base, "python-modules.txt")
        if os.path.isfile(path):
            log(f"detect_py_modules_file: found at {path}")
            return path

    log("detect_py_modules_file: not found")
    return None


def get_py_modules_file():
    return SELECTED_PY_MODULES_FILE or detect_py_modules_file()


# ---------------- WinRAR интеграция ----------------


def _resolve_rarreg_source() -> str:
    base_file = get_py_modules_file()
    candidates = []
    if base_file:
        base_dir = os.path.dirname(base_file)
        candidates.append(os.path.join(base_dir, "rarreg.key"))
    candidates.append(os.path.join(SCRIPT_SOURCE_DIR, "rarreg.key"))
    candidates.append(os.path.join(SCRIPT_SOURCE_OLD_DIR, "rarreg.key"))
    candidates.append(os.path.join(BUNDLE_SOURCE_DIR, "rarreg.key"))
    for p in candidates:
        if p and os.path.isfile(p):
            log(f"[winrar] rarreg.key found at {p}")
            return p
    raise FileNotFoundError("rarreg.key не найден рядом с python-modules.txt или в Source/Source_old")


def detect_winrar_icon() -> str | None:
    base_file = get_py_modules_file()
    if not base_file:
        return None
    base_dir = os.path.dirname(base_file)
    try:
        for name in os.listdir(base_dir):
            lower = name.lower()
            if lower.endswith(".ico") and "winrar" in lower:
                path = os.path.join(base_dir, name)
                if os.path.isfile(path):
                    log(f"[winrar] icon found at {path}")
                    return path
    except Exception as e:
        log_error(f"[winrar] detect_winrar_icon error: {e}")
    return None


def get_latest_russian_winrar_url() -> str:
    url = "https://www.rarlab.com/download.htm"
    log(f"[winrar] fetch: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    russian_links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text() or ""
        href = a["href"]
        if "Russian" in text and href.lower().endswith(".exe"):
            russian_links.append(href)
    if not russian_links:
        raise RuntimeError("Не найден русский установщик WinRAR.")
    full = urljoin(url, russian_links[0])
    log(f"[winrar] latest RU installer: {full}")
    return full


def download_winrar_russian() -> str:
    link = get_latest_russian_winrar_url()
    log(f"[winrar] download from {link}")
    r = requests.get(link, stream=True, timeout=60)
    r.raise_for_status()
    path = os.path.join(CACHE_DIR, "winrar_ru.exe")
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)
    log(f"[winrar] saved to {path}")
    return path


def _probe_install_dir_from_registry() -> str | None:
    if winreg is None:
        return None

    def _open(root, sub, acc=winreg.KEY_READ):
        try:
            return winreg.OpenKey(root, sub, 0, acc)
        except FileNotFoundError:
            return None

    def _read_str(key, name):
        if not key:
            return None
        try:
            val, _ = winreg.QueryValueEx(key, name)
            return str(val)
        except Exception:
            return None

    for hive in (r"SOFTWARE\WinRAR", r"SOFTWARE\WOW6432Node\WinRAR"):
        k = _open(winreg.HKEY_LOCAL_MACHINE, hive, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
        exepath = _read_str(k, "ExePath")
        if exepath and os.path.isfile(exepath):
            return os.path.dirname(exepath)

    k = _open(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WinRAR.exe",
        winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
    )
    p = _read_str(k, None) or _read_str(k, "Path")
    if p and os.path.isdir(p):
        return p

    def scan_uninstall(base):
        k0 = _open(winreg.HKEY_LOCAL_MACHINE, base, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
        if not k0:
            return None
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(k0, i)
                i += 1
            except OSError:
                break
            ks = _open(
                winreg.HKEY_LOCAL_MACHINE,
                base + "\\" + sub,
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            )
            dn = None
            try:
                dn, _ = winreg.QueryValueEx(ks, "DisplayName")
            except Exception:
                pass
            if dn and "WinRAR" in str(dn):
                loc = _read_str(ks, "InstallLocation")
                if loc and os.path.isdir(loc):
                    return loc
                un = _read_str(ks, "UninstallString")
                if un:
                    head = un.strip().strip('"').split()[0].strip('"')
                    if os.path.isfile(head):
                        return os.path.dirname(head)
        return None

    for base in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        loc = scan_uninstall(base)
        if loc:
            return loc

    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base_dir = os.environ.get(env)
        if base_dir:
            pth = os.path.join(base_dir, "WinRAR")
            if os.path.isdir(pth):
                return pth
    return None


def _wait_full_install(timeout_sec: int = 300) -> str:
    log("[winrar] waiting for uninstall.exe/WinRAR.exe...")
    start = time.time()
    while time.time() - start < timeout_sec:
        inst = _probe_install_dir_from_registry()
        if inst:
            u = os.path.join(inst, "uninstall.exe")
            w = os.path.join(inst, "WinRAR.exe")
            if os.path.isfile(u) and os.path.isfile(w):
                log(f"[winrar] install dir: {inst}")
                return inst
        time.sleep(2)
    raise TimeoutError("WinRAR: не дождался появления uninstall.exe/WinRAR.exe")


def _copy_rarreg(install_dir: str, src: str | None = None):
    src = src or _resolve_rarreg_source()
    dst = os.path.join(install_dir, "rarreg.key")
    shutil.copyfile(src, dst)
    log(f"[winrar] rarreg.key copied to {dst}")


def _delete_default_profiles():
    log("[winrar] deleting default profiles...")
    base = r"HKEY_CURRENT_USER\SOFTWARE\WinRAR\Profiles"
    for i in range(1, 6):
        subprocess.run(
            ["reg", "delete", fr"{base}\{i}", "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    log("[winrar] profiles 1..5 deleted")


def _runas_cmd(cmdline: str):
    log(f"[winrar] runas cmd: {cmdline}")
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = f"/c {cmdline}"
    sei.lpDirectory = None
    sei.nShow = 1
    if not ShellExecuteExW(ctypes.byref(sei)):
        raise OSError("ShellExecuteExW failed")
    try:
        WaitForSingleObject(sei.hProcess, INFINITE)
    finally:
        CloseHandle(sei.hProcess)


def swap_icons_exact(icon_path: str):
    if not os.path.isfile(icon_path):
        raise FileNotFoundError(icon_path)
    log(f"[winrar] swap icons -> {icon_path}")

    cmds = [
        fr'reg add "HKEY_LOCAL_MACHINE\SOFTWARE\Classes\WinRAR\DefaultIcon" /f /ve /t REG_SZ /d "{icon_path}"',
        fr'reg add "HKEY_CLASSES_ROOT\WinRAR\DefaultIcon" /f /ve /t REG_SZ /d "{icon_path}"',
        fr'reg add "HKEY_CLASSES_ROOT\WinRAR.ZIP\DefaultIcon" /f /ve /t REG_SZ /d "{icon_path}"',
        fr'reg add "HKEY_LOCAL_MACHINE\SOFTWARE\Classes\WinRAR.ZIP\DefaultIcon" /f /ve /t REG_SZ /d "{icon_path}"',
    ]
    for c in cmds:
        try:
            _runas_cmd(c)
        except Exception as e:
            log_error(f"[winrar] reg add failed: {e}")

    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        ie4u = os.path.join(sysroot, "System32", "ie4uinit.exe")
        if os.path.isfile(ie4u):
            subprocess.run(
                [ie4u, "-ClearIconCache"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        log_error(f"[winrar] icon cache refresh error: {e}")

    log("[winrar] icons updated")


def install_winrar_installer(installer_path: str, interactive: bool = True):
    log(f"[winrar] run installer: {installer_path}, interactive={interactive}")
    params = "" if interactive else "/S"
    last_err = None
    for verb in ("runas", None):
        try:
            _shell_execute_wait(installer_path, params, verb)
            log("[winrar] installer finished")
            return
        except Exception as e:
            last_err = e
            log_error(f"[winrar] ShellExecuteEx (verb={verb}) error: {e}")
    try:
        args = [installer_path] + ([params] if params else [])
        cp = subprocess.run(args)
        log(f"[winrar] fallback subprocess.run exit={cp.returncode}")
        if cp.returncode != 0:
            raise RuntimeError(f"WinRAR installer exit code {cp.returncode}")
    except Exception as e:
        raise RuntimeError(f"Не удалось запустить установщик WinRAR: {e or last_err}")


def install_winrar_full(interactive: bool = True):
    if os.name != "nt" or winreg is None:
        raise RuntimeError("WinRAR installer: только для Windows с winreg")
    installer = download_winrar_russian()
    install_winrar_installer(installer, interactive=interactive)
    install_dir = _wait_full_install(timeout_sec=300)
    try:
        _copy_rarreg(install_dir)
    except Exception as e:
        log_error(f"[winrar] rarreg.key error: {e}")
    try:
        _delete_default_profiles()
    except Exception as e:
        log_error(f"[winrar] delete profiles error: {e}")
    try:
        icon_path = detect_winrar_icon()
        if icon_path:
            swap_icons_exact(icon_path)
        else:
            log("[winrar] no icon near python-modules.txt, skip icon swap")
    except Exception as e:
        log_error(f"[winrar] icon swap error: {e}")
    try:
        os.remove(installer)
        log(f"[winrar] installer removed: {installer}")
    except Exception as e:
        log_error(f"[winrar] remove installer error: {e}")


# ---------------- .NET Framework 3.5 + WIF 3.5 ----------------


def install_netfx3_wif(interactive: bool = True) -> bool:
    cmds = [
        'DISM.exe /Online /Enable-Feature /FeatureName:NetFx3 /All /NoRestart',
        'DISM.exe /Online /Enable-Feature /FeatureName:Windows-Identity-Foundation /All /NoRestart',
    ]
    ok = True
    for cmd in cmds:
        log(f"netfx3_wif RUN: {cmd}")
        if interactive:
            cp = subprocess.run(cmd, shell=True)
        else:
            cp = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        log(f"netfx3_wif: exit={cp.returncode}")
        if cp.returncode not in (0, 3010):
            log_error(f"netfx3_wif error: cmd='{cmd}', exit={cp.returncode}")
            ok = False
    return ok


def is_app_available_for_install(app: dict) -> bool:
    key = (app.get("key") or "").lower()
    type_ = (app.get("type") or "").lower()
    urls = app.get("urls") or []
    winget_ids = app.get("winget") or []
    if isinstance(winget_ids, str):
        winget_ids = [winget_ids]

    if key == "py_modules":
        return get_py_modules_file() is not None

    if key in ("vc_redist_all", "winrarinstaller", "netfx3_wif"):
        return True

    if type_ == "link":
        return bool(urls)

    if type_ in ("exe", "msi", "zip", "custom"):
        if urls:
            return True
        if winget_ids:
            return True

    return False


def check_urls_status(urls):
    if not urls:
        return ("URLs: нет", 0, 0, "NO_URLS")

    total = len(urls)
    ok = 0
    unknown = 0
    fail = 0

    for u in urls:
        try:
            r = requests.head(
                u,
                allow_redirects=True,
                timeout=6,
                headers={"User-Agent": "InstallHubPY"},
            )
            code = r.status_code

            if not (200 <= code < 400):
                try:
                    r2 = requests.get(
                        u,
                        allow_redirects=True,
                        stream=True,
                        timeout=12,
                        headers={"User-Agent": "InstallHubPY"},
                    )
                    code = r2.status_code
                except Exception as e2:
                    unknown += 1
                    log(f"URL check GET error {u}: {e2}")
                    continue

            if 200 <= code < 400:
                ok += 1
            elif code in (404, 410, 451):
                fail += 1
                log(f"URL dead {u}: {code}")
            else:
                unknown += 1
                log(f"URL check unknown state {u}: {code}")
        except Exception as e:
            unknown += 1
            log(f"URL check HEAD error {u}: {e}")

    if ok > 0:
        if ok == total:
            status = "OK"
            line = f"URLs: {total}, живых: {ok} — OK"
        else:
            status = "PARTIAL"
            line = f"URLs: {total}, живых: {ok} — ЧАСТИЧНО"
    else:
        if fail > 0 and unknown == 0:
            status = "FAIL"
            line = f"URLs: {total}, живых: 0 — FAIL"
        else:
            status = "UNKNOWN"
            line = f"URLs: {total}, живых: 0 — UNKNOWN"

    return (line, ok, total, status)


def get_winget_installed_snapshot():
    if not have_winget():
        return ""
    try:
        cp = subprocess.run(["winget", "list"], capture_output=True, text=True)
        if cp.returncode != 0:
            log_error(f"get_winget_installed_snapshot: exit={cp.returncode}")
            return ""
        log("get_winget_installed_snapshot: snapshot captured")
        return cp.stdout or ""
    except Exception as e:
        log_error(f"get_winget_installed_snapshot error: {e}")
        return ""


def is_installed_via_winget(winget_ids, snapshot: str) -> bool:
    if not snapshot or not winget_ids:
        return False
    up = snapshot.upper()
    for pkg in winget_ids:
        if not pkg:
            continue
        if pkg.upper() in up:
            return True
    return False


def _paths_for_key(key: str):
    key = (key or "").lower()
    env = os.environ
    pf = env.get("ProgramFiles", r"C:\Program Files")
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = env.get("LOCALAPPDATA", "")
    appdata = env.get("APPDATA", "")
    progdata = env.get("ProgramData", r"C:\ProgramData")
    sysroot = env.get("SystemRoot", r"C:\Windows")

    paths = []

    if key == "chrome":
        paths.append(os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"))
        paths.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
    elif key == "discord":
        paths.append(os.path.join(local, "Discord"))
    elif key == "vencord":
        paths.append(os.path.join(appdata, "VencordDesktop"))
        paths.append(os.path.join(appdata, "Vencord"))
    elif key == "7zip":
        paths.append(os.path.join(pf, "7-Zip", "7z.exe"))
        paths.append(os.path.join(pf86, "7-Zip", "7z.exe"))
    elif key == "3utools":
        paths.append(os.path.join(pf86, "3uTools", "3uTools.exe"))
        paths.append(os.path.join(pf, "3uTools", "3uTools.exe"))
    elif key == "imazing":
        paths.append(os.path.join(pf, "DigiDNA", "iMazing 3", "iMazing 3.exe"))
    elif key == "everything":
        paths.append(os.path.join(pf, "Everything", "Everything.exe"))
        paths.append(os.path.join(pf86, "Everything", "Everything.exe"))
    elif key == "autohotkey":
        paths.append(os.path.join(pf, "AutoHotkey", "v2", "AutoHotkey.exe"))
        paths.append(os.path.join(pf86, "AutoHotkey", "v2", "AutoHotkey.exe"))
    elif key == "qbittorrent":
        paths.append(os.path.join(pf, "qBittorrent", "qbittorrent.exe"))
        paths.append(os.path.join(pf86, "qBittorrent", "qbittorrent.exe"))
    elif key == "qttabbar":
        paths.append(os.path.join(pf, "QTTabBar"))
        paths.append(os.path.join(pf86, "QTTabBar"))
    elif key == "upscayl":
        paths.append(os.path.join(local, "Programs", "Upscayl"))
        paths.append(os.path.join(pf, "Upscayl"))
    elif key == "winrarinstaller":
        paths.append(os.path.join(pf, "WinRAR", "WinRAR.exe"))
        paths.append(os.path.join(pf86, "WinRAR", "WinRAR.exe"))
    elif key == "steam":
        paths.append(os.path.join(pf, "Steam", "Steam.exe"))
        paths.append(os.path.join(pf86, "Steam", "Steam.exe"))
    elif key == "rockstar":
        paths.append(os.path.join(pf, "Rockstar Games", "Launcher", "Launcher.exe"))
    elif key == "epic":
        paths.append(
            os.path.join(
                pf,
                "Epic Games",
                "Launcher",
                "Portal",
                "Binaries",
                "Win64",
                "EpicGamesLauncher.exe",
            )
        )
    elif key == "uplay":
        paths.append(os.path.join(pf, "Ubisoft", "Ubisoft Game Launcher", "upc.exe"))
    elif key == "notepadpp":
        paths.append(os.path.join(pf, "Notepad++", "notepad++.exe"))
        paths.append(os.path.join(pf86, "Notepad++", "notepad++.exe"))
    elif key == "vscode":
        paths.append(os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"))
        paths.append(os.path.join(pf, "Microsoft VS Code", "Code.exe"))
    elif key == "zapret":
        paths.append(os.path.join(ZAPRET_DIR, "service.bat"))
    elif key == "v2rayn":
        paths.append(os.path.join(progdata, "v2rayn", "v2rayN.exe"))
    elif key in ("vc_runtime", "vc_redist_all"):
        paths.append(os.path.join(sysroot, "System32", "vcruntime140.dll"))
    elif key == "webview2":
        paths.append(os.path.join(pf, "Microsoft", "EdgeWebView", "Application"))
    elif key == "python_3_12_0":
        paths.append(os.path.join(pf, "Python312", "python.exe"))
        paths.append(os.path.join(pf86, "Python312", "python.exe"))
    elif key == "dotnet_sdk_7_0_102":
        paths.append(os.path.join(pf, "dotnet", "sdk", "7.0.102"))
    elif key == "dotnet_runtime_6_0_13":
        paths.append(os.path.join(pf, "dotnet", "shared", "Microsoft.NETCore.App", "6.0.13"))
    elif key == "netfx3_wif":
        paths.append(os.path.join(sysroot, "Microsoft.NET", "Framework", "v2.0.50727"))

    return paths


def _exists_any(paths):
    for p in paths or []:
        if not p:
            continue
        p = os.path.expandvars(p)
        if os.path.isfile(p) or os.path.isdir(p):
            return True
    return False


def is_app_installed(app: dict, winget_snapshot=None) -> bool:
    key = (app.get("key") or "").lower()
    type_ = (app.get("type") or "").lower()
    winget_ids = app.get("winget") or []
    if isinstance(winget_ids, str):
        winget_ids = [winget_ids]

    if _exists_any(_paths_for_key(key)):
        log(f"is_app_installed: key={key} -> True by filesystem")
        return True

    if type_ == "zip" and key not in ("zapret",):
        progdata = os.environ.get("ProgramData", r"C:\ProgramData")
        if _exists_any([os.path.join(progdata, key)]):
            log(f"is_app_installed: key={key} -> True by ProgramData folder")
            return True

    if winget_snapshot and winget_ids:
        if is_installed_via_winget(winget_ids, winget_snapshot):
            log(f"is_app_installed: key={key} -> True by winget")
            return True

    log(f"is_app_installed: key={key} -> False")
    return False


def reload_user_config():
    global USER_APPS_COUNT, CONFIG_STATE, CONFIG_ERROR, CATALOG

    CATALOG = [a for a in CATALOG if not a.get("_user_app")]

    group = None
    for g in GROUPS:
        if g.get("title") == "Пользовательские":
            group = g
            break
    if group is not None:
        group["keys"] = []

    USER_APPS_COUNT = 0
    CONFIG_ERROR = ""

    if not os.path.isfile(CONFIG_PATH):
        CONFIG_STATE = "missing"
        log("reload_user_config: config.json missing")
        return

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        CONFIG_STATE = "invalid_json"
        CONFIG_ERROR = str(e)
        log_error(f"reload_user_config: invalid JSON: {e}")
        return

    if isinstance(raw, dict):
        apps = raw.get("user_apps") or raw.get("apps") or raw.get("items")
    elif isinstance(raw, list):
        apps = raw
    else:
        CONFIG_STATE = "invalid_schema"
        CONFIG_ERROR = "Ожидается список или объект с ключом user_apps/apps/items"
        log_error("reload_user_config: invalid schema (root type)")
        return

    if not isinstance(apps, list):
        CONFIG_STATE = "invalid_schema"
        CONFIG_ERROR = "Поле user_apps/apps/items должно быть списком"
        log_error("reload_user_config: invalid schema (apps is not list)")
        return

    keys = []
    for item in apps:
        if not isinstance(item, dict):
            continue
        key = item.get("key") or item.get("id")
        name = item.get("name") or item.get("title")
        urls = item.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if not key or not name:
            continue
        type_ = (item.get("type") or "exe").lower()
        if type_ not in ("exe", "msi", "zip", "link", "custom"):
            type_ = "exe"
        silent = item.get("silent") or ""
        winget_ids = item.get("winget") or []
        if isinstance(winget_ids, str):
            winget_ids = [winget_ids]

        app = {
            "key": key,
            "name": name,
            "type": type_,
            "silent": silent,
            "urls": urls,
            "winget": winget_ids,
            "_user_app": True,
        }
        CATALOG.append(app)
        keys.append(key)

    if group is not None:
        group["keys"] = keys
    USER_APPS_COUNT = len(keys)
    CONFIG_STATE = "ok"
    log(f"reload_user_config: OK, user apps count={USER_APPS_COUNT}")


reload_user_config()


class ToastManager:
    def __init__(self, root):
        self.root = root
        self.active = []

    def show(self, title: str, text: str, ms=3000, width=340):
        log(f"Toast: {title} | {text}")
        win = ctk.CTkToplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.configure(fg_color="#0f1e47")
        frame = ctk.CTkFrame(
            win,
            fg_color="#0f1e47",
            corner_radius=12,
            border_width=1,
            border_color="#223265",
        )
        frame.pack(fill="both", expand=True)
        title_lbl = ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=13, weight="bold"))
        body_lbl = ctk.CTkLabel(frame, text=text, font=ctk.CTkFont(size=12))
        title_lbl.pack(anchor="w", padx=14, pady=(10, 2))
        body_lbl.pack(anchor="w", padx=14, pady=(0, 12))
        win.update_idletasks()
        h = frame.winfo_reqheight() + 2
        x = self.root.winfo_screenwidth() - width - 16
        y = self.root.winfo_screenheight() - 16 - (len(self.active) * (h + 10)) - h
        win.geometry(f"{width}x{h}+{x}+{y}")
        self.active.append(win)
        self._fade(win, 0.0, 0.98, step=0.08, delay=20)
        win.after(ms, lambda: self._dismiss(win))

    def _fade(self, win, a_from, a_to, step=0.05, delay=25):
        a = a_from + step
        if (step > 0 and a >= a_to) or (step < 0 and a <= a_to):
            try:
                win.attributes("-alpha", a_to)
            except Exception:
                pass
            return
        try:
            win.attributes("-alpha", a)
        except Exception:
            return
        win.after(delay, lambda: self._fade(win, a, a_to, step, delay))

    def _dismiss(self, win):
        if not win.winfo_exists():
            return
        self._fade(win, float(win.attributes("-alpha")), 0.0, step=-0.08, delay=20)
        win.after(220, lambda: self._destroy(win))

    def _destroy(self, win):
        if win in self.active:
            self.active.remove(win)
        try:
            win.destroy()
        except Exception:
            pass


_executor = ThreadPoolExecutor(max_workers=4)
_running = {}


def set_busy(key, busy: bool):
    ev = _running.get(key)
    if ev is None:
        ev = threading.Event()
        _running[key] = ev
    if busy:
        ev.set()
    else:
        ev.clear()
    log(f"set_busy: key={key}, busy={busy}")


def install_task(app_key, interactive=True, on_ok=None, on_fail=None):
    app = get_app_by_key(app_key)
    if not app:
        log_error(f"install_task: app not found key={app_key}")
        return
    log(f"install_task: START key={app_key}, interactive={interactive}")
    set_busy(app_key, True)
    success = False
    try:
        # winget как первый источник, если есть
        if app.get("winget"):
            log(f"install_task: trying winget for key={app_key}")
            if winget_install(app["winget"], interactive=interactive):
                log(f"install_task: key={app_key} installed via winget")
                success = True
                return

        # VC++ 2005–2022
        if app_key == "vc_redist_all":
            success = install_vc_redist_all(interactive=interactive)
            return

        # HEVC
        if app_key == "hevc":
            fname = "HEVC-extension.appx"
            dest = os.path.join(SOURCE_DIR, fname)
            dl = download(app["urls"], dest)
            if not os.path.isfile(dl):
                log_error(f"HEVC file not found after download: {dl}")
                return
            sig = file_signature(dl)
            log(f"install_task[hevc]: file={dl}, sig={sig}")
            if sig == "html":
                log_error("HEVC: downloaded HTML instead of APPX, fallback open URL")
                first_url = app["urls"][0] if app.get("urls") else ""
                if first_url:
                    try:
                        os.startfile(first_url)
                        log(f"install_task[hevc]: opened URL in browser: {first_url}")
                    except Exception as e:
                        log_error(f"HEVC fallback browser open error: {e}")
                return
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f'Add-AppxPackage -Path "{dl}"',
            ]
            log("install_task[hevc] RUN: " + " ".join(cmd))
            cp = subprocess.run(cmd, capture_output=True, text=True)
            log(f"install_task[hevc]: Add-AppxPackage exit={cp.returncode}")
            if cp.returncode == 0:
                success = True
            else:
                log_error(f"HEVC install error: {cp.stderr[:300]}")
            try:
                os.remove(dl)
                log("install_task[hevc]: temp file removed")
            except Exception as e:
                log_error(f"install_task[hevc]: temp remove error: {e}")
            return

        # Python модули
        if app_key == "py_modules":
            lst = get_py_modules_file()
            log(f"install_task[py_modules]: file={lst}")
            if lst:
                try:
                    python_cli = shutil.which("py") or sys.executable
                    if interactive:
                        cmd = [
                            "cmd.exe",
                            "/k",
                            python_cli,
                            "-m",
                            "pip",
                            "install",
                            "-r",
                            lst,
                        ]
                        log("install_task[py_modules] interactive RUN: " + " ".join(cmd))
                        subprocess.Popen(
                            cmd,
                            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                        )
                    else:
                        cmd = [python_cli, "-m", "pip", "install", "-r", lst]
                        log("install_task[py_modules] silent RUN: " + " ".join(cmd))
                        subprocess.Popen(
                            cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    success = True
                except Exception as e:
                    log_error(f"pip modules error: {e}")
            return

        # WinRAR
        if app_key == "winrarinstaller":
            try:
                install_winrar_full(interactive=interactive)
                success = True
            except Exception as e:
                log_error(f"install_task[winrar]: {e}")
            return

        # .NET Framework 3.5 + WIF 3.5
        if app_key == "netfx3_wif":
            success = install_netfx3_wif(interactive=interactive)
            return

        # Линки
        if app["type"] == "link":
            url = app["urls"][0] if app.get("urls") else ""
            log(f"install_task[{app_key}]: link type, url={url}")
            if url:
                try:
                    os.startfile(url)
                    success = True
                except Exception as e:
                    log_error(f"open link error {url}: {e}")
            return

        # Обычные exe/msi/zip
        ext = first_url_ext(app["urls"])
        if not ext:
            if app["type"] == "msi":
                ext = ".msi"
            elif app["type"] == "zip":
                ext = ".zip"
            else:
                ext = ".exe"
        fname = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in app["name"])
        dest = os.path.join(CACHE_DIR, fname + ext)
        log(f"install_task[{app_key}]: downloading to {dest}")
        dl = download(app["urls"], dest)
        sig = file_signature(dl)
        log(f"install_task[{app_key}]: downloaded, path={dl}, sig={sig}")
        if sig == "html":
            log_error(f"{app_key}: downloaded HTML instead of installer, fallback: open URL in browser")
            first_url = app["urls"][0] if app.get("urls") else ""
            if first_url:
                try:
                    os.startfile(first_url)
                    log(f"install_task[{app_key}]: opened URL in browser: {first_url}")
                except Exception as e:
                    log_error(f"fallback browser open error for {first_url}: {e}")
            return
        if sig == "exe" and os.path.splitext(dl)[1].lower() != ".exe":
            newp = os.path.splitext(dl)[0] + ".exe"
            os.replace(dl, newp)
            log(f"install_task[{app_key}]: rename to .exe -> {newp}")
            dl = newp

        if app["type"] == "msi":
            install_msi(dl, silent_args=app.get("silent") or "/qn /norestart", interactive=interactive)
            success = True
        elif app["type"] == "exe":
            install_exe(dl, silent_args=app.get("silent") or "/S", interactive=interactive)
            success = True
        elif app["type"] == "zip":
            if app_key.lower() == "zapret":
                deploy_zapret(dl)
            elif app_key.lower() == "v2rayn":
                deploy_v2rayn(dl)
            else:
                target = os.path.join(
                    os.environ.get("ProgramData", "C:\\ProgramData"),
                    app["key"],
                )
                expand_any_archive(dl, target)
            success = True
    except Exception:
        log_error(f"install_task crashed for key={app_key}:\n{traceback.format_exc()}")
    finally:
        set_busy(app_key, False)
        log(f"install_task: FINISH key={app_key}, success={success}")
        if success and on_ok:
            try:
                on_ok(app.get("name", app_key))
            except Exception as e:
                log_error(f"on_ok handler error for key={app_key}: {e}")
        if not success and on_fail:
            try:
                on_fail(app.get("name", app_key))
            except Exception as e:
                log_error(f"on_fail handler error for key={app_key}: {e}")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.BG = "#0c1533"
        self.CARD = "#0f1e47"
        self.BORDER = "#223265"

        self.title(APP_TITLE + (" — Без прав администратора" if not is_admin() else ""))
        self.geometry("1020x640")
        self.minsize(900, 540)
        self.configure(fg_color=self.BG)

        self.toast = ToastManager(self)
        self.silent_var = ctk.BooleanVar(value=False)

        top = ctk.CTkFrame(self, fg_color=self.CARD, corner_radius=0)
        top.pack(side="top", fill="x")

        title_lbl = ctk.CTkLabel(
            top,
            text=APP_TITLE,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title_lbl.pack(side="left", padx=14, pady=8)

        cfg_text = "config.json: нет"
        if CONFIG_STATE == "ok":
            cfg_text = f"config.json: {USER_APPS_COUNT} пользовательских приложений"
        elif CONFIG_STATE == "invalid_json":
            cfg_text = "config.json: ошибка JSON"
        elif CONFIG_STATE == "invalid_schema":
            cfg_text = "config.json: неверная схема"

        self.cfg_label = ctk.CTkLabel(
            top,
            text=cfg_text,
            font=ctk.CTkFont(size=12),
        )
        self.cfg_label.pack(side="left", padx=(10, 0))

        self.silent_switch = ctk.CTkSwitch(
            top,
            text="Тихая установка",
            variable=self.silent_var,
        )
        self.silent_switch.pack(side="right", padx=12)

        self.refresh_btn = ctk.CTkButton(
            top,
            text="Обновить статус",
            width=140,
            command=self.refresh_statuses,
        )
        self.refresh_btn.pack(side="right", padx=(0, 12), pady=6)

        main = ctk.CTkFrame(self, fg_color=self.BG, corner_radius=0)
        main.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.scroll = ctk.CTkScrollableFrame(
            main,
            fg_color=self.BG,
            corner_radius=0,
        )
        self.scroll.pack(fill="both", expand=True)

        self.app_widgets: dict[str, dict[str, ctk.CTkLabel | ctk.CTkButton]] = {}
        self._build_groups()

        self.after(600, self.refresh_statuses)

    def _build_groups(self):
        for group in GROUPS:
            g_frame = ctk.CTkFrame(
                self.scroll,
                fg_color=self.CARD,
                corner_radius=12,
                border_width=1,
                border_color=self.BORDER,
            )
            g_frame.pack(fill="x", pady=6)

            g_title = ctk.CTkLabel(
                g_frame,
                text=group["title"],
                font=ctk.CTkFont(size=15, weight="bold"),
            )
            g_title.pack(anchor="w", padx=12, pady=(8, 4))

            inner = ctk.CTkFrame(
                g_frame,
                fg_color="#111827",
                corner_radius=8,
            )
            inner.pack(fill="x", padx=8, pady=(0, 8))

            for key in group["keys"]:
                app = get_app_by_key(key)
                if not app:
                    continue

                row = ctk.CTkFrame(inner, fg_color="#111827")
                row.pack(fill="x", padx=6, pady=3)

                name_lbl = ctk.CTkLabel(
                    row,
                    text=app["name"],
                    anchor="w",
                )
                name_lbl.pack(side="left", padx=(4, 8), pady=4, fill="x", expand=True)

                status_lbl = ctk.CTkLabel(
                    row,
                    text="...",
                    width=110,
                    anchor="center",
                )
                status_lbl.pack(side="left", padx=4)

                btn = ctk.CTkButton(
                    row,
                    text="Установить",
                    width=130,
                    command=lambda k=key: self.handle_install(k),
                )
                btn.pack(side="right", padx=(4, 8), pady=3)

                self.app_widgets[key] = {
                    "status": status_lbl,
                    "button": btn,
                }

    def handle_install(self, key: str):
        app = get_app_by_key(key)
        if not app:
            self.toast.show("Ошибка", f"Элемент {key} не найден")
            return

        ev = _running.get(key)
        if ev is not None and ev.is_set():
            self.toast.show("Уже идёт", app["name"])
            return

        silent = self.silent_var.get()
        interactive = not silent

        def on_ok(name: str):
            self.toast.show("Готово", f"{name} установлено")
            self.after(1000, self.refresh_statuses)

        def on_fail(name: str):
            self.toast.show("Ошибка", f"{name} не установлено")
            self.after(1000, self.refresh_statuses)

        self.toast.show("Запущено", app["name"])
        _executor.submit(install_task, key, interactive, on_ok, on_fail)

    def refresh_statuses(self):
        snap = get_winget_installed_snapshot()
        for key, widgets in self.app_widgets.items():
            app = get_app_by_key(key)
            if not app:
                continue

            available = is_app_available_for_install(app)
            status_lbl: ctk.CTkLabel = widgets["status"]  # type: ignore
            btn: ctk.CTkButton = widgets["button"]  # type: ignore

            if not available:
                status_lbl.configure(text="недоступно", text_color="#f97316")
                btn.configure(state="disabled")
                continue

            installed = is_app_installed(app, snap)
            if installed:
                status_lbl.configure(text="установлено", text_color="#22c55e")
            else:
                status_lbl.configure(text="не установлено", text_color="#ef4444")

            btn.configure(state="normal")


def main():
    elevate_if_needed()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
