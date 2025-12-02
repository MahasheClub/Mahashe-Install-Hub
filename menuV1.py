# Mahashe Install Hub — всплывающие уведомления + авто-запуск от имени администратора.
# Зависимости: customtkinter, requests

import os, sys, ctypes, threading, subprocess, shutil, time, zipfile, re, json, traceback
from urllib.parse import urlparse, unquote
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import requests

# PyInstaller: обеспечить доступ к Tcl/Tk при "onefile" до импорта customtkinter
if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ.setdefault("TCL_LIBRARY", os.path.join(base, "tcl", "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(base, "tcl", "tk8.6"))
else:
    base = None

import customtkinter as ctk

APP_TITLE = "Mahashe Install Hub"
CACHE_DIR = os.path.join(os.environ.get("TEMP", os.getcwd()), "Mahashe-InstallHubPY")
LOG_PATH = os.path.join(CACHE_DIR, "installhub.log")
ZAPRET_DIR = r"C:\Windows\_Zapret"

# Надёжная база для exe (где лежит config.json и сам файл)
SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
# База для ресурсов (при frozen — _MEIPASS, в dev — SCRIPT_DIR)
if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
else:
    BUNDLE_DIR = SCRIPT_DIR

# Рабочие каталоги во временной директории
SOURCE_DIR = os.path.join(CACHE_DIR, "Source")  # всё временное теперь здесь

# Каталоги для поиска python-modules.txt
SCRIPT_SOURCE_DIR = os.path.join(SCRIPT_DIR, "Source")
SCRIPT_SOURCE_OLD_DIR = os.path.join(SCRIPT_DIR, "Source_old")
BUNDLE_SOURCE_DIR = os.path.join(BUNDLE_DIR, "Source")  # то самое %TEMP%\_MEIxxxx\Source

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ZAPRET_DIR, exist_ok=True)
os.makedirs(SOURCE_DIR, exist_ok=True)

# Путь к winget, если он вообще есть в системе
WINGET_PATH = shutil.which("winget")

USER_APPS_COUNT = 0  # количество пользовательских приложений из config.json
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
                log(f"download: GET {u}, attempt {i+1}")
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
                log_error(f"download error [{u}] attempt={i+1}: {e}")
                time.sleep(2 * i + 1)

    raise RuntimeError("Не удалось скачать ни по одному URL")


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
    args = ["msiexec.exe", "/i", path] if interactive else ["msiexec.exe", "/i", path, *silent_args.split()]
    log("install_msi RUN: " + " ".join(args))
    cp = subprocess.run(args)
    log(f"install_msi: exit={cp.returncode}")
    if cp.returncode != 0:
        log_error(f"MSI exit {cp.returncode}")
        raise RuntimeError(f"MSI exit {cp.returncode}")


def install_exe(path, silent_args="/S", interactive=True):
    args = [path] if interactive else [path, *([silent_args] if silent_args and silent_args.strip() else [])]
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
    """
    Проверка наличия winget без WinError 2:
    - сначала смотрим WINGET_PATH (shutil.which)
    - если его нет, просто пишем инфо-лог и возвращаем False
    """
    if not WINGET_PATH:
        log("have_winget: winget not found in PATH")
        return False
    try:
        cp = subprocess.run([WINGET_PATH, "--version"], capture_output=True, text=True)
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
            WINGET_PATH,
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

    # выпрямление вложенной папки вида v2rayN-windows-64-SelfContained
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
        "silent": "",
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
    {
        "key": "winrarinstaller",
        "name": "WinRaR",
        "type": "exe",
        "silent": "",
        "urls": [
            "https://mahashe.su/Files/Winrar-Installer.exe"
        ],
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
        "silent": "",
        "urls": ["https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.8.8/npp.8.8.8.Installer.x64.exe"],
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
        "key": "vc_runtime",
        "name": "VC++ Runtime",
        "type": "exe",
        "silent": "/quiet /norestart",
        "urls": [
            "https://mahashe.su/Files/VCR.exe"
        ],
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
    {"title": "Лаунчеры", "keys": ["crossout", "steam", "rockstar", "epic", "uplay"]},
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
            "vc_runtime",
            "webview2",
            "java_site",
            "hevc",
            "dotnet_sdk_7_0_102",
            "dotnet_runtime_6_0_13",
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


# выбранный пользователем файл с модулями (если есть)
SELECTED_PY_MODULES_FILE = None


def detect_py_modules_file():
    """
    Ищем python-modules.txt:
    - внутри распакованного архива PyInstaller: %TEMP%\\_MEIxxxx\\Source\\python-modules.txt
    - рядом с exe в папках Source и Source_old
    - во временной Source (на всякий случай)
    """
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


def is_app_available_for_install(app: dict) -> bool:
    key = (app.get("key") or "").lower()
    type_ = (app.get("type") or "").lower()
    urls = app.get("urls") or []
    winget_ids = app.get("winget") or []
    if isinstance(winget_ids, str):
        winget_ids = [winget_ids]

    if key == "py_modules":
        return get_py_modules_file() is not None

    if type_ == "link":
        return bool(urls)

    if type_ in ("exe", "msi", "zip", "custom"):
        if urls:
            return True
        if winget_ids:
            return True

    return False


def check_urls_status(urls):
    """
    Мягкая проверка реальных ссылок:
    - HEAD -> если не 2xx/3xx, пробуем GET
    - 404/410/451 считаем явно мёртвыми (FAIL)
    - всё остальное — UNKNOWN
    Логи отсюда не считаются критичными ошибками, поэтому пишем через log(), а не log_error().
    """
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
        cp = subprocess.run([WINGET_PATH, "list"], capture_output=True, text=True)
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
    elif key == "vc_runtime":
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

    # 1) проверка известных путей
    if _exists_any(_paths_for_key(key)):
        log(f"is_app_installed: key={key} -> True by filesystem")
        return True

    # 2) zip, распакованные в ProgramData\<key>
    if type_ == "zip" and key not in ("zapret",):
        progdata = os.environ.get("ProgramData", r"C:\ProgramData")
        if _exists_any([os.path.join(progdata, key)]):
            log(f"is_app_installed: key={key} -> True by ProgramData folder")
            return True

    # 3) winget snapshot
    if winget_snapshot and winget_ids:
        if is_installed_via_winget(winget_ids, winget_snapshot):
            log(f"is_app_installed: key={key} -> True by winget")
            return True

    log(f"is_app_installed: key={key} -> False")
    return False


def reload_user_config():
    global USER_APPS_COUNT, CONFIG_STATE, CONFIG_ERROR, CATALOG

    # срезаем старые пользовательские
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

        # HEVC: отдельная логика + фикс путей с пробелами
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
                log_error(f"HEVC temp remove error: {e}")
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

        # Линки: просто открываем сайт
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
        self.BLUE = "#1d4ed8"

        self.title(APP_TITLE + (" — Без прав администратора" if not is_admin() else ""))
        self.geometry("980x680")
        self.resizable(False, False)
        self.configure(fg_color=self.BG)

        self.toaster = ToastManager(self)
        self.user_cfg_button = None

        self.status_win = None
        self.status_tabs = None
        self.status_text_main = None
        self.status_text_ok = None
        self.status_text_fail = None

        self.tabs = None
        self.rows = {}
        self.sf_by_tab = {}
        self._cfg_last_sig = None

        top = ctk.CTkFrame(self, fg_color=self.BG, corner_radius=0)
        top.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(
            top,
            text="Установить всё",
            command=self.install_all,
            corner_radius=15,
        ).pack(side="left")
        ctk.CTkButton(
            top,
            text="Очистить кэш",
            command=self.clear_cache,
            corner_radius=15,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            top,
            text="Статус",
            command=self.show_status,
            corner_radius=15,
        ).pack(side="left")

        self._build_tabs()

        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind_all("<Button-4>", lambda e: self._scroll(-60), add="+")
        self.bind_all("<Button-5>", lambda e: self._scroll(60), add="+")
        self.after(1200, self.autorefresh)

        log("App: UI initialized")

    def toast_ok(self, appname: str):
        self.after(0, lambda: self.toaster.show("Установлено", appname, ms=3200))

    def toast_fail(self, appname: str):
        self.after(0, lambda: self.toaster.show("Ошибка установки", appname, ms=3500))

    def _card(self, parent):
        return ctk.CTkFrame(
            parent,
            fg_color=self.CARD,
            corner_radius=15,
            border_width=1,
            border_color=self.BORDER,
        )

    def _build_tabs(self):
        if self.tabs is not None:
            self.tabs.destroy()
        self.tabs = ctk.CTkTabview(
            self,
            corner_radius=15,
            border_width=1,
            border_color=self.BORDER,
            fg_color=self.BG,
            segmented_button_fg_color=self.CARD,
            segmented_button_selected_color=self.BLUE,
            segmented_button_selected_hover_color="#2563eb",
            segmented_button_unselected_color=self.CARD,
            segmented_button_unselected_hover_color="#142352",
            text_color="white",
        )
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.sf_by_tab.clear()
        self.rows.clear()

        for g in GROUPS:
            tab = self.tabs.add(g["title"])

            if g["title"] == "Пользовательские":
                continue

            sf = ctk.CTkScrollableFrame(
                tab,
                corner_radius=15,
                fg_color=self.BG,
                border_width=0,
                scrollbar_button_color=self.BG,
                scrollbar_button_hover_color=self.BG,
                scrollbar_fg_color=self.BG,
            )
            sf.pack(fill="both", expand=True, padx=4, pady=4)
            self.sf_by_tab[g["title"]] = sf
            self._populate_group(sf, g["keys"])

        self.refresh_user_apps_ui()
        log("App: tabs built")

    def _populate_group(self, parent, keys):
        for key in keys:
            app = get_app_by_key(key)
            if not app:
                log_error(f"_populate_group: app key not found: {key}")
                continue
            card = self._card(parent)
            card.pack(fill="x", padx=8, pady=8)
            card.grid_columnconfigure(0, weight=1)

            name = ctk.CTkLabel(
                card,
                text=app["name"],
                font=ctk.CTkFont(size=14, weight="bold"),
            )
            name.grid(row=0, column=0, sticky="w", padx=14, pady=12)

            available = is_app_available_for_install(app)
            btn_text = "Установить" if available else "Установка (FAIL)"

            btn_install = ctk.CTkButton(
                card,
                text=btn_text,
                corner_radius=15,
                command=lambda k=key: self.enqueue(k, True),
                width=140,
            )

            if key == "py_modules":
                btn_second = ctk.CTkButton(
                    card,
                    text="Выбрать .txt",
                    corner_radius=15,
                    command=self.select_py_modules_file,
                    width=130,
                )
            else:
                btn_second = ctk.CTkButton(
                    card,
                    text="Тихо",
                    corner_radius=15,
                    command=lambda k=key: self.enqueue(k, False),
                    width=100,
                )

            btn_install.grid(row=0, column=1, padx=(12, 6), pady=10)
            btn_second.grid(row=0, column=2, padx=(6, 14), pady=10)

            self.rows[key] = {"install": btn_install, "silent": btn_second}
            log(f"_populate_group: button row created for key={key}")

    def select_py_modules_file(self):
        from tkinter import filedialog

        log("select_py_modules_file: open file dialog")
        path = filedialog.askopenfilename(
            parent=self,
            title="Выберите .txt с модулями",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            log("select_py_modules_file: canceled")
            return
        if not os.path.isfile(path):
            self.toaster.show("Файл не найден", path, ms=3200)
            log_error(f"select_py_modules_file: file not found {path}")
            return
        global SELECTED_PY_MODULES_FILE
        SELECTED_PY_MODULES_FILE = path
        log(f"select_py_modules_file: selected {path}")
        self.toaster.show("Файл выбран", os.path.basename(path), ms=3200)

    def _active_sf(self):
        return self.sf_by_tab.get(self.tabs.get())

    def _scroll(self, delta):
        sf = self._active_sf()
        if not sf:
            return
        try:
            sf._parent_canvas.yview_scroll(int(-delta / 30), "units")
        except Exception:
            pass

    def _on_wheel(self, e):
        self._scroll(e.delta)
        return "break"

    def enqueue(self, key, interactive):
        app = get_app_by_key(key)
        if not app:
            log_error(f"enqueue: app not found key={key}")
            return

        type_ = (app.get("type") or "").lower()
        check_key = (app.get("key") or "").lower()
        log(f"enqueue: key={key}, interactive={interactive}, type={type_}")

        # Проверка "уже установлено" только для реальных установщиков
        if type_ in ("exe", "msi", "zip", "custom") and check_key not in ("py_modules", "hevc"):
            snapshot = get_winget_installed_snapshot()
            try:
                if is_app_installed(app, snapshot):
                    log(f"enqueue: key={key} already installed, skipping installation")
                    self.toaster.show("Уже установлено", app["name"], ms=3200)
                    return
            except Exception as e:
                log_error(f"enqueue: is_app_installed error for key={key}: {e}")

        if not get_app_by_key(key):
            log_error(f"enqueue: app lost key={key}")
            return
        ev = _running.get(key)
        if ev is None:
            ev = threading.Event()
            _running[key] = ev
        if ev.is_set():
            log(f"enqueue: key={key} already running")
            return
        ev.set()
        self._set_buttons_state(key, False)

        def worker():
            try:
                install_task(key, interactive, on_ok=self.toast_ok, on_fail=self.toast_fail)
            finally:
                time.sleep(0.2)
                ev.clear()
                log(f"enqueue.worker: key={key} finished, re-enabling buttons")

        _executor.submit(worker)

    def _set_buttons_state(self, key, enabled):
        row = self.rows.get(key)
        if not row:
            return
        state = "normal" if enabled else "disabled"
        row["install"].configure(state=state)
        row["silent"].configure(state=state)
        log(f"_set_buttons_state: key={key}, enabled={enabled}")

    def autorefresh(self):
        self.refresh_state()
        self.check_user_config_changes()
        self.after(1200, self.autorefresh)

    def refresh_state(self):
        for key, row in self.rows.items():
            busy = _running.get(key).is_set() if _running.get(key) else False
            self._set_buttons_state(key, not busy)

    def check_user_config_changes(self):
        try:
            if os.path.isfile(CONFIG_PATH):
                sig = ("exists", os.path.getmtime(CONFIG_PATH), os.path.getsize(CONFIG_PATH))
            else:
                sig = ("missing", 0, 0)
        except Exception:
            sig = ("error", 0, 0)
        if self._cfg_last_sig != sig:
            log(f"check_user_config_changes: config signature changed {self._cfg_last_sig} -> {sig}")
            self._cfg_last_sig = sig
            self.refresh_user_apps_ui()

    def install_all(self):
        keys = [a["key"] for a in CATALOG]
        log(f"install_all: enqueue all keys={keys}")
        for a in CATALOG:
            self.enqueue(a["key"], False)

    def clear_cache(self):
        log(f"clear_cache: path={CACHE_DIR}")
        try:
            for name in os.listdir(CACHE_DIR):
                p = os.path.join(CACHE_DIR, name)
                if os.path.isfile(p):
                    os.remove(p)
            log("Кэш очищен")
        except Exception as e:
            log_error(f"Ошибка очистки кэша: {e}")

    def refresh_user_apps_ui(self):
        log("refresh_user_apps_ui: reload_user_config")
        reload_user_config()
        if self.tabs is None or not self.tabs.winfo_exists():
            log("refresh_user_apps_ui: tabs not ready")
            return
        try:
            tab = self.tabs.tab("Пользовательские")
        except Exception:
            log_error("refresh_user_apps_ui: 'Пользовательские' tab not found")
            return

        for child in tab.winfo_children():
            child.destroy()
        self.user_cfg_button = None

        if CONFIG_STATE == "ok":
            log("refresh_user_apps_ui: CONFIG_STATE=ok")
            sf = ctk.CTkScrollableFrame(
                tab,
                corner_radius=15,
                fg_color=self.BG,
                border_width=0,
                scrollbar_button_color=self.BG,
                scrollbar_button_hover_color=self.BG,
                scrollbar_fg_color=self.BG,
            )
            sf.pack(fill="both", expand=True, padx=4, pady=4)
            self.sf_by_tab["Пользовательские"] = sf
            group = next((g for g in GROUPS if g["title"] == "Пользовательские"), None)
            if group:
                self._populate_group(sf, group["keys"])
        else:
            log(f"refresh_user_apps_ui: CONFIG_STATE={CONFIG_STATE}, ERROR={CONFIG_ERROR!r}")
            if CONFIG_STATE == "missing":
                msg = "config.json не найден.\nНажмите кнопку, чтобы создать шаблон."
            elif CONFIG_STATE == "invalid_json":
                msg = "config.json содержит некорректный JSON:\n" + CONFIG_ERROR
            elif CONFIG_STATE == "invalid_schema":
                msg = "config.json имеет неверную структуру:\n" + CONFIG_ERROR
            else:
                msg = "config.json в неизвестном состоянии."
            lbl = ctk.CTkLabel(tab, text=msg, justify="left", anchor="w")
            lbl.pack(anchor="w", padx=8, pady=(6, 4))
            self.user_cfg_button = ctk.CTkButton(
                tab,
                text="Создать шаблон config.json",
                corner_radius=15,
                command=self.create_config_template,
            )
            self.user_cfg_button.pack(anchor="w", padx=8, pady=(0, 6))

            sf = ctk.CTkScrollableFrame(
                tab,
                corner_radius=15,
                fg_color=self.BG,
                border_width=0,
                scrollbar_button_color=self.BG,
                scrollbar_button_hover_color=self.BG,
                scrollbar_fg_color=self.BG,
            )
            sf.pack(fill="both", expand=True, padx=4, pady=4)
            self.sf_by_tab["Пользовательские"] = sf

    def create_config_template(self):
        overwrite = os.path.isfile(CONFIG_PATH) and CONFIG_STATE in ("invalid_json", "invalid_schema")
        log(f"create_config_template: CONFIG_PATH={CONFIG_PATH}, overwrite={overwrite}")
        if os.path.isfile(CONFIG_PATH) and not overwrite:
            if self.user_cfg_button is not None:
                self.user_cfg_button.destroy()
                self.user_cfg_button = None
            self.toaster.show("config.json", "Файл уже существует", ms=2800)
            return

        template = {
            "user_apps": [
                {
                    "key": "my_app",
                    "name": "Пример приложения",
                    "type": "exe",
                    "silent": "/S",
                    "urls": ["https://example.com/installer.exe"],
                    "winget": [],
                }
            ]
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            log(f"Создан шаблон config.json -> {CONFIG_PATH}")
            self.toaster.show("Создан шаблон", "config.json сохранён рядом с exe", ms=3200)
            self.refresh_user_apps_ui()
        except Exception as e:
            log_error(f"Ошибка создания config.json: {e}")
            self.toaster.show("Ошибка", f"Не удалось создать config.json: {e}", ms=4000)

    def _status_set_text(self, widget, text: str):
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _status_append_line(self, widget, line: str):
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        if widget.index("end-1c") != "1.0":
            widget.insert("end", "\n" + line)
        else:
            widget.insert("end", line)
        widget.see("end")
        widget.configure(state="disabled")

    def _status_clear_all(self):
        for w in (self.status_text_main, self.status_text_ok, self.status_text_fail):
            if w is not None and w.winfo_exists():
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.configure(state="disabled")

    def show_status(self):
        log("show_status: open status window")
        if self.status_win is not None and self.status_win.winfo_exists():
            self.status_win.lift()
            self.status_win.focus_force()
            return

        win = ctk.CTkToplevel(self)
        win.title("Статус Mahashe Install Hub")
        win.geometry("720x520")
        win.configure(fg_color=self.BG)
        win.grab_set()
        self.status_win = win

        top_bar = ctk.CTkFrame(win, fg_color=self.BG, corner_radius=0)
        top_bar.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            top_bar,
            text="Диагностика и состояние программы",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            top_bar,
            text="Обновить",
            corner_radius=15,
            width=100,
            command=self.refresh_status_window,
        ).pack(side="right")

        self.status_tabs = ctk.CTkTabview(
            win,
            corner_radius=12,
            border_width=1,
            border_color=self.BORDER,
            fg_color=self.BG,
            segmented_button_fg_color=self.CARD,
            segmented_button_selected_color=self.BLUE,
            segmented_button_selected_hover_color="#2563eb",
            segmented_button_unselected_color=self.CARD,
            segmented_button_unselected_hover_color="#142352",
            text_color="white",
        )
        self.status_tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        tab_main = self.status_tabs.add("Основное")
        tab_ok = self.status_tabs.add("OK")
        tab_fail = self.status_tabs.add("FAIL")

        self.status_text_main = ctk.CTkTextbox(
            tab_main,
            fg_color=self.CARD,
            border_width=1,
            border_color=self.BORDER,
            wrap="word",
            font=ctk.CTkFont(size=12),
        )
        self.status_text_main.pack(fill="both", expand=True, padx=4, pady=4)

        self.status_text_ok = ctk.CTkTextbox(
            tab_ok,
            fg_color=self.CARD,
            border_width=1,
            border_color=self.BORDER,
            wrap="word",
            font=ctk.CTkFont(size=12),
        )
        self.status_text_ok.pack(fill="both", expand=True, padx=4, pady=4)

        self.status_text_fail = ctk.CTkTextbox(
            tab_fail,
            fg_color=self.CARD,
            border_width=1,
            border_color=self.BORDER,
            wrap="word",
            font=ctk.CTkFont(size=12),
        )
        self.status_text_fail.pack(fill="both", expand=True, padx=4, pady=4)

        self._status_clear_all()
        self._status_set_text(self.status_text_main, "Идёт проверка...\n")
        self._status_set_text(
            self.status_text_ok,
            "Приложения с проверенными рабочими ссылками:\n",
        )
        self._status_set_text(
            self.status_text_fail,
            "Приложения с некорректной конфигурацией / без источников:\n",
        )

        threading.Thread(target=self._collect_status_safe, daemon=True).start()

    def refresh_status_window(self):
        log("refresh_status_window: restart checks")
        if self.status_tabs is None or not self.status_tabs.winfo_exists():
            return
        self._status_clear_all()
        self._status_set_text(self.status_text_main, "Идёт повторная проверка...\n")
        self._status_set_text(
            self.status_text_ok,
            "Приложения с проверенными рабочими ссылками:\n",
        )
        self._status_set_text(
            self.status_text_fail,
            "Приложения с некорректной конфигурацией / без источников:\n",
        )
        threading.Thread(target=self._collect_status_safe, daemon=True).start()

    def _collect_status_safe(self):
        try:
            self._collect_status()
        except Exception:
            log_error("Status collector crashed:\n" + traceback.format_exc())

    def _collect_status(self):
        def add_main(line=""):
            self.after(0, lambda t=line: self._status_append_line(self.status_text_main, t))

        def add_ok(line=""):
            self.after(0, lambda t=line: self._status_append_line(self.status_text_ok, t))

        def add_fail(line=""):
            self.after(0, lambda t=line: self._status_append_line(self.status_text_fail, t))

        log("_collect_status: start")
        self.after(
            0,
            lambda: self._status_set_text(self.status_text_main, "Статус Mahashe Install Hub\n"),
        )

        winget_snapshot = get_winget_installed_snapshot()

        add_main("")
        add_main("=== Общие проверки ===")
        add_main(f"Администратор: {'ДА' if is_admin() else 'НЕТ'}")
        add_main(f"CACHE_DIR: {CACHE_DIR}")
        add_main(f"SOURCE_DIR (temp): {SOURCE_DIR}")
        cfg_exists = CONFIG_STATE != "missing"
        add_main(f"config.json: {'найден' if cfg_exists else 'нет'} ({CONFIG_PATH})")
        if CONFIG_STATE == "ok":
            add_main(f"Пользовательские приложения из config.json: {USER_APPS_COUNT}")
        elif CONFIG_STATE == "invalid_json":
            add_main(f"config.json: некорректный JSON: {CONFIG_ERROR}")
        elif CONFIG_STATE == "invalid_schema":
            add_main(f"config.json: неверная структура: {CONFIG_ERROR}")

        pm_file = get_py_modules_file()
        add_main("")
        add_main("=== Python modules ===")
        if pm_file:
            add_main(f"Файл python-modules.txt: найден ({pm_file}) — ГОТОВО")
        else:
            add_main(
                "Файл python-modules.txt: не найден — функция 'Python модули By Mahashe' недоступна"
            )

        add_main("")
        add_main("=== Каталог приложений ===")
        for app in CATALOG:
            key = app.get("key", "?")
            name = app.get("name", "?")
            type_ = app.get("type", "?")
            urls = app.get("urls") or []
            winget_ids = app.get("winget") or []

            avail = is_app_available_for_install(app)

            add_main(f"[{key}] {name}")
            add_main(f"  Тип: {type_}")
            if winget_ids:
                add_main(f"  Winget: {', '.join(winget_ids)}")
            if key == "py_modules":
                if pm_file:
                    add_main(f"  Источник модулей: {pm_file} — OK")
                else:
                    add_main("  Источник модулей: НЕ НАЙДЕН — FAIL")

            # Состояние установки (по эвристикам)
            if type_ in ("exe", "msi", "zip", "custom") and key not in ("py_modules",):
                try:
                    installed = is_app_installed(app, winget_snapshot)
                    add_main(f"  Установлено: {'ДА' if installed else 'не обнаружено'}")
                except Exception:
                    add_main("  Установлено: ошибка проверки")

            if type_ == "link":
                if urls:
                    add_main(f"  Ссылка: {urls[0]} (тип link — только открытие сайта)")
                    entry_line = f"[{key}] {name} — ссылка: {urls[0]}"
                    add_ok(entry_line)
                else:
                    add_main("  Ссылка: отсутствует")
                    if not avail:
                        entry_line = f"[{key}] {name} — нет URL (link)"
                        add_fail(entry_line)
                add_main("")
                continue

            if urls:
                line, ok_cnt, total_cnt, status = check_urls_status(urls)
                add_main(f"  {line}")
                if ok_cnt > 0:
                    entry_line = f"[{key}] {name} — {line}"
                    add_ok(entry_line)
            else:
                add_main("  URLs: нет")

            if not avail:
                reason = "нет URL/winget"
                if key == "py_modules":
                    reason = "нет файла python-modules.txt"
                entry_line = f"[{key}] {name} — недоступно ({reason})"
                add_fail(entry_line)

            add_main("")
        log("_collect_status: done")


if __name__ == "__main__":
    elevate_if_needed()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"==== {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} START ====\n")
    if not is_admin():
        log("ВНИМАНИЕ: процесс не с правами администратора")
    log("App main: starting mainloop")
    App().mainloop()