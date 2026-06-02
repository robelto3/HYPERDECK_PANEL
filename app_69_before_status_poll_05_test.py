from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import html
import json
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = "0.0.0.0"
PORT = 8080
CONFIG_FILE = Path("config.json")
SIM_STATES = {}

DEFAULT_CONFIG = {
    "panel_ip": "10.0.0.8",
    "atem_logger_ip": "10.0.0.9",
    "simulation": True,
    "tc_compare": True,
    "tc_tolerance_frames": 2,
    "decks": [
        {"name": "REC 1", "ip": "10.0.0.11"},
        {"name": "REC 2", "ip": "10.0.0.12"},
        {"name": "REC 3", "ip": "10.0.0.13"},
        {"name": "REC 4", "ip": "10.0.0.14"},
        {"name": "REC 5", "ip": "10.0.0.15"},
        {"name": "REC 6", "ip": "10.0.0.16"},
        {"name": "REC 7", "ip": "10.0.0.21"},
        {"name": "REC 8", "ip": "10.0.0.22"},
        {"name": "PGM REC", "ip": "10.0.0.24"}
    ]
}


def save_config(cfg):
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_config():
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = DEFAULT_CONFIG.copy()

    cfg.setdefault("panel_ip", "10.0.0.8")
    cfg.setdefault("atem_logger_ip", "10.0.0.9")
    cfg.setdefault("simulation", True)
    cfg.setdefault("tc_compare", True)
    cfg.setdefault("tc_tolerance_frames", 2)
    cfg.setdefault("decks", DEFAULT_CONFIG["decks"])

    for deck in cfg["decks"]:
        deck.setdefault("armed", True)

        if is_pgm_deck(deck.get("name", "")):
            deck["armed"] = True

    return cfg


def is_pgm_deck(name):
    return "PGM" in name.upper()


def deck_is_armed(deck):
    if is_pgm_deck(deck.get("name", "")):
        return True

    return bool(deck.get("armed", True))


def get_armed_decks(cfg):
    return [
        deck
        for deck in cfg.get("decks", [])
        if deck_is_armed(deck)
    ]


def get_armed_deck_indexes(cfg):
    return [
        index
        for index, deck in enumerate(cfg.get("decks", []))
        if deck_is_armed(deck)
    ]


def frames_to_tcx2(total_frames, fps=25):
    total_frames %= 24 * 60 * 60 * fps

    hh = total_frames // (60 * 60 * fps)
    total_frames %= 60 * 60 * fps

    mm = total_frames // (60 * fps)
    total_frames %= 60 * fps

    ss = total_frames // fps
    ff = (total_frames % fps) * 2

    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def init_sim_states(deck_count):
    defaults = [
        {"status": "RECORDING", "led": "recording", "tc_valid": True, "offset": 0},
        {"status": "RECORDING", "led": "recording", "tc_valid": True, "offset": 0},
        {"status": "TC WARNING", "led": "warning", "tc_valid": True, "offset": 5},
        {"status": "OFFLINE", "led": "offline", "tc_valid": False, "offset": 0},
        {"status": "NO SIGNAL", "led": "warning", "tc_valid": False, "offset": 0},
        {"status": "STOPPED", "led": "stopped", "tc_valid": True, "offset": 0},
        {"status": "RECORDING", "led": "recording", "tc_valid": True, "offset": 0},
        {"status": "RECORDING", "led": "recording", "tc_valid": True, "offset": 0},
        {"status": "RECORDING", "led": "recording", "tc_valid": True, "offset": 0},
    ]

    for i in range(deck_count):
        if i not in SIM_STATES:
            if i < len(defaults):
                SIM_STATES[i] = defaults[i].copy()
            else:
                SIM_STATES[i] = {
                    "status": "STOPPED",
                    "led": "stopped",
                    "tc_valid": True,
                    "offset": 0
                }

    for key in list(SIM_STATES.keys()):
        if key >= deck_count:
            del SIM_STATES[key]


def set_sim_deck_status(index, cmd):
    if index not in SIM_STATES:
        return

    if cmd == "rec":
        SIM_STATES[index] = {
            "status": "RECORDING",
            "led": "recording",
            "tc_valid": True,
            "offset": 0
        }

    elif cmd == "stop":
        SIM_STATES[index] = {
            "status": "STOPPED",
            "led": "stopped",
            "tc_valid": True,
            "offset": 0
        }


def get_sim_state(index):
    state = SIM_STATES.get(index, {
        "status": "STOPPED",
        "led": "stopped",
        "tc_valid": True,
        "offset": 0
    })

    if not state.get("tc_valid", False):
        return state["status"], state["led"], "--:--:--:--", False, 0

    total_frames = int(time.time() * 25) + int(state.get("offset", 0))
    tc = frames_to_tcx2(total_frames)

    return state["status"], state["led"], tc, True, total_frames


def recv_hyperdeck_message(sock, timeout=0.6):
    sock.settimeout(timeout)
    data = b""

    while True:
        try:
            chunk = sock.recv(4096)

            if not chunk:
                break

            data += chunk

            if b"\r\n\r\n" in data or b"\n\n" in data:
                break

        except socket.timeout:
            break

    return data.decode("utf-8", errors="replace")


def parse_hyperdeck_info(text):
    info = {}

    for line in text.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        info[key] = value

    return info


def tc_text_to_25fps_frames(tc_text, is_50fps=False):
    try:
        parts = tc_text.strip().split(":")

        if len(parts) != 4:
            return None

        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2])
        ff = int(parts[3])

        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            return None

        # HyperDeck může hlásit 1080p50, ale frame hodnota se pro náš panel
        # převádí na 25fps základ a na obrazovce se zobrazuje zpět jako TCx2.
        if is_50fps or ff > 24:
            ff = ff // 2

        if not (0 <= ff <= 24):
            return None

        return ((hh * 60 * 60) + (mm * 60) + ss) * 25 + ff

    except ValueError:
        return None


def get_real_state(deck):
    ip = deck.get("ip", "").strip()

    if not ip:
        return "OFFLINE", "offline", "--:--:--:--", False, 0

    try:
        with socket.create_connection((ip, 9993), timeout=0.6) as sock:
            recv_hyperdeck_message(sock, timeout=0.4)

            sock.sendall(b"transport info\r\n")
            response = recv_hyperdeck_message(sock, timeout=0.6)

    except OSError:
        return "OFFLINE", "offline", "--:--:--:--", False, 0

    info = parse_hyperdeck_info(response)

    status_raw = info.get("status", "").lower()
    input_format = info.get("input video format", "").lower()
    video_format = info.get("video format", "").lower()
    tc_text = info.get("display timecode") or info.get("timecode") or ""

    if input_format == "none":
        return "NO SIGNAL", "warning", "--:--:--:--", False, 0

    if status_raw in ("record", "recording"):
        status_text = "RECORDING"
        led_class = "recording"

    elif status_raw in ("stopped", "stop", "preview", "idle"):
        status_text = "STOPPED"
        led_class = "stopped"

    elif status_raw in ("play", "playing"):
        status_text = "PLAYING"
        led_class = "warning"

    else:
        status_text = status_raw.upper() if status_raw else "ONLINE"
        led_class = "warning"

    is_50fps = "50" in input_format or "50" in video_format
    total_frames = tc_text_to_25fps_frames(tc_text, is_50fps=is_50fps)

    if total_frames is None:
        return status_text, led_class, "--:--:--:--", False, 0

    return status_text, led_class, frames_to_tcx2(total_frames), True, total_frames


def send_hyperdeck_command(deck, cmd):
    ip = deck.get("ip", "").strip()

    command_map = {
        "rec": "record",
        "stop": "stop"
    }

    command = command_map.get(cmd)

    if not ip or not command:
        return False, "bad command"

    try:
        with socket.create_connection((ip, 9993), timeout=1.0) as sock:
            recv_hyperdeck_message(sock, timeout=0.4)

            sock.sendall(b"remote: override: true\r\n")
            recv_hyperdeck_message(sock, timeout=0.5)

            sock.sendall((command + "\r\n").encode("ascii"))
            response = recv_hyperdeck_message(sock, timeout=1.0)

    except OSError as e:
        return False, str(e)

    return "200 ok" in response.lower(), response.strip()


def send_hyperdeck_commands_parallel(decks, cmd):
    results = []

    if not decks:
        return results

    max_workers = min(len(decks), 12)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(send_hyperdeck_command, deck, cmd): deck
            for deck in decks
        }

        for future in as_completed(future_map):
            deck = future_map[future]

            try:
                ok, response = future.result()
            except Exception as e:
                ok, response = False, str(e)

            results.append({
                "name": deck.get("name", ""),
                "ip": deck.get("ip", ""),
                "ok": ok,
                "response": response
            })

    return results


def make_alarm(cfg):
    if not cfg.get("tc_compare", True):
        return '<div id="tcAlarm" class="alarm simulation-ok" style="display:block;">TC COMPARE OFF</div>'

    tolerance = int(cfg.get("tc_tolerance_frames", 2))

    if not cfg.get("simulation", False):
        return '<div id="tcAlarm" class="alarm simulation-ok" style="display:block;">REAL MODE – TRANSPORT INFO AKTIVNÍ</div>'

    init_sim_states(len(cfg["decks"]))
    recording = []

    for i, deck in enumerate(cfg["decks"]):
        state = SIM_STATES.get(i, {})

        if state.get("status") not in ("RECORDING", "TC WARNING"):
            continue

        if not state.get("tc_valid", False):
            continue

        recording.append({
            "name": deck.get("name", f"REC {i + 1}"),
            "offset": int(state.get("offset", 0))
        })

    if len(recording) < 2:
        return '<div id="tcAlarm" class="alarm simulation-ok" style="display:block;">SIMULATION MODE – není co porovnávat</div>'

    ref = recording[0]
    bad = []

    for item in recording[1:]:
        diff = item["offset"] - ref["offset"]

        if abs(diff) > tolerance:
            bad.append((item["name"], diff))

    if bad:
        parts = []

        for name, diff in bad:
            sign = "+" if diff > 0 else ""
            parts.append(f"{html.escape(name)} {sign}{diff}f")

        msg = ", ".join(parts)

        return (
            f'<div id="tcAlarm" class="alarm" style="display:block;">'
            f'TC MISMATCH – reference {html.escape(ref["name"])} | {msg} | tolerance {tolerance}f'
            f'</div>'
        )

    return (
        f'<div id="tcAlarm" class="alarm simulation-ok" style="display:block;">'
        f'SIMULATION MODE – TC OK | tolerance {tolerance}f'
        f'</div>'
    )


def transport_response_to_state(response):
    if not response.startswith("208 transport info"):
        raise OSError("bad transport response")

    info = parse_hyperdeck_info(response)

    status_raw = info.get("status", "").lower()
    input_format = info.get("input video format", "").lower()
    video_format = info.get("video format", "").lower()
    tc_text = info.get("display timecode") or info.get("timecode") or ""

    if input_format == "none":
        return "NO SIGNAL", "warning", "--:--:--:--", False, 0

    if status_raw in ("record", "recording"):
        status_text = "RECORDING"
        led_class = "recording"

    elif status_raw in ("stopped", "stop", "preview", "idle"):
        status_text = "STOPPED"
        led_class = "stopped"

    elif status_raw in ("play", "playing"):
        status_text = "PLAYING"
        led_class = "warning"

    else:
        status_text = status_raw.upper() if status_raw else "ONLINE"
        led_class = "warning"

    is_50fps = "50" in input_format or "50" in video_format
    total_frames = tc_text_to_25fps_frames(tc_text, is_50fps=is_50fps)

    if total_frames is None:
        return status_text, led_class, "--:--:--:--", False, 0

    return status_text, led_class, format_tc_x2_from_25fps_frames(total_frames), True, total_frames


STATUS_LOCK = threading.Lock()
STATUS_POLLERS = {}
STATUS_CACHE = {}
STATUS_POLL_INTERVAL = 1.0
STATUS_CONNECT_TIMEOUT = 1.0
STATUS_HELLO_TIMEOUT = 0.7
STATUS_TRANSPORT_TIMEOUT = 0.7


def get_skipped_state():
    return ("SKIPPED", "skipped", "--:--:--:--", False, 0)


def get_offline_state():
    return ("OFFLINE", "offline", "--:--:--:--", False, 0)


def status_poller_loop(index, ip, stop_event):
    sock = None

    def close_sock():
        nonlocal sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        sock = None

    def connect_socket():
        new_sock = socket.create_connection((ip, 9993), timeout=STATUS_CONNECT_TIMEOUT)
        new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        recv_hyperdeck_message(new_sock, timeout=STATUS_HELLO_TIMEOUT)
        return new_sock

    def read_transport_once():
        nonlocal sock

        if sock is None:
            sock = connect_socket()

        sock.sendall(b"transport info\r\n")
        response = recv_hyperdeck_message(sock, timeout=STATUS_TRANSPORT_TIMEOUT)
        return transport_response_to_state(response)

    while not stop_event.is_set():
        try:
            state = read_transport_once()

            with STATUS_LOCK:
                STATUS_CACHE[index] = state

            stop_event.wait(STATUS_POLL_INTERVAL)

        except Exception:
            # První chyba může být jen škobrtnutí spojení.
            # Hned socket zavřeme, obnovíme spojení a zkusíme transport info ještě jednou.
            close_sock()

            try:
                state = read_transport_once()

                with STATUS_LOCK:
                    STATUS_CACHE[index] = state

            except Exception:
                close_sock()

                with STATUS_LOCK:
                    STATUS_CACHE[index] = get_offline_state()

            stop_event.wait(STATUS_POLL_INTERVAL)

    close_sock()


def ensure_status_pollers(cfg):
    decks = cfg.get("decks", [])
    simulation = cfg.get("simulation", False)

    if simulation:
        desired = {}
    else:
        desired = {
            i: deck.get("ip", "").strip()
            for i, deck in enumerate(decks)
            if deck_is_armed(deck) and deck.get("ip", "").strip()
        }

    with STATUS_LOCK:
        existing = list(STATUS_POLLERS.items())

    # Zastavit pollery, které už nemají běžet nebo mají jinou IP.
    for index, item in existing:
        old_ip = item["ip"]
        stop_event = item["stop"]

        if index not in desired or desired[index] != old_ip:
            stop_event.set()

            with STATUS_LOCK:
                STATUS_POLLERS.pop(index, None)
                STATUS_CACHE[index] = get_skipped_state()

    # Nastavit skipped pro neaktivní rekordéry.
    with STATUS_LOCK:
        for i in range(len(decks)):
            if i not in desired and i not in STATUS_POLLERS:
                STATUS_CACHE[i] = get_skipped_state()

    # Spustit nové pollery.
    for index, ip in desired.items():
        with STATUS_LOCK:
            already_running = index in STATUS_POLLERS

        if already_running:
            continue

        stop_event = threading.Event()
        thread = threading.Thread(
            target=status_poller_loop,
            args=(index, ip, stop_event),
            daemon=True
        )

        with STATUS_LOCK:
            STATUS_CACHE[index] = get_offline_state()
            STATUS_POLLERS[index] = {
                "ip": ip,
                "stop": stop_event,
                "thread": thread,
            }

        thread.start()


def get_status_map(cfg):
    decks = cfg.get("decks", [])
    simulation = cfg.get("simulation", False)

    if simulation:
        init_sim_states(len(decks))

        return {
            i: get_sim_state(i) if deck_is_armed(deck) else get_skipped_state()
            for i, deck in enumerate(decks)
        }

    ensure_status_pollers(cfg)

    with STATUS_LOCK:
        cache = dict(STATUS_CACHE)

    return {
        i: cache.get(i, get_offline_state() if deck_is_armed(deck) else get_skipped_state())
        for i, deck in enumerate(decks)
    }



def make_rows(cfg):
    rows = ""
    simulation = cfg.get("simulation", False)

    if simulation:
        init_sim_states(len(cfg["decks"]))

    status_map = get_status_map(cfg)

    for i, deck in enumerate(cfg["decks"]):
        raw_name = deck.get("name", "")
        name = html.escape(raw_name)
        ip = html.escape(deck.get("ip", ""))

        pgm = is_pgm_deck(raw_name)
        armed = deck_is_armed(deck)

        row_classes = []

        if pgm:
            row_classes.append("pgm-row")

        if not armed:
            row_classes.append("disarmed-row")

        row_class = " ".join(row_classes)

        if pgm:
            arm_cell = '<td class="arm-cell always-arm"></td>'
        else:
            checked = "checked" if armed else ""

            arm_cell = f"""
  <td class="arm-cell">
    <form class="arm-form" method="POST" action="/toggle-arm">
      <input type="hidden" name="deck" value="{i}">
      <input class="arm-check" type="checkbox" name="armed" value="1" {checked} onchange="this.form.submit()">
    </form>
  </td>"""

        status_text, led_class, tc, tc_valid, total_frames = status_map.get(
            i,
            get_skipped_state()
        )

        if tc_valid:
            tc_cell = f'<td class="tc live-tc" data-role="tc" data-base-frame="{total_frames}">{tc}</td>'
        else:
            tc_cell = f'<td class="tc" data-role="tc">{tc}</td>'

        rows += f"""
<tr class="{row_class}" data-deck-index="{i}">
  {arm_cell}
  <td class="name">{name}</td>
  <td class="ip">{ip}</td>
  <td>
    <form class="inline-form" method="POST" action="/action">
      <input type="hidden" name="deck" value="{i}">
      <input type="hidden" name="cmd" value="rec">
      <button class="btn rec" type="submit">REC</button>
    </form>
  </td>
  <td>
    <form class="inline-form" method="POST" action="/action">
      <input type="hidden" name="deck" value="{i}">
      <input type="hidden" name="cmd" value="stop">
      <button class="btn stop" type="submit">STOP</button>
    </form>
  </td>
  <td class="status" data-role="status"><span class="led {led_class}"></span> {status_text}</td>
  {tc_cell}
</tr>
"""

    return rows


def make_settings_rows(cfg):
    rows = ""

    for i, deck in enumerate(cfg["decks"]):
        name = html.escape(deck.get("name", ""), quote=True)
        ip = html.escape(deck.get("ip", ""), quote=True)
        row_class = "pgm-row" if is_pgm_deck(name) else ""

        rows += f"""
<tr class="{row_class}">
  <td>
    <input class="text-input name-input" type="text" name="deck_name_{i}" value="{name}">
  </td>
  <td>
    <input class="text-input ip-input" type="text" name="deck_ip_{i}" value="{ip}">
  </td>
</tr>
"""

    return rows


HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>HyperDeck Panel</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#09090d;color:#eee;font-family:Arial,sans-serif;overflow-y:auto;overflow-x:hidden}
.wrap{width:100vw;min-height:100vh;padding:12px 18px 40px}
.header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:10px}
h1{margin:0;font-size:30px;letter-spacing:2px;white-space:nowrap}
.topnav{display:flex;justify-content:center;gap:12px}
.navbtn{border:1px solid #444;border-radius:10px;padding:10px 22px;background:#242432;color:#eee;font-size:18px;font-weight:bold;cursor:pointer}
.navbtn.active{background:#1b6cff;border-color:#7dabff;box-shadow:0 0 14px #1b6cff}
.notice{margin-bottom:10px;padding:10px;border-radius:10px;background:#113b20;color:#78ff9c;text-align:center;font-size:22px;font-weight:bold;box-shadow:0 0 14px #008d35}
.alarm{display:none;margin-bottom:10px;padding:10px;border-radius:10px;background:#7a0000;color:#fff;text-align:center;font-size:24px;font-weight:bold;box-shadow:0 0 18px #f33}
.simulation-ok{background:#113b20;color:#78ff9c;box-shadow:0 0 14px #008d35}
.view{display:none}
.view.active{display:block}
.panel-card{background:#15151d;border:1px solid #33333d;border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th,td{padding:8px 10px;border-bottom:1px solid #33333d;text-align:center;font-size:19px}
tr:last-child td{border-bottom:none}
th{color:#a8a8b5;font-size:14px;text-transform:uppercase;letter-spacing:1px;background:#1d1d28}
/* column layout start */
th:nth-child(1),td:nth-child(1){width:10%}
th:nth-child(2),td:nth-child(2){width:13%}
th:nth-child(3),td:nth-child(3){width:13%}
th:nth-child(4),td:nth-child(4){width:13%}
th:nth-child(5),td:nth-child(5){width:13%}
th:nth-child(6),td:nth-child(6){width:18%}
th:nth-child(7),td:nth-child(7){width:20%}
/* column layout end */






.arm-cell{width:auto;padding-left:82px;padding-right:52px;text-align:center}
.arm-form{margin:0}
.arm-check{width:24px;height:24px;cursor:pointer;vertical-align:-5px}
.always-arm{font-size:14px;font-weight:bold;color:#78ff9c;letter-spacing:1px}
.name{font-weight:bold;text-align:center;font-size:23px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ip{color:#b8b8c0;font-size:16px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tc{font-family:monospace;font-size:29px;letter-spacing:1px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status{font-weight:bold;width:auto;min-width:0;max-width:none;text-align:left;padding-left:82px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.led{display:inline-block;width:22px;height:22px;border-radius:50%;margin-right:10px;vertical-align:-3px;background:#777;box-shadow:0 0 10px #777}
.stopped{background:#1b6cff;box-shadow:0 0 18px #1b6cff}
.recording{background:#ff2b2b;box-shadow:0 0 18px #ff2b2b}
.offline{background:#f33;box-shadow:0 0 18px #f33}
.warning{background:#fc3;box-shadow:0 0 18px #fc3}
.skipped{background:#555;box-shadow:none}
.inline-form{margin:0;display:inline}
.btn{border:none;border-radius:9px;padding:10px 22px;font-size:19px;font-weight:bold;color:#fff;cursor:pointer;min-width:92px}
.rec{background:#b00020}
.rec:hover{background:#d00030}
.stop{background:#0057c8}
.stop:hover{background:#0b6cff}
.pgm-row td{background:#202018;border-top:3px solid #777746}
.pgm-row .name::after{content:""}
.disarmed-row td{color:#777!important;filter:grayscale(1);opacity:.42}
.disarmed-row .btn{filter:grayscale(1)}
.bottom{display:flex;justify-content:center;gap:34px;margin-top:18px}
.big{width:270px;height:66px;font-size:28px;border-radius:14px}
.logger-frame{width:100%;height:calc(100vh - 116px);border:1px solid #444;border-radius:12px;background:#000}
.logger-note{text-align:center;color:#aaa;margin-bottom:8px;font-size:15px}
.settings-note{margin-top:14px;text-align:center;color:#aaa;font-size:18px}
.settings-form{max-width:760px;margin:0 auto}
.settings-form table{margin-top:10px}
.text-input{width:100%;background:#0f0f15;color:#eee;border:1px solid #555;border-radius:8px;padding:10px 12px;font-size:22px}
.name-input{font-weight:bold}
.ip-input{font-family:monospace;text-align:center}
.save-row{display:flex;justify-content:center;gap:24px;margin-top:20px}
.save-btn{background:#1b6cff;width:260px;height:62px;font-size:26px;border-radius:14px}
.small-label{color:#aaa;font-size:15px;margin-bottom:6px;text-align:center}
.settings-switch-row{display:flex;justify-content:center;gap:28px;margin:14px 0}
.check-label{font-size:20px;font-weight:bold;color:#eee;white-space:nowrap}
.check-label input{width:20px;height:20px;vertical-align:-3px;margin-right:8px}
.footer{position:fixed;left:0;right:0;bottom:4px;text-align:center;color:#777;font-size:13px;pointer-events:none}
@media(max-height:760px){
  h1{font-size:26px}
  th,td{padding:6px 8px;font-size:17px}
  .tc{font-size:25px}
  .name{font-size:20px}
  .btn{padding:8px 18px;font-size:17px}
  .big{height:58px;font-size:25px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>HYPERDECK CONTROL</h1>
    <div class="topnav">
      <button id="btnDecks" class="navbtn active" onclick="showView('decks')">HYPERDECK</button>
      <button id="btnLogger" class="navbtn" onclick="showView('logger')">ATEM LOGGER</button>
      <button id="btnSettings" class="navbtn" onclick="showView('settings')">SETTINGS</button>
    </div>
  </div>

  __NOTICE__
  __ALARM__

  <div id="viewDecks" class="view active">
    <div class="panel-card">
      <table>
        <thead>
          <tr>
            <th></th>
            <th>Deck</th>
            <th>IP</th>
            <th>Rec</th>
            <th>Stop</th>
            <th>Status</th>
            <th>Timecode</th>
          </tr>
        </thead>
        <tbody>
          __ROWS__
        </tbody>
      </table>
    </div>

    <div class="bottom">
      <form class="inline-form" method="POST" action="/action">
        <input type="hidden" name="cmd" value="rec_all">
        <button class="btn rec big" type="submit">REC ALL</button>
      </form>

      <form class="inline-form" method="POST" action="/action">
        <input type="hidden" name="cmd" value="stop_all">
        <button class="btn stop big" type="submit">STOP ALL</button>
      </form>
    </div>
  </div>

  <div id="viewLogger" class="view">
    <div class="logger-note">ATEM logger: http://__ATEM_LOGGER_IP__</div>
    <iframe class="logger-frame" src="http://__ATEM_LOGGER_IP__"></iframe>
  </div>

  <div id="viewSettings" class="view">
    <form class="settings-form" method="POST" action="/save-settings">
      <div class="small-label">HYPERDECK CONTROL IP</div>
      <input class="text-input ip-input" type="text" name="panel_ip" value="__PANEL_IP__">

      <div class="small-label" style="margin-top:12px;">ATEM LOGGER IP</div>
      <input class="text-input ip-input" type="text" name="atem_logger_ip" value="__ATEM_LOGGER_IP__">

      <div class="settings-switch-row">
        <label class="check-label">
          <input type="checkbox" name="simulation" value="1" __SIMULATION_CHECKED__>
          SIMULATION MODE
        </label>

        <label class="check-label">
          <input type="checkbox" name="tc_compare" value="1" __TC_COMPARE_CHECKED__>
          TC COMPARE
        </label>
      </div>

      <div class="small-label">TC TOLERANCE FRAMES</div>
      <input class="text-input ip-input" type="number" min="0" max="50" name="tc_tolerance_frames" value="__TC_TOLERANCE_FRAMES__">

      <div class="panel-card">
        <table>
          <thead>
            <tr>
              <th>Deck name</th>
              <th>IP address</th>
            </tr>
          </thead>
          <tbody>
            __SETTINGS_ROWS__
          </tbody>
        </table>
      </div>

      <div class="save-row">
        <button class="btn save-btn" type="submit">ULOŽIT</button>
      </div>

      <div class="settings-note">
        Pozor: HYPERDECK CONTROL IP mění zatím jen hodnotu uloženou v konfiguraci panelu.
        Skutečná IP adresa Raspberry se nastavuje v systému.
      </div>
    </form>
  </div>

  <div class="footer">
    RPi __PANEL_IP__ | ATEM logger __ATEM_LOGGER_IP__ | Tento program napsala Astra, Bobova AI asistentka.
  </div>
</div>

<script>
function showView(name) {
  document.getElementById("viewDecks").classList.remove("active");
  document.getElementById("viewLogger").classList.remove("active");
  document.getElementById("viewSettings").classList.remove("active");

  document.getElementById("btnDecks").classList.remove("active");
  document.getElementById("btnLogger").classList.remove("active");
  document.getElementById("btnSettings").classList.remove("active");

  if (name === "decks") {
    document.getElementById("viewDecks").classList.add("active");
    document.getElementById("btnDecks").classList.add("active");
  }

  if (name === "logger") {
    document.getElementById("viewLogger").classList.add("active");
    document.getElementById("btnLogger").classList.add("active");
  }

  if (name === "settings") {
    document.getElementById("viewSettings").classList.add("active");
    document.getElementById("btnSettings").classList.add("active");
  }
}

function formatTCx2From25Frames(totalFrames) {
  const fps = 25;
  const dayFrames = 24 * 60 * 60 * fps;

  totalFrames = ((totalFrames % dayFrames) + dayFrames) % dayFrames;

  const hours = Math.floor(totalFrames / (60 * 60 * fps));
  totalFrames %= 60 * 60 * fps;

  const minutes = Math.floor(totalFrames / (60 * fps));
  totalFrames %= 60 * fps;

  const seconds = Math.floor(totalFrames / fps);
  const frame25 = totalFrames % fps;
  const frameX2 = frame25 * 2;

  const pad = (n) => String(n).padStart(2, "0");

  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}:${pad(frameX2)}`;
}

function initLiveTC() {
  document.querySelectorAll(".live-tc").forEach((el) => {
    el.dataset.startMs = String(Date.now());
    el.dataset.startFrame = el.dataset.baseFrame;
  });
}

function updateLiveTC() {
  const now = Date.now();

  document.querySelectorAll(".live-tc").forEach((el) => {
    const startMs = Number(el.dataset.startMs || now);
    const startFrame = Number(el.dataset.startFrame || 0);
    const elapsedFrames = Math.floor((now - startMs) / 40);
    const currentFrame = startFrame + elapsedFrames;

    el.textContent = formatTCx2From25Frames(currentFrame);
  });
}

initLiveTC();
setInterval(updateLiveTC, 40);

const STATUS_POLL_ENABLED = __STATUS_POLL_ENABLED__;
let statusFetchBusy = false;

function applyStatusData(data) {
  if (data.alarm_html) {
    const alarm = document.getElementById("tcAlarm");
    if (alarm) {
      alarm.outerHTML = data.alarm_html;
    }
  }

  data.decks.forEach((deck) => {
    const row = document.querySelector(`tr[data-deck-index="${deck.index}"]`);

    if (!row) {
      return;
    }

    const statusCell = row.querySelector('[data-role="status"]');

    if (statusCell) {
      statusCell.innerHTML = `<span class="led ${deck.led}"></span> ${deck.status}`;
    }

    const tcCell = row.querySelector('[data-role="tc"]');

    if (tcCell) {
      if (deck.tc_valid) {
        tcCell.classList.add("live-tc");
        tcCell.dataset.baseFrame = String(deck.total_frames);
        tcCell.dataset.startFrame = String(deck.total_frames);
        tcCell.dataset.startMs = String(Date.now());
        tcCell.textContent = deck.tc;
      } else {
        tcCell.classList.remove("live-tc");
        delete tcCell.dataset.baseFrame;
        delete tcCell.dataset.startFrame;
        delete tcCell.dataset.startMs;
        tcCell.textContent = deck.tc;
      }
    }
  });
}

async function pollStatus() {
  if (!STATUS_POLL_ENABLED || statusFetchBusy) {
    return;
  }

  const deckView = document.getElementById("viewDecks");

  if (!deckView || !deckView.classList.contains("active")) {
    return;
  }

  statusFetchBusy = true;

  try {
    const response = await fetch("/api/status", {cache: "no-store"});

    if (response.ok) {
      const data = await response.json();
      applyStatusData(data);
    }

  } catch (err) {
    console.log("Status update error:", err);

  } finally {
    statusFetchBusy = false;
  }
}

setInterval(pollStatus, 1000);

setTimeout(() => {
  const notice = document.querySelector(".notice");

  if (notice) {
    notice.style.display = "none";
    window.history.replaceState({}, document.title, "/");
  }
}, 2500);
</script>
</body>
</html>
"""


def build_status_api():
    cfg = load_config()
    simulation = cfg.get("simulation", False)
    decks_out = []
    status_map = get_status_map(cfg)

    for i, deck in enumerate(cfg["decks"]):
        status_text, led_class, tc, tc_valid, total_frames = status_map.get(
            i,
            get_skipped_state()
        )

        decks_out.append({
            "index": i,
            "name": deck.get("name", f"REC {i + 1}"),
            "ip": deck.get("ip", ""),
            "status": status_text,
            "led": led_class,
            "tc": tc,
            "tc_valid": bool(tc_valid),
            "total_frames": int(total_frames),
            "armed": deck_is_armed(deck)
        })

    return {
        "simulation": simulation,
        "alarm_html": make_alarm(cfg),
        "decks": decks_out
    }


def build_page(saved=False):
    cfg = load_config()

    panel_ip = html.escape(cfg.get("panel_ip", "10.0.0.8"))
    atem_logger_ip = html.escape(cfg.get("atem_logger_ip", "10.0.0.9"))

    notice = ""
    if saved:
        notice = '<div class="notice">Nastavení uloženo.</div>'

    simulation_checked = "checked" if cfg.get("simulation", False) else ""
    tc_compare_checked = "checked" if cfg.get("tc_compare", True) else ""
    tc_tolerance_frames = html.escape(str(cfg.get("tc_tolerance_frames", 2)))
    status_poll_enabled = "false" if cfg.get("simulation", False) else "true"

    page = HTML_TEMPLATE
    page = page.replace("__NOTICE__", notice)
    page = page.replace("__ALARM__", make_alarm(cfg))
    page = page.replace("__ROWS__", make_rows(cfg))
    page = page.replace("__SETTINGS_ROWS__", make_settings_rows(cfg))
    page = page.replace("__PANEL_IP__", panel_ip)
    page = page.replace("__ATEM_LOGGER_IP__", atem_logger_ip)
    page = page.replace("__SIMULATION_CHECKED__", simulation_checked)
    page = page.replace("__TC_COMPARE_CHECKED__", tc_compare_checked)
    page = page.replace("__TC_TOLERANCE_FRAMES__", tc_tolerance_frames)
    page = page.replace("__STATUS_POLL_ENABLED__", status_poll_enabled)

    return page


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/status":
            payload = json.dumps(build_status_api(), ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        saved = "saved=1" in parsed.query

        page = build_page(saved=saved).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)

        if self.path == "/toggle-arm":
            cfg = load_config()

            try:
                deck_index = int(form.get("deck", ["-1"])[0])

                if 0 <= deck_index < len(cfg["decks"]):
                    deck = cfg["decks"][deck_index]

                    if not is_pgm_deck(deck.get("name", "")):
                        deck["armed"] = "armed" in form
                        cfg["decks"][deck_index] = deck
                        save_config(cfg)

            except ValueError:
                pass

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path == "/action":
            cfg = load_config()
            simulation = cfg.get("simulation", False)
            cmd = form.get("cmd", [""])[0]

            if simulation:
                init_sim_states(len(cfg["decks"]))

                if cmd == "rec_all":
                    for i in get_armed_deck_indexes(cfg):
                        set_sim_deck_status(i, "rec")

                elif cmd == "stop_all":
                    for i in get_armed_deck_indexes(cfg):
                        set_sim_deck_status(i, "stop")

                elif cmd in ("rec", "stop"):
                    try:
                        deck_index = int(form.get("deck", ["-1"])[0])
                        set_sim_deck_status(deck_index, cmd)
                    except ValueError:
                        pass

            else:
                if cmd == "rec_all":
                    send_hyperdeck_commands_parallel(get_armed_decks(cfg), "rec")

                elif cmd == "stop_all":
                    send_hyperdeck_commands_parallel(get_armed_decks(cfg), "stop")

                elif cmd in ("rec", "stop"):
                    try:
                        deck_index = int(form.get("deck", ["-1"])[0])

                        if 0 <= deck_index < len(cfg["decks"]):
                            send_hyperdeck_command(cfg["decks"][deck_index], cmd)

                    except ValueError:
                        pass

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path == "/save-settings":
            cfg = load_config()

            cfg["panel_ip"] = form.get(
                "panel_ip",
                [cfg.get("panel_ip", "10.0.0.8")]
            )[0].strip()

            cfg["atem_logger_ip"] = form.get(
                "atem_logger_ip",
                [cfg.get("atem_logger_ip", "10.0.0.9")]
            )[0].strip()

            cfg["simulation"] = "simulation" in form
            cfg["tc_compare"] = "tc_compare" in form

            try:
                tolerance = int(form.get(
                    "tc_tolerance_frames",
                    [cfg.get("tc_tolerance_frames", 2)]
                )[0])

                cfg["tc_tolerance_frames"] = max(0, min(50, tolerance))

            except ValueError:
                cfg["tc_tolerance_frames"] = 2

            new_decks = []

            for i, old_deck in enumerate(cfg["decks"]):
                name = form.get(
                    f"deck_name_{i}",
                    [old_deck.get("name", f"REC {i + 1}")]
                )[0].strip()

                ip = form.get(
                    f"deck_ip_{i}",
                    [old_deck.get("ip", "")]
                )[0].strip()

                if not name:
                    name = old_deck.get("name", f"REC {i + 1}")

                armed = deck_is_armed(old_deck)

                if is_pgm_deck(name):
                    armed = True

                new_decks.append({
                    "name": name,
                    "ip": ip,
                    "armed": armed
                })

            cfg["decks"] = new_decks
            save_config(cfg)

            self.send_response(303)
            self.send_header("Location", "/?saved=1")
            self.end_headers()
            return

        self.send_error(404)

    def log_message(self, format, *args):
        pass


server = HTTPServer((HOST, PORT), Handler)
print(f"HyperDeck Panel bezi na http://10.0.0.8:{PORT}")
server.serve_forever()
