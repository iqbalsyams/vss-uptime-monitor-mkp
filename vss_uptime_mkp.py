"""Monitor uptime server VSS lokal MKP (192.168.185.6:9966).

Satu exe, dua mode:
  VSS_UptimeMonitor_MKP.exe            -> dashboard
  VSS_UptimeMonitor_MKP.exe --monitor  -> pencatat background (dijalankan Task Scheduler saat boot)

Tiap interval dicek: gateway lokal, ping server, HTTP layanan VSS, ping internet.
Monitor hanya menulis sampel; downtime dihitung dashboard dari sampel, jadi
restart/listrik padam tidak meninggalkan state setengah jadi.
Data di folder data_uptime/ di samping exe.
"""
import argparse
import ctypes
import http.client
import json
import logging
import logging.handlers
import msvcrt
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

APP_NAME = "VSS Uptime Monitor - MKP"
TASK_NAME = "VSS Uptime Monitor MKP"
TS_FMT = "%Y-%m-%d %H:%M:%S"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: ping tidak memunculkan jendela console
# Server MKP memblokir ICMP. Port milik OS/database (bukan aplikasi VSS) dipakai
# sebagai tanda "mesin hidup" supaya server mati bisa dibedakan dari VSS mati.
HOST_PORT_CANDIDATES = [3389, 445, 135, 22, 5985, 3306, 5432, 1433]

DEFAULT_CONFIG = {
    "server_ip": "192.168.185.6",
    "vss_port": 9966,
    "internet_host": "8.8.8.8",
    "gateway": "auto",
    "host_port": "auto",
    "interval_sec": 30,
    "fail_threshold": 3,
    "keep_days": 180,
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = os.path.join(app_dir(), "data_uptime")
DB_PATH = os.path.join(DATA_DIR, "uptime.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")
LOCK_PATH = os.path.join(DATA_DIR, "monitor.lock")
PID_PATH = os.path.join(DATA_DIR, "monitor.pid")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    ts INTEGER PRIMARY KEY, gw INTEGER, ping INTEGER, tcp INTEGER,
    http INTEGER, inet INTEGER, rtt_ms INTEGER);
CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, boot_ts INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""
# Kolom index pada baris samples yang dibaca dashboard.
TS, GW, PING, TCP, HTTP, INET, RTT, HOST = range(8)


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg


def gap_limit(cfg):
    return max(3 * cfg["interval_sec"], 120)


# ---------------------------------------------------------------- pengecekan

def ping(host, timeout_ms=1000):
    """(ok, rtt_ms). Cek 'TTL=' karena returncode 0 juga untuk 'Destination host unreachable'."""
    try:
        out = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), host],
                             capture_output=True, timeout=timeout_ms / 1000 + 3,
                             creationflags=NO_WINDOW).stdout.decode("latin-1", "ignore")
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if "TTL=" not in out.upper():
        return False, None
    m = re.search(r"[=<](\d+)\s?ms", out)
    return True, int(m.group(1)) if m else None


def tcp_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_alive(host, port, timeout=5):
    """Status HTTP apa pun (termasuk 404) berarti layanan VSS masih menjawab."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/")
        conn.getresponse().read(256)
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def detect_gateway():
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True,
                             timeout=10, creationflags=NO_WINDOW).stdout.decode("latin-1", "ignore")
    except (OSError, subprocess.TimeoutExpired):
        return None
    best = None
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0" and p[2][0].isdigit():
            metric = int(p[4]) if p[4].isdigit() else 9999
            if best is None or metric < best[0]:
                best = (metric, p[2])
    return best[1] if best else None


def detect_host_port(ip, skip):
    for p in HOST_PORT_CANDIDATES:
        if p != skip and tcp_open(ip, p, timeout=1):
            return p
    return None


def check_all(cfg, gateway, host_port):
    ip, port = cfg["server_ip"], int(cfg["vss_port"])
    gw = None
    if gateway:
        gw = int(ping(gateway)[0])
    ping_ok, rtt = ping(ip)
    tcp = tcp_open(ip, port)
    http_ok = http_alive(ip, port) if tcp else False
    inet = ping(cfg["internet_host"])[0]
    host = int(tcp_open(ip, host_port)) if host_port else None
    return gw, int(ping_ok), int(tcp), int(http_ok), int(inet), rtt, host


# ---------------------------------------------------------------- mode monitor

def boot_time():
    return int(time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000)


def run_monitor():
    cfg = load_config()
    log = logging.getLogger("monitor")
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    lock = open(LOCK_PATH, "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("monitor lain sudah berjalan, keluar")
        return
    # Exe onefile = proses induk (bootloader) + anak (ini). schtasks /End hanya mematikan
    # induk, jadi keduanya dicatat agar "Hapus monitor" bisa mematikan anak yang mengunci exe.
    with open(PID_PATH, "w") as f:
        f.write(f"{os.getpid()} {os.getppid()}")

    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: cegah PC idle-sleep selama monitor jalan.
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)

    db = sqlite3.connect(DB_PATH, timeout=30)
    db.executescript(SCHEMA)
    try:
        db.execute("ALTER TABLE samples ADD COLUMN host INTEGER")  # database dari versi pertama
    except sqlite3.OperationalError:
        pass
    db.execute("INSERT INTO events VALUES(?,?,?)", (int(time.time()), "start", boot_time()))
    db.commit()
    log.info("monitor mulai, config=%s", cfg)

    interval = int(cfg["interval_sec"])
    auto_host = cfg["host_port"] == "auto"
    host_port = None if auto_host else int(cfg["host_port"] or 0) or None
    gateway, gw_checked, host_checked, pruned = None, 0, 0, 0
    while True:
        t0 = time.time()
        try:
            if cfg["gateway"] != "auto":
                gateway = cfg["gateway"]
            elif t0 - gw_checked > 600 or gateway is None:
                gateway, gw_checked = detect_gateway(), t0
            # Dicari ulang tiap 10 menit selama belum ketemu (server mungkin sedang mati saat boot).
            if auto_host and host_port is None and t0 - host_checked > 600:
                host_port, host_checked = detect_host_port(cfg["server_ip"], int(cfg["vss_port"])), t0
                log.info("port cek mesin server: %s", host_port or "tidak ada yang terbuka")
            db.execute("INSERT OR REPLACE INTO meta VALUES('host_port', ?)", (str(host_port or ""),))
            row = check_all(cfg, gateway, host_port)
            db.execute("INSERT OR REPLACE INTO samples(ts, gw, ping, tcp, http, inet, rtt_ms, host) "
                       "VALUES(?,?,?,?,?,?,?,?)", (int(t0),) + row)
            if t0 - pruned > 86400:
                db.execute("DELETE FROM samples WHERE ts < ?", (int(t0) - cfg["keep_days"] * 86400,))
                pruned = t0
            db.commit()
        except Exception:
            log.exception("gagal satu siklus")
        time.sleep(max(1.0, interval - (time.time() - t0)))


# ---------------------------------------------------------------- analisis

def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime(TS_FMT) if ts else ""


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} dtk"
    h, m = divmod(sec // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d} hari {h} jam"
    return f"{h} jam {m} mnt" if h else f"{m} mnt"


def machine_alive(r):
    """1/0, atau None kalau tidak bisa diketahui (ping diblokir dan tidak ada port cek mesin)."""
    if r[PING] or r[HOST]:
        return 1
    return None if r[HOST] is None else 0


def classify(run):
    n = len(run)
    gw_fail = sum(1 for r in run if r[GW] == 0)
    alive = sum(1 for r in run if machine_alive(r) or r[TCP])
    if gw_fail > n / 2:
        return "lan", "Jaringan lokal putus", "Gateway PC monitor juga tidak merespons: switch/router/kabel PC monitor, bukan server"
    if alive > n / 2:
        return "service", "Layanan VSS mati", "Mesin server hidup tapi web/API :9966 tidak menjawab (aplikasi VSS hang/mati)"
    if all(machine_alive(r) is None for r in run):
        return "down", "Server tidak merespons", ("Port 9966 gagal, gateway OK. Tidak bisa dibedakan server mati "
                                                  "atau aplikasi VSS mati (ping diblokir, tidak ada port cek mesin)")
    return "down", "Server tidak merespons", "Mesin server dan port 9966 tidak menjawab, gateway OK: server mati/hang atau kabel server"


def gap_note(start, end, events):
    boots = [e[1] for e in events if e[0] >= start and (end is None or e[0] <= end + 600)]
    for b in boots:
        if start - 60 <= b and (end is None or b <= end):
            return f"PC monitor menyala ulang pukul {fmt_ts(b)[11:16]}: PC mati/restart (cek listrik)"
    if end is None:
        return "Monitor tidak mencatat sampai sekarang: PC mati, sleep, atau monitor berhenti"
    return "PC tidak restart: monitor berhenti atau PC sleep"


def derive_incidents(rows, events, cfg, now=None):
    """Kejadian = >= fail_threshold sampel gagal berturut-turut, plus celah tanpa data."""
    now = now or time.time()
    limit, thr = gap_limit(cfg), int(cfg["fail_threshold"])
    out, run = [], []
    prev = None

    def flush(end_ts, ongoing=False, cut=False):
        if len(run) >= thr:
            kind, label, note = classify(run)
            if cut:
                note += "; monitor berhenti mencatat sebelum pulih"
            out.append({"start": run[0][0], "end": end_ts, "ongoing": ongoing, "kind": kind,
                        "label": label, "note": note, "samples": len(run)})
        run.clear()

    for r in rows:
        ts = r[0]
        if prev is not None and ts - prev > limit:
            if run:
                flush(prev, cut=True)
            out.append({"start": prev, "end": ts, "ongoing": False, "kind": "gap",
                        "label": "Monitor tidak mencatat", "note": gap_note(prev, ts, events),
                        "samples": 0})
        if r[4]:
            if run:
                flush(ts)
        else:
            run.append(r)
        prev = ts
    if prev is not None and now - prev > limit:
        if run:
            flush(prev, cut=True)
        out.append({"start": prev, "end": None, "ongoing": True, "kind": "gap",
                    "label": "Monitor tidak mencatat", "note": gap_note(prev, None, events),
                    "samples": 0})
    elif run:
        flush(None, ongoing=True)
    for inc in out:
        inc["duration"] = (inc["end"] or now) - inc["start"]
    return out


def uptime_pct(rows, since):
    sel = [r[4] for r in rows if r[0] >= since]
    return (100.0 * sum(sel) / len(sel), len(sel)) if sel else (None, 0)


def server_state(r):
    if r[HTTP]:
        return "UP"
    if r[GW] == 0:
        return "LAN"
    if machine_alive(r) or r[TCP]:
        return "SERVICE"
    return "DOWN"


def open_db_readonly():
    if not os.path.exists(DB_PATH):
        return None
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)


def load_data(since):
    """(rows, events, meta). Toleran terhadap database versi pertama (tanpa kolom host/tabel meta)."""
    db = open_db_readonly()
    if db is None:
        return [], [], {}
    try:
        cols = {c[1] for c in db.execute("PRAGMA table_info(samples)")}
        host = "host" if "host" in cols else "NULL"
        rows = db.execute(f"SELECT ts, gw, ping, tcp, http, inet, rtt_ms, {host} FROM samples "
                          "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        events = db.execute("SELECT ts, boot_ts FROM events WHERE kind='start' AND ts >= ?",
                            (since,)).fetchall()
        try:
            meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.OperationalError:
            meta = {}
    finally:
        db.close()
    return rows, events, meta


# ---------------------------------------------------------------- Task Scheduler

def task_xml():
    if getattr(sys, "frozen", False):
        cmd, args = sys.executable, "--monitor"
    else:
        cmd, args = sys.executable, f'"{os.path.abspath(__file__)}" --monitor'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Pencatat uptime server VSS MKP ({escape(cmd)})</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(cmd)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(app_dir())}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def run_elevated(cmdline):
    """Jalankan cmd.exe sebagai admin (muncul prompt UAC). True kalau prompt diterima."""
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {cmdline}", None, 0)
    return rc > 32


def install_task():
    path = os.path.join(tempfile.gettempdir(), "vss_uptime_task.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(task_xml())
    return run_elevated(f'schtasks /Create /TN "{TASK_NAME}" /XML "{path}" /F '
                        f'&& schtasks /Run /TN "{TASK_NAME}"')


def uninstall_task():
    cmd = f'schtasks /End /TN "{TASK_NAME}" & schtasks /Delete /TN "{TASK_NAME}" /F'
    image = os.path.basename(sys.executable)
    try:
        with open(PID_PATH) as f:
            pids = f.read().split()
    except OSError:
        pids = []
    # Filter nama image ikut dipakai supaya PID yang sudah dipakai ulang proses lain tidak ikut mati.
    for pid in pids:
        if pid.isdigit():
            cmd += f' & taskkill /F /FI "PID eq {pid}" /FI "IMAGENAME eq {image}"'
    return run_elevated(cmd)


# ---------------------------------------------------------------- ekspor Excel

def export_excel(path, cfg):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows, events, meta = load_data(0)
    incidents = derive_incidents(rows, events, cfg)
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")

    def sheet(ws, header, data, widths):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for d in data:
            ws.append(d)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Downtime"
    sheet(ws, ["Mulai", "Selesai", "Durasi (menit)", "Durasi", "Jenis", "Keterangan"],
          [[fmt_ts(i["start"]), "masih berlangsung" if i["ongoing"] else fmt_ts(i["end"]),
            round(i["duration"] / 60, 1), fmt_dur(i["duration"]), i["label"], i["note"]]
           for i in reversed(incidents)],
          [20, 20, 14, 16, 26, 90])

    by_day = {}
    for r in rows:
        d = by_day.setdefault(datetime.fromtimestamp(r[0]).date(), [0, 0])
        d[0] += 1
        d[1] += r[4]
    inc_day = {}
    for i in incidents:
        if i["kind"] != "gap":
            k = datetime.fromtimestamp(i["start"]).date()
            inc_day[k] = inc_day.get(k, 0) + 1
    interval = cfg["interval_sec"]
    sheet(wb.create_sheet("Harian"),
          ["Tanggal", "Jumlah sampel", "Uptime (%)", "Perkiraan down (menit)", "Kejadian down",
           "Jam tercatat"],
          [[d.isoformat(), n, round(100 * ok / n, 2), round((n - ok) * interval / 60, 1),
            inc_day.get(d, 0), round(n * interval / 3600, 1)] for d, (n, ok) in sorted(by_day.items())],
          [12, 15, 12, 22, 15, 13])

    hours = [[0, 0, 0] for _ in range(24)]
    for r in rows:
        h = hours[datetime.fromtimestamp(r[0]).hour]
        h[0] += 1
        h[1] += 1 - r[4]
    for i in incidents:
        if i["kind"] != "gap":
            hours[datetime.fromtimestamp(i["start"]).hour][2] += 1
    sheet(wb.create_sheet("Per Jam"),
          ["Jam", "Jumlah sampel", "Sampel down", "Down (%)", "Kejadian dimulai di jam ini"],
          [[f"{h:02d}:00-{h:02d}:59", n, dn, round(100 * dn / n, 2) if n else None, k]
           for h, (n, dn, k) in enumerate(hours)],
          [14, 15, 13, 10, 28])

    ws = wb.create_sheet("Info")
    for line in [
        ["Server", f"{cfg['server_ip']}:{cfg['vss_port']}"],
        ["Cek mesin server", f"port {meta['host_port']}" if meta.get("host_port")
         else "tidak ada (ping diblokir, tidak ada port lain terbuka)"],
        ["Interval cek", f"{interval} detik"],
        ["Ambang down", f"{cfg['fail_threshold']} cek gagal berturut-turut"],
        ["Data dari", fmt_ts(rows[0][0]) if rows else "-"],
        ["Data sampai", fmt_ts(rows[-1][0]) if rows else "-"],
        ["Zona waktu", "jam PC monitor (WITA)"],
        ["Uptime", "% sampel yang layanan VSS :9966 menjawab HTTP; jam tanpa data tidak dihitung"],
        ["Dibuat", datetime.now().strftime(TS_FMT)],
    ]:
        ws.append(line)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 80
    wb.save(path)


# ---------------------------------------------------------------- dashboard

COLORS = {
    "UP": ("#16A34A", "SERVER UP"),
    "SERVICE": ("#EA580C", "LAYANAN VSS MATI"),
    "DOWN": ("#DC2626", "SERVER DOWN"),
    "LAN": ("#7C3AED", "JARINGAN LOKAL PUTUS"),
    "STALE": ("#6B7280", "MONITOR TIDAK BERJALAN"),
    "NODATA": ("#6B7280", "BELUM ADA DATA"),
}
ROW_TAGS = {"down": "#FEE2E2", "service": "#FFEDD5", "lan": "#EDE9FE", "gap": "#F3F4F6"}
REFRESH_MS = 15000
HARI = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]


def run_dashboard():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

    cfg = load_config()
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("1100x860")
    root.minsize(900, 700)
    icon = os.path.join(getattr(sys, "_MEIPASS", app_dir()), "mceasy.ico")
    if os.path.exists(icon):
        root.iconbitmap(icon)

    style = ttk.Style()
    style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    # status besar
    banner = tk.Frame(root, bg="#6B7280")
    banner.pack(fill="x")
    status_lbl = tk.Label(banner, font=("Segoe UI", 24, "bold"), fg="white", bg="#6B7280")
    status_lbl.pack(anchor="w", padx=16, pady=(12, 0))
    sub_lbl = tk.Label(banner, font=("Segoe UI", 10), fg="white", bg="#6B7280")
    sub_lbl.pack(anchor="w", padx=16, pady=(0, 12))

    body = tk.Frame(root, padx=16, pady=10)
    body.pack(fill="both", expand=True)

    checks = tk.Frame(body)
    checks.pack(fill="x")
    check_lbls = {}
    for key, title in [("gw", "Gateway lokal"), ("ping", "Mesin server"),
                       ("http", f"Layanan VSS :{cfg['vss_port']}"), ("inet", "Internet")]:
        f = tk.Frame(checks, bd=1, relief="solid", padx=10, pady=6)
        f.pack(side="left", padx=(0, 8), fill="x", expand=True)
        tk.Label(f, text=title, font=("Segoe UI", 9), fg="#4B5563").pack(anchor="w")
        lbl = tk.Label(f, font=("Segoe UI", 12, "bold"))
        lbl.pack(anchor="w")
        check_lbls[key] = lbl

    stats = tk.Frame(body, pady=10)
    stats.pack(fill="x")
    stat_lbls = {}
    for key, title in [("24h", "Uptime 24 jam"), ("7d", "Uptime 7 hari"), ("30d", "Uptime 30 hari"),
                       ("inc", "Kejadian down 7 hari"), ("dur", "Total down 7 hari")]:
        f = tk.Frame(stats)
        f.pack(side="left", padx=(0, 28))
        tk.Label(f, text=title, font=("Segoe UI", 9), fg="#4B5563").pack(anchor="w")
        lbl = tk.Label(f, font=("Segoe UI", 16, "bold"))
        lbl.pack(anchor="w")
        stat_lbls[key] = lbl

    # Dipasang sebelum tabel supaya tidak terdorong keluar jendela saat tabel mengembang.
    bottom = tk.Frame(body)
    bottom.pack(side="bottom", fill="x")
    monitor_lbl = tk.Label(bottom, font=("Segoe UI", 9), fg="#4B5563", anchor="w", justify="left")
    monitor_lbl.pack(fill="x")
    buttons = tk.Frame(bottom, pady=6)
    buttons.pack(fill="x")

    tk.Label(body, text="Uptime per jam, 7 hari terakhir", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    heat = tk.Canvas(body, height=230, highlightthickness=0)
    heat.pack(fill="x", pady=(4, 8))

    tk.Label(body, text="Riwayat downtime (30 hari)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    tree_frame = tk.Frame(body)
    tree_frame.pack(fill="both", expand=True, pady=(4, 8))
    cols = [("start", "Mulai", 150), ("end", "Selesai", 150), ("dur", "Durasi", 110),
            ("label", "Jenis", 190), ("note", "Keterangan", 480)]
    tree = ttk.Treeview(tree_frame, columns=[c[0] for c in cols], show="headings")
    for key, title, w in cols:
        tree.heading(key, text=title)
        tree.column(key, width=w, minwidth=60, stretch=key == "note")
    for tag, bg in ROW_TAGS.items():
        tree.tag_configure(tag, background=bg)
    sb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def do_export():
        default = f"uptime_vss_MKP_{datetime.now():%Y%m%d-%H%M}.xlsx"
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile=default,
                                            initialdir=app_dir(), filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        try:
            export_excel(path, cfg)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Gagal ekspor: {e}")
            return
        messagebox.showinfo(APP_NAME, f"Tersimpan:\n{path}")

    def do_install():
        if not messagebox.askyesno(APP_NAME, "Daftarkan monitor ke Task Scheduler?\n\n"
                                   "Monitor akan jalan otomatis setiap PC menyala, tanpa perlu login.\n"
                                   "Windows akan meminta izin admin.\n\n"
                                   "Jangan pindahkan exe ini setelah dipasang."):
            return
        if install_task():
            messagebox.showinfo(APP_NAME, "Monitor dipasang dan dijalankan.\n"
                                "Status di bawah akan berubah dalam 1 menit.")
            root.after(8000, refresh)
        else:
            messagebox.showwarning(APP_NAME, "Izin admin ditolak, monitor belum dipasang.")

    def do_uninstall():
        if messagebox.askyesno(APP_NAME, "Hentikan dan hapus monitor dari Task Scheduler?\n"
                               "Data yang sudah tercatat tidak dihapus."):
            uninstall_task()
            root.after(5000, refresh)

    for text, cmd in [("Hapus monitor", do_uninstall), ("Pasang monitor otomatis", do_install),
                      ("Buka folder data", lambda: os.startfile(DATA_DIR)),
                      ("Ekspor Excel", do_export), ("Refresh", lambda: refresh())]:
        ttk.Button(buttons, text=text, command=cmd).pack(side="right", padx=(6, 0))

    def set_banner(key, sub):
        color, text = COLORS[key]
        for w in (banner, status_lbl, sub_lbl):
            w.configure(bg=color)
        status_lbl.configure(text=text)
        sub_lbl.configure(text=sub)

    def set_check(key, val, extra="", unknown="tidak diketahui"):
        if val is None:
            check_lbls[key].configure(text=unknown, fg="#6B7280")
        else:
            check_lbls[key].configure(text=("OK" if val else "GAGAL") + extra,
                                      fg="#16A34A" if val else "#DC2626")

    def pct_text(p):
        return "-" if p is None else f"{p:.2f}%"

    def draw_heat(rows):
        heat.delete("all")
        w = max(heat.winfo_width(), 800)
        left, top, cell_h = 92, 20, 24
        cell_w = (w - left - 4) / 24
        for h in range(24):
            if h % 2 == 0:
                heat.create_text(left + h * cell_w + cell_w / 2, 10, text=f"{h:02d}",
                                 font=("Segoe UI", 8), fill="#4B5563")
        buckets = {}
        for r in rows:
            dt = datetime.fromtimestamp(r[0])
            b = buckets.setdefault((dt.date(), dt.hour), [0, 0])
            b[0] += 1
            b[1] += r[4]
        today = datetime.now().date()
        for i in range(7):
            day = today - timedelta(days=6 - i)
            y = top + i * cell_h
            heat.create_text(left - 8, y + cell_h / 2, anchor="e", font=("Segoe UI", 9),
                             text=f"{HARI[day.weekday()]} {day:%d/%m}")
            for h in range(24):
                n, ok = buckets.get((day, h), (0, 0))
                if n == 0:
                    color = "#E5E7EB"
                else:
                    frac = ok / n
                    color = ("#22C55E" if frac >= 0.999 else "#FACC15" if frac >= 0.9
                             else "#F97316" if frac >= 0.5 else "#DC2626")
                x = left + h * cell_w
                heat.create_rectangle(x + 1, y + 1, x + cell_w - 1, y + cell_h - 1,
                                      fill=color, outline="")
        ly = top + 7 * cell_h + 8
        x = left
        for color, text in [("#22C55E", "100%"), ("#FACC15", "90-99%"), ("#F97316", "50-89%"),
                            ("#DC2626", "<50%"), ("#E5E7EB", "tidak ada data (monitor/PC mati)")]:
            heat.create_rectangle(x, ly, x + 12, ly + 12, fill=color, outline="")
            t = heat.create_text(x + 18, ly + 6, anchor="w", text=text, font=("Segoe UI", 8))
            x = heat.bbox(t)[2] + 16

    def refresh():
        now = time.time()
        rows, events, meta = load_data(int(now) - 30 * 86400)
        if not rows:
            set_banner("NODATA", f"Server {cfg['server_ip']}:{cfg['vss_port']}. "
                       "Klik 'Pasang monitor otomatis' untuk mulai mencatat.")
            for k in check_lbls:
                set_check(k, None)
        else:
            last = rows[-1]
            age = now - last[0]
            sub = (f"Server {cfg['server_ip']}:{cfg['vss_port']}  |  cek terakhir {fmt_ts(last[0])} "
                   f"({fmt_dur(age)} lalu)")
            set_banner("STALE" if age > gap_limit(cfg) else server_state(last), sub)
            set_check("gw", None if last[1] is None else bool(last[1]))
            if last[PING]:
                set_check("ping", True, f"  ping {last[RTT]} ms" if last[RTT] is not None else "")
            elif last[HOST] is not None:
                set_check("ping", bool(last[HOST]), f"  (port {meta.get('host_port', '?')})")
            else:
                set_check("ping", None, unknown="ping diblokir, tidak ada port cek")
            set_check("http", bool(last[4]),
                      "" if last[4] or not last[3] else "  (port terbuka, tidak menjawab)")
            set_check("inet", bool(last[5]))

        stat_lbls["24h"].configure(text=pct_text(uptime_pct(rows, now - 86400)[0]))
        stat_lbls["7d"].configure(text=pct_text(uptime_pct(rows, now - 7 * 86400)[0]))
        stat_lbls["30d"].configure(text=pct_text(uptime_pct(rows, now - 30 * 86400)[0]))
        incidents = derive_incidents(rows, events, cfg, now)
        week = [i for i in incidents if i["kind"] != "gap" and i["start"] >= now - 7 * 86400]
        stat_lbls["inc"].configure(text=str(len(week)))
        stat_lbls["dur"].configure(text=fmt_dur(sum(i["duration"] for i in week)) if week else "0")

        draw_heat([r for r in rows if r[0] >= now - 8 * 86400])

        tree.delete(*tree.get_children())
        for i in reversed(incidents):
            tree.insert("", "end", tags=(i["kind"],), values=(
                fmt_ts(i["start"]), "masih berlangsung" if i["ongoing"] else fmt_ts(i["end"]),
                fmt_dur(i["duration"]), i["label"], i["note"]))

        if rows and now - rows[-1][0] <= gap_limit(cfg):
            mon = "Monitor background: berjalan."
        else:
            mon = "Monitor background: TIDAK berjalan. Klik 'Pasang monitor otomatis'."
        monitor_lbl.configure(text=f"{mon}  Menutup jendela ini tidak menghentikan monitor.\n"
                                   f"Data: {DATA_DIR}")

    def tick():
        refresh()
        root.after(REFRESH_MS, tick)

    heat.bind("<Configure>", lambda e: refresh())
    root.after(100, tick)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--monitor", action="store_true", help="jalankan pencatat background")
    args = ap.parse_args()
    if args.monitor:
        run_monitor()
    else:
        run_dashboard()


if __name__ == "__main__":
    main()
