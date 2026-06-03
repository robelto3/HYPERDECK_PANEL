from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import html
import json
import socket
import time
import threading
import copy
import os
import ipaddress
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

HOST = "0.0.0.0"
PORT = 8080
CONFIG_FILE = Path("config.json")
PANEL_BROWSER_URL = f"http://127.0.0.1:{PORT}/"

DEFAULT_CONFIG = {
    "panel_ip": "10.0.0.8",
    "atem_logger_ip": "10.0.0.9",
    "expected_format": "1080p50",
    "tc_reference_mode": "group",
    "tc_compare": True,
    "tc_tolerance_frames": 2,
    "decks": [
        {"name": "Cam 1", "ip": "10.0.0.11"},
        {"name": "Cam 2", "ip": "10.0.0.12"},
        {"name": "Cam 3", "ip": "10.0.0.13"},
        {"name": "Cam 4", "ip": "10.0.0.14"},
        {"name": "Cam 5", "ip": "10.0.0.15"},
        {"name": "Cam 6", "ip": "10.0.0.16"},
        {"name": "Cam 7", "ip": "10.0.0.21"},
        {"name": "Cam 8", "ip": "10.0.0.22"},
        {"name": "Cam 9", "ip": ""},
        {"name": "Cam 10", "ip": ""},
        {"name": "Režie", "ip": "10.0.0.24"}
    ]
}


def transport_response_to_state(response):
    if not response.startswith("208 transport info"):
        raise OSError("bad transport response")

    info = parse_hyperdeck_info(response)

    status_raw = info.get("status", "").lower()
    input_format = info.get("input video format", "").lower()
    video_format = info.get("video format", "").lower()
    tc_text = info.get("display timecode") or info.get("timecode") or ""
    format_text = input_format or video_format or "---"

    if input_format == "none":
        return "NO SIGNAL", "warning", format_text, "--:--:--:--", False, 0

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
        return status_text, led_class, format_text, "--:--:--:--", False, 0

    return status_text, led_class, format_text, frames_to_tcx2(total_frames), True, total_frames


STATUS_LOCK = threading.Lock()
STATUS_POLLERS = {}
STATUS_POLLERS_PAUSED = False
STATUS_CACHE = {}
STATUS_POLL_INTERVAL = 0.5
STATUS_CONNECT_TIMEOUT = 1.0
STATUS_HELLO_TIMEOUT = 0.7
STATUS_TRANSPORT_TIMEOUT = 0.7


def get_skipped_state():
    return ("SKIPPED", "skipped", "---", "--:--:--:--", False, 0)


def get_offline_state():
    return ("OFFLINE", "offline", "---", "--:--:--:--", False, 0)


def make_status_cache_entry(state):
    return {
        "state": state,
        "updated_monotonic": time.monotonic()
    }


def normalize_state_to_snapshot(state, updated_monotonic, snapshot_monotonic):
    status_text, led_class, format_text, tc, tc_valid, total_frames = state

    if not tc_valid:
        return state

    elapsed_seconds = max(0.0, snapshot_monotonic - updated_monotonic)
    elapsed_frames = int(elapsed_seconds * 25)
    normalized_frames = int(total_frames) + elapsed_frames

    return (
        status_text,
        led_class,
        format_text,
        frames_to_tcx2(normalized_frames),
        tc_valid,
        normalized_frames
    )


def read_status_cache_entry(entry, snapshot_monotonic):
    if isinstance(entry, dict):
        state = entry.get("state", get_offline_state())
        updated_monotonic = entry.get("updated_monotonic", snapshot_monotonic)
        return normalize_state_to_snapshot(state, updated_monotonic, snapshot_monotonic)

    return entry


def status_poller_loop(index, ip, stop_event):
    sock = None
    fail_count = 0

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
            fail_count = 0

            with STATUS_LOCK:
                STATUS_CACHE[index] = make_status_cache_entry(state)

            stop_event.wait(STATUS_POLL_INTERVAL)

        except Exception:
            # První chyba může být jen škobrtnutí spojení.
            # Hned socket zavřeme, obnovíme spojení a zkusíme transport info ještě jednou.
            close_sock()

            try:
                state = read_transport_once()
                fail_count = 0

                with STATUS_LOCK:
                    STATUS_CACHE[index] = make_status_cache_entry(state)

            except Exception:
                close_sock()
                fail_count += 1

                # Jedno krátké škobrtnutí spojení neukazujeme jako OFFLINE.
                # OFFLINE ukážeme až po dvou selháních za sebou.
                if fail_count >= 2:
                    with STATUS_LOCK:
                        STATUS_CACHE[index] = make_status_cache_entry(get_offline_state())

            stop_event.wait(STATUS_POLL_INTERVAL)

    close_sock()


def ensure_status_pollers(cfg):
    if STATUS_POLLERS_PAUSED:
        return
    decks = cfg.get("decks", [])
    desired = {
        i: deck.get("ip", "").strip()
        for i, deck in enumerate(decks)
        if deck_is_armed_at_index(cfg, i, deck) and deck.get("ip", "").strip()
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
                STATUS_CACHE[index] = make_status_cache_entry(get_skipped_state())

    # Nastavit skipped pro neaktivní rekordéry.
    with STATUS_LOCK:
        for i in range(len(decks)):
            if i not in desired and i not in STATUS_POLLERS:
                STATUS_CACHE[i] = make_status_cache_entry(get_skipped_state())

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
            STATUS_CACHE[index] = make_status_cache_entry(get_offline_state())
            STATUS_POLLERS[index] = {
                "ip": ip,
                "stop": stop_event,
                "thread": thread,
            }

        thread.start()


def get_status_map(cfg):
    decks = cfg.get("decks", [])
    result = {}

    if not decks:
        return result

    ensure_status_pollers(cfg)
    snapshot_monotonic = time.monotonic()

    with STATUS_LOCK:
        cached = STATUS_CACHE.copy()

    for i, deck in enumerate(decks):
        if deck_is_armed_at_index(cfg, i, deck):
            entry = cached.get(i, make_status_cache_entry(get_offline_state()))
            result[i] = read_status_cache_entry(entry, snapshot_monotonic)
        else:
            result[i] = get_skipped_state()

    return result


def build_tc_compare(cfg, status_map):
    result = {
        "enabled": bool(cfg.get("tc_compare", True)),
        "tolerance": int(cfg.get("tc_tolerance_frames", 2)),
        "reference": None,
        "bad": [],
        "by_index": {},
        "ambiguous": False,
        "reference_invalid": False,
        "reference_mode": normalize_tc_reference_mode(cfg.get("tc_reference_mode", "group"))
    }

    if not result["enabled"]:
        return result

    candidates = []

    for i, deck in enumerate(cfg.get("decks", [])):
        status = status_map.get(i, get_skipped_state())
        status_text, led_class, format_text, tc, tc_valid, total_frames = status

        if not deck_is_armed_at_index(cfg, i, deck):
            continue

        if not tc_valid:
            continue

        candidates.append({
            "index": i,
            "name": deck.get("name", f"REC {i + 1}"),
            "frames": int(total_frames)
        })

    if len(candidates) < 2:
        return result

    if result["reference_mode"] == "control_room":
        control_room_index = len(cfg.get("decks", [])) - 1
        reference = None

        for candidate in candidates:
            if candidate["index"] == control_room_index:
                reference = candidate
                break

        if reference is None:
            result["reference_invalid"] = True
            return result

        result["reference"] = reference

        for item in candidates:
            diff = item["frames"] - reference["frames"]
            warning = item["index"] != reference["index"] and abs(diff) > result["tolerance"]

            result["by_index"][item["index"]] = {
                "diff": diff,
                "warning": warning
            }

            if warning:
                result["bad"].append({
                    "name": item["name"],
                    "diff": diff
                })

        return result

    groups = []

    for candidate in candidates:
        placed = False

        for group in groups:
            if abs(candidate["frames"] - group["reference"]["frames"]) <= result["tolerance"]:
                group["items"].append(candidate)
                placed = True
                break

        if not placed:
            groups.append({
                "reference": candidate,
                "items": [candidate]
            })

    groups.sort(key=lambda group: len(group["items"]), reverse=True)
    largest_size = len(groups[0]["items"])
    largest_groups = [
        group
        for group in groups
        if len(group["items"]) == largest_size
    ]
    result["ambiguous"] = len(largest_groups) > 1

    reference = groups[0]["reference"]
    result["reference"] = reference
    reference_indexes = {
        item["index"]
        for item in groups[0]["items"]
    }

    for item in candidates:
        diff = item["frames"] - reference["frames"]
        warning = item["index"] not in reference_indexes

        result["by_index"][item["index"]] = {
            "diff": diff,
            "warning": warning
        }

        if warning:
            result["bad"].append({
                "name": item["name"],
                "diff": diff
            })

    return result


def format_tc_diff(diff):
    if abs(diff) > 50:
        return "+50+f" if diff > 0 else "-50+f"

    sign = "+" if diff > 0 else ""
    return f"{sign}{diff}f"


def normalize_tc_reference_mode(value):
    value = (value or "").strip().lower()

    if value in ("group", "control_room"):
        return value

    return "group"



TC_INPUT_VALUES = ("embedded", "external", "internal")

TC_INPUT_LABELS = {
    "embedded": "Video input",
    "external": "External",
    "internal": "Internal",
}


def normalize_ipv4(value, fallback="", allow_empty=False):
    value = (value or "").strip()

    if allow_empty and not value:
        return ""

    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return fallback

    if ip.version != 4:
        return fallback

    return str(ip)


def normalize_format(value, fallback="1080p50"):
    value = (value or "").strip()

    if not value:
        return fallback

    return value.lower()


def format_is_ok(format_text, expected_format="1080p50"):
    expected = normalize_format(expected_format)
    return (format_text or "").strip().lower() == expected




def save_config(cfg):
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp_file = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")

    with tmp_file.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_file, CONFIG_FILE)

    try:
        dir_fd = os.open(str(CONFIG_FILE.parent), os.O_DIRECTORY)
    except OSError:
        return

    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_config():
    if not CONFIG_FILE.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config(cfg)
    else:
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = copy.deepcopy(DEFAULT_CONFIG)

    cfg.setdefault("panel_ip", "10.0.0.8")
    cfg.setdefault("atem_logger_ip", "10.0.0.9")
    cfg.setdefault("expected_format", "1080p50")
    cfg.setdefault("tc_reference_mode", "group")
    cfg.setdefault("tc_compare", True)
    cfg.setdefault("tc_tolerance_frames", 2)
    cfg.setdefault("decks", copy.deepcopy(DEFAULT_CONFIG["decks"]))

    for index, deck in enumerate(cfg["decks"]):
        deck.setdefault("armed", True)

        if is_control_room_deck(cfg, index):
            deck["armed"] = True

    return cfg


def is_control_room_deck(cfg, index):
    decks = cfg.get("decks", [])
    return bool(decks) and index == len(decks) - 1


def deck_is_armed(deck):
    return bool(deck.get("armed", True))


def deck_is_armed_at_index(cfg, index, deck):
    if is_control_room_deck(cfg, index):
        return True

    return deck_is_armed(deck)


def get_armed_decks(cfg):
    return [
        deck
        for index, deck in enumerate(cfg.get("decks", []))
        if deck_is_armed_at_index(cfg, index, deck)
    ]


def get_armed_deck_indexes(cfg):
    return [
        index
        for index, deck in enumerate(cfg.get("decks", []))
        if deck_is_armed_at_index(cfg, index, deck)
    ]


def get_active_count_text(cfg):
    decks = cfg.get("decks", [])
    active_cameras = 0

    for index, deck in enumerate(decks):
        if is_control_room_deck(cfg, index):
            continue

        if deck_is_armed_at_index(cfg, index, deck):
            active_cameras += 1

    control_room_count = 1 if decks else 0
    return f"{active_cameras}+{control_room_count}"


def frames_to_tcx2(total_frames, fps=25):
    total_frames %= 24 * 60 * 60 * fps

    hh = total_frames // (60 * 60 * fps)
    total_frames %= 60 * 60 * fps

    mm = total_frames // (60 * fps)
    total_frames %= 60 * fps

    ss = total_frames // fps
    ff = (total_frames % fps) * 2

    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


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
        return "OFFLINE", "offline", "---", "--:--:--:--", False, 0

    try:
        with socket.create_connection((ip, 9993), timeout=0.25) as sock:
            recv_hyperdeck_message(sock, timeout=0.4)

            sock.sendall(b"transport info\r\n")
            response = recv_hyperdeck_message(sock, timeout=0.6)

    except OSError:
        return "OFFLINE", "offline", "---", "--:--:--:--", False, 0

    info = parse_hyperdeck_info(response)

    status_raw = info.get("status", "").lower()
    input_format = info.get("input video format", "").lower()
    video_format = info.get("video format", "").lower()
    tc_text = info.get("display timecode") or info.get("timecode") or ""
    format_text = input_format or video_format or "---"

    if input_format == "none":
        return "NO SIGNAL", "warning", format_text, "--:--:--:--", False, 0

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
        return status_text, led_class, format_text, "--:--:--:--", False, 0

    return status_text, led_class, format_text, frames_to_tcx2(total_frames), True, total_frames


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


def get_hyperdeck_timecode_input(deck):
    ip = deck.get("ip", "").strip()

    if not ip:
        return "---"

    try:
        with socket.create_connection((ip, 9993), timeout=0.35) as sock:
            recv_hyperdeck_message(sock, timeout=0.25)

            sock.sendall(b"configuration\r\n")
            response = recv_hyperdeck_message(sock, timeout=0.7)

    except OSError:
        return "---"

    info = parse_hyperdeck_info(response)

    value = info.get("timecode input", "").strip().lower()

    if value in TC_INPUT_VALUES:
        return value

    return "---"


def resume_status_pollers():
    global STATUS_POLLERS_PAUSED
    STATUS_POLLERS_PAUSED = False


def stop_all_status_pollers(pause_seconds=0.4):
    global STATUS_POLLERS_PAUSED
    STATUS_POLLERS_PAUSED = True
    items = []

    with STATUS_LOCK:
        items = list(STATUS_POLLERS.items())
        STATUS_POLLERS.clear()

    for index, info in items:
        stop_event = info.get("stop_event") or info.get("stop") or info.get("event")

        if stop_event is not None:
            stop_event.set()

    for index, info in items:
        thread = info.get("thread")

        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    if pause_seconds > 0:
        time.sleep(pause_seconds)


def get_tc_input_map(cfg):
    result = {}

    for i, deck in enumerate(cfg.get("decks", [])):
        if deck_is_armed_at_index(cfg, i, deck):
            result[i] = get_hyperdeck_timecode_input(deck)
        else:
            result[i] = "---"

    return result


def set_hyperdeck_timecode_input(deck, tc_input):
    tc_input = tc_input.strip().lower()

    if tc_input not in TC_INPUT_VALUES:
        return False, "bad timecode input"

    ip = deck.get("ip", "").strip()

    if not ip:
        return False, "missing ip"

    try:
        with socket.create_connection((ip, 9993), timeout=1.0) as sock:
            recv_hyperdeck_message(sock, timeout=0.4)

            sock.sendall(b"remote: override: true\r\n")
            recv_hyperdeck_message(sock, timeout=0.5)

            cmd = f"configuration: timecode input: {tc_input}\r\n"
            sock.sendall(cmd.encode("ascii"))
            response = recv_hyperdeck_message(sock, timeout=1.0)

    except OSError as e:
        return False, str(e)

    return "200 ok" in response.lower(), response.strip()


def tc_input_label(value):
    value = (value or "---").strip().lower()

    if value in TC_INPUT_LABELS:
        return TC_INPUT_LABELS[value]

    return "---"


def make_tc_input_select(index, current_value="---"):
    current_value = (current_value or "---").strip().lower()
    current_label = html.escape(tc_input_label(current_value), quote=True)

    # První položka ukazuje aktuální stav, ale value="" znamená: neposílat změnu.
    options = [f'<option value="" selected>{current_label}</option>']

    for value in TC_INPUT_VALUES:
        if value == current_value:
            continue

        label = html.escape(TC_INPUT_LABELS.get(value, value), quote=True)
        options.append(f'<option value="{value}">{label}</option>')

    return (
        f'<select class="select-input tc-select" name="deck_tc_input_{index}">'
        + "".join(options)
        + '</select>'
    )


def make_alarm(cfg, status_map=None):
    if not cfg.get("tc_compare", True):
        return '<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">TC COMPARE OFF</div>'

    if status_map is None:
        status_map = get_status_map(cfg)

    tc_compare = build_tc_compare(cfg, status_map)

    if tc_compare.get("reference_invalid", False):
        return '<div id="tcAlarm" class="alarm" style="display:block;">TC REFERENCE INVALID – Režie nemá platný TC</div>'

    if not tc_compare["reference"]:
        return '<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">REAL MODE – není co porovnávat</div>'

    if tc_compare["bad"]:
        parts = []

        for item in tc_compare["bad"]:
            name = item["name"]
            diff = item["diff"]
            parts.append(f"{html.escape(name)} {format_tc_diff(diff)}")

        msg = ", ".join(parts)
        ref = tc_compare["reference"]
        tolerance = tc_compare["tolerance"]

        if tc_compare.get("ambiguous", False):
            return (
                f'<div id="tcAlarm" class="alarm" style="display:block;">'
                f'TC MISMATCH | {msg} | tolerance {tolerance}f'
                f'</div>'
            )

        return (
            f'<div id="tcAlarm" class="alarm" style="display:block;">'
            f'TC MISMATCH – reference {html.escape(ref["name"])} | {msg} | tolerance {tolerance}f'
            f'</div>'
        )

    tolerance = tc_compare["tolerance"]

    return (
        f'<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">'
        f'REAL MODE – TC OK | tolerance {tolerance}f'
        f'</div>'
    )


def make_rows(cfg):
    rows = ""
    expected_format = cfg.get("expected_format", "1080p50")

    status_map = get_status_map(cfg)
    tc_compare = build_tc_compare(cfg, status_map)

    for i, deck in enumerate(cfg["decks"]):
        raw_name = deck.get("name", "")
        name = html.escape(raw_name)
        ip = html.escape(deck.get("ip", ""))

        pgm = is_control_room_deck(cfg, i)
        armed = deck_is_armed_at_index(cfg, i, deck)

        if not armed:
            continue

        row_classes = []

        if pgm:
            row_classes.append("pgm-row")

        row_class = " ".join(row_classes)

        status_text, led_class, format_text, tc, tc_valid, total_frames = status_map.get(
            i,
            get_skipped_state()
        )
        format_html = html.escape(format_text or "---")
        format_class = "format" if format_is_ok(format_text, expected_format) else "format format-warning"
        tc_warning = tc_compare["by_index"].get(i, {}).get("warning", False)
        tc_class = "tc tc-warning" if tc_warning else "tc"

        if tc_valid:
            tc_cell = f'<td class="{tc_class} live-tc" data-role="tc" data-base-frame="{total_frames}">{tc}</td>'
        else:
            tc_cell = f'<td class="{tc_class}" data-role="tc">{tc}</td>'

        rows += f"""
<tr class="{row_class}" data-deck-index="{i}">
  <td class="ip">{ip}</td>
  <td class="name">{name}</td>
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
  <td class="{format_class}" data-role="format">{format_html}</td>
  {tc_cell}
</tr>
"""

    return rows



def make_settings_rows(cfg, load_tc_inputs=False):
    rows = ""

    if load_tc_inputs:
        tc_inputs = get_tc_input_map(cfg)
    else:
        tc_inputs = {}

    for i, deck in enumerate(cfg["decks"]):
        raw_name = deck.get("name", "")
        name = html.escape(raw_name, quote=True)
        ip = html.escape(deck.get("ip", ""), quote=True)
        control_room = is_control_room_deck(cfg, i)
        row_class = "pgm-row" if control_room else ""
        current_tc = tc_inputs.get(i, "---")
        tc_select = make_tc_input_select(i, current_tc)

        if control_room:
            arm_cell = '<td class="arm-cell always-arm"></td>'
        else:
            checked = "checked" if deck_is_armed(deck) else ""
            arm_cell = (
                '<td class="arm-cell">'
                f'<input class="arm-check" type="checkbox" name="deck_armed_{i}" value="1" {checked}>'
                '</td>'
            )

        rows += f"""
<tr class="{row_class}">
  {arm_cell}
  <td>
    <input class="text-input name-input" type="text" name="deck_name_{i}" value="{name}">
  </td>
  <td>
    <input class="text-input ip-input" type="text" name="deck_ip_{i}" value="{ip}">
  </td>
  <td>
    {tc_select}
    <input type="hidden" name="deck_tc_original_{i}" value="{html.escape(current_tc, quote=True)}">
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
.active-count{flex:1;text-align:center;font-family:monospace;font-size:26px;font-weight:bold;color:#78ff9c;white-space:nowrap}
.topnav{display:flex;justify-content:center;align-items:center;gap:12px}
.navbtn{border:1px solid #444;border-radius:10px;padding:10px 22px;background:#242432;color:#eee;font-size:18px;font-weight:bold;cursor:pointer}
.navbtn.active{background:#1b6cff;border-color:#7dabff;box-shadow:0 0 14px #1b6cff}
.close-panel-btn{width:44px;height:44px;min-width:44px;padding:0;border:1px solid #5d2222;border-radius:4px;background:#3a1515;color:#fff;font-size:26px;font-weight:bold;line-height:1;cursor:pointer}
.close-panel-btn:hover{background:#7a0000;border-color:#ff6666;box-shadow:0 0 12px rgba(255,75,75,.55)}
.notice{
  position:fixed !important;
  top:14px !important;
  right:18px !important;
  z-index:99999 !important;
  margin:0 !important;
  padding:8px 14px !important;
  border-radius:10px !important;
  background:#113b20 !important;
  color:#78ff9c !important;
  text-align:center !important;
  font-size:16px !important;
  font-weight:bold !important;
  box-shadow:0 0 14px #008d35 !important;
  pointer-events:none !important;
  opacity:1;
  transform:translateY(0);
  transition:opacity .35s ease, transform .35s ease;
}
.notice.toast-hide{
  opacity:0;
  transform:translateY(-8px);
}
.alarm{display:none;margin-bottom:10px;padding:10px;border-radius:10px;background:#7a0000;color:#fff;text-align:center;font-size:24px;font-weight:bold;box-shadow:0 0 18px #f33}
.alarm-ok{background:#113b20;color:#78ff9c;box-shadow:0 0 14px #008d35}
.view{display:none}
.view.active{display:block}
.panel-card{background:#15151d;border:1px solid #33333d;border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th,td{padding:8px 10px;border-bottom:1px solid #33333d;text-align:center;font-size:19px}
tr:last-child td{border-bottom:none}
th{color:#a8a8b5;font-size:14px;text-transform:uppercase;letter-spacing:1px;background:#1d1d28}
/* column layout start */
.decks-table th:nth-child(1),.decks-table td:nth-child(1){width:14%}
.decks-table th:nth-child(2),.decks-table td:nth-child(2){width:13%}
.decks-table th:nth-child(3),.decks-table td:nth-child(3){width:12%}
.decks-table th:nth-child(4),.decks-table td:nth-child(4){width:12%}
.decks-table th:nth-child(5),.decks-table td:nth-child(5){width:18%}
.decks-table th:nth-child(6),.decks-table td:nth-child(6){width:13%}
.decks-table th:nth-child(7),.decks-table td:nth-child(7){width:18%}
.settings-table th:nth-child(1),.settings-table td:nth-child(1){width:10%}
.settings-table th:nth-child(2),.settings-table td:nth-child(2){width:31%}
.settings-table th:nth-child(3),.settings-table td:nth-child(3){width:31%}
.settings-table th:nth-child(4),.settings-table td:nth-child(4){width:28%}
/* column layout end */






.arm-cell{width:auto;padding-left:82px;padding-right:52px;text-align:center}
.settings-table .arm-cell{padding-left:6px;padding-right:6px}
.arm-form{margin:0}
.arm-check{width:24px;height:24px;cursor:pointer;vertical-align:-5px}
.always-arm{font-size:14px;font-weight:bold;color:#78ff9c;letter-spacing:1px}
.name{font-weight:bold;text-align:center;font-size:23px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ip{color:#b8b8c0;font-size:16px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tc{font-family:monospace;font-size:29px;letter-spacing:1px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tc-warning{color:#ff4b4b!important;font-weight:bold;text-shadow:0 0 10px rgba(255,75,75,.45)}
.format{font-family:monospace;font-size:21px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.format-warning{color:#ff4b4b!important;font-weight:bold;text-shadow:0 0 10px rgba(255,75,75,.45)}
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

/* compact settings start */
.settings-form{
  max-width:720px !important;
  margin:0 auto !important;
  font-size:13px !important;
}

.settings-form table{
  margin-top:4px !important;
  font-size:13px !important;
}

.settings-form th,
.settings-form td{
  padding:3px 6px !important;
  line-height:1.15 !important;
}

.settings-form input[type="text"],
.settings-form select,
.settings-form .text-input{
  font-size:13px !important;
  padding:3px 6px !important;
  min-height:24px !important;
  height:26px !important;
  box-sizing:border-box !important;
}

.settings-switch-row{
  margin:4px 0 !important;
  gap:12px !important;
  font-size:13px !important;
}

.settings-note{
  margin-top:4px !important;
  font-size:13px !important;
  line-height:1.15 !important;
}

.save-row{
  margin-top:6px !important;
  gap:12px !important;
}

.save-btn{
  width:170px !important;
  height:34px !important;
  min-height:34px !important;
  padding:4px 10px !important;
  font-size:16px !important;
  border-radius:9px !important;
}

#settingsView h1,
#settings-view h1,
#settings h1,
.settings-view h1,
.settings-page h1,
.settings-panel h1{
  font-size:20px !important;
  margin:4px 0 6px 0 !important;
}

#settingsView h2,
#settings-view h2,
#settings h2,
.settings-view h2,
.settings-page h2,
.settings-panel h2{
  font-size:16px !important;
  margin:4px 0 4px 0 !important;
}

#settingsView .card,
#settings-view .card,
#settings .card,
.settings-view .card,
.settings-page .card,
.settings-panel .card{
  padding:7px 10px !important;
  margin:5px 0 !important;
}

#settingsView p,
#settings-view p,
#settings p,
.settings-view p,
.settings-page p,
.settings-panel p{
  margin:2px 0 !important;
  line-height:1.15 !important;
}
/* compact settings end */


/* initial settings no flash start */
body.initial-settings #hyperdeckView,
body.initial-settings #hyperdeck-view,
body.initial-settings #hyperdeckPanel,
body.initial-settings #hyperdeck-panel,
body.initial-settings #mainView,
body.initial-settings #main-view,
body.initial-settings .hyperdeck-view,
body.initial-settings .hyperdeck-panel,
body.initial-settings .main-view {
  display:none !important;
}

body.initial-settings #settingsView,
body.initial-settings #settings-view,
body.initial-settings #settings,
body.initial-settings .settings-view,
body.initial-settings .settings-page,
body.initial-settings .settings-panel {
  display:block !important;
}
/* initial settings no flash end */

</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>HYPERDECK CONTROL</h1>
    <div class="active-count">__ACTIVE_COUNT__</div>
    <div class="topnav">
      <button id="btnDecks" class="navbtn active" onclick="window.location.href='/'">HYPERDECK</button>
      <button id="btnLogger" class="navbtn" onclick="window.location.href='/atem'">ATEM LOGGER</button>
      <button id="btnSettings" class="navbtn" onclick="window.location.href='/settings'">SETTINGS</button>
      <button class="close-panel-btn" type="button" title="Zavřít panel" onclick="closePanel()">×</button>
    </div>
  </div>

  __NOTICE__
  __ALARM__

  <div id="viewDecks" class="view active">
    <div class="panel-card">
      <table class="decks-table">
        <thead>
          <tr>
            <th>IP</th>
            <th>Deck</th>
            <th>Rec</th>
            <th>Stop</th>
            <th>Status</th>
            <th>Format</th>
            <th>TC</th>
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

      <div class="small-label" style="margin-top:12px;">FORMÁT</div>
      <input class="text-input ip-input" type="text" name="expected_format" value="__EXPECTED_FORMAT__">

      <div class="small-label" style="margin-top:12px;">REFERENCE TC</div>
      <select class="text-input ip-input" name="tc_reference_mode">
        <option value="group" __TC_REF_GROUP_SELECTED__>Skupina</option>
        <option value="control_room" __TC_REF_CONTROL_ROOM_SELECTED__>Režie</option>
      </select>

      <div class="settings-switch-row">
        <label class="check-label">
          <input type="checkbox" name="tc_compare" value="1" __TC_COMPARE_CHECKED__>
          TC COMPARE
        </label>
      </div>

      <div class="small-label">TC TOLERANCE FRAMES</div>
      <input class="text-input ip-input" type="number" min="0" max="50" name="tc_tolerance_frames" value="__TC_TOLERANCE_FRAMES__">

      <div class="panel-card">
        <table class="settings-table">
          <thead>
            <tr>
              <th>ARM</th>
              <th>Deck name</th>
              <th>IP address</th>
              <th>TC IN</th>
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

async function closePanel() {
  try {
    await fetch("/api/close-panel", {
      method: "POST",
      cache: "no-store"
    });
  } catch (err) {
    console.log("Close panel error:", err);
  }
}

function addActive(id) {
  const el = document.getElementById(id);
  if (el) {
    el.classList.add("active");
  }
}

function removeActive(id) {
  const el = document.getElementById(id);
  if (el) {
    el.classList.remove("active");
  }
}

function showView(name) {
  // remove initial settings class start
  if (document.body) {
    document.body.classList.remove("initial-settings");
  }
  // remove initial settings class end
  // pause status polling in settings start
  if (name === "settings") {
    if (typeof stopStatusPolling === "function") {
      stopStatusPolling();
    }
  } else {
    if (typeof startStatusPolling === "function") {
      startStatusPolling();
    }
  }
  // pause status polling in settings end

  removeActive("viewDecks");
  removeActive("viewLogger");
  removeActive("viewSettings");

  removeActive("btnDecks");
  removeActive("btnLogger");
  removeActive("btnSettings");

  if (name === "decks") {
    addActive("viewDecks");
    addActive("btnDecks");
  }

  if (name === "logger") {
    addActive("viewLogger");
    addActive("btnLogger");
  }

  if (name === "settings") {
    addActive("viewSettings");
    addActive("btnSettings");
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
  const startMs = Date.now();

  document.querySelectorAll(".live-tc").forEach((el) => {
    el.dataset.startMs = String(startMs);
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
  const statusUpdateMs = Date.now();

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
      statusCell.textContent = "";

      const led = document.createElement("span");
      led.className = `led ${deck.led}`;
      statusCell.appendChild(led);
      statusCell.appendChild(document.createTextNode(` ${deck.status}`));
    }

    const formatCell = row.querySelector('[data-role="format"]');

    if (formatCell) {
      formatCell.textContent = deck.format || "---";
      formatCell.classList.toggle("format-warning", !deck.format_ok);
    }

    const tcCell = row.querySelector('[data-role="tc"]');

    if (tcCell) {
      if (deck.tc_valid) {
        tcCell.classList.add("live-tc");
        tcCell.classList.toggle("tc-warning", !!deck.tc_warning);
        tcCell.dataset.baseFrame = String(deck.total_frames);
        tcCell.dataset.startFrame = String(deck.total_frames);
        tcCell.dataset.startMs = String(statusUpdateMs);
        tcCell.textContent = deck.tc;
      } else {
        tcCell.classList.remove("live-tc");
        tcCell.classList.remove("tc-warning");
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

// controlled status polling start
let pollStatusTimer = null;

function statusPollingAllowedByUrl() {
  try {
    return window.location.pathname === "/";
  } catch (e) {
    return false;
  }
}

function startStatusPolling() {
  if (pollStatusTimer !== null) {
    return;
  }

  pollStatus();
  pollStatusTimer = setInterval(pollStatus, 500);
}

function stopStatusPolling() {
  if (pollStatusTimer !== null) {
    clearInterval(pollStatusTimer);
    pollStatusTimer = null;
  }
}

if (statusPollingAllowedByUrl()) {
  startStatusPolling();
}
// controlled status polling end

setTimeout(() => {
  const notice = document.querySelector(".notice");

  if (notice) {
    notice.style.display = "none";
    window.history.replaceState({}, document.title, "/");
  }
}, 2500);

// view from url start
(function() {
  const path = window.location.pathname;
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");

  if (path === "/settings" || view === "settings") {
    showView("settings");
    return;
  }

  if (path === "/atem" || view === "logger") {
    showView("logger");
    return;
  }

  showView("decks");
})();
// view from url end


// saved toast start
(function() {
  const notice = document.querySelector(".notice");

  if (!notice) {
    return;
  }

  setTimeout(function() {
    notice.classList.add("toast-hide");
  }, 1700);

  setTimeout(function() {
    if (notice.parentNode) {
      notice.parentNode.removeChild(notice);
    }
  }, 2200);

  try {
    const url = new URL(window.location.href);

    if (url.searchParams.get("saved") === "1") {
      url.searchParams.delete("saved");
      const newUrl = url.pathname + (url.search ? url.search : "") + url.hash;
      window.history.replaceState({}, "", newUrl);
    }
  } catch (e) {
    console.log(e);
  }
})();
// saved toast end

</script>
</body>
</html>
"""


def build_status_api():
    cfg = load_config()
    expected_format = cfg.get("expected_format", "1080p50")
    decks_out = []
    status_map = get_status_map(cfg)
    tc_compare = build_tc_compare(cfg, status_map)

    for i, deck in enumerate(cfg["decks"]):
        if not deck_is_armed_at_index(cfg, i, deck):
            continue

        status_text, led_class, format_text, tc, tc_valid, total_frames = status_map.get(
            i,
            get_skipped_state()
        )
        tc_info = tc_compare["by_index"].get(i, {})

        decks_out.append({
            "index": i,
            "name": deck.get("name", f"REC {i + 1}"),
            "ip": deck.get("ip", ""),
            "status": status_text,
            "led": led_class,
            "format": format_text,
            "format_ok": format_is_ok(format_text, expected_format),
            "tc": tc,
            "tc_valid": bool(tc_valid),
            "tc_warning": bool(tc_info.get("warning", False)),
            "tc_diff_frames": int(tc_info.get("diff", 0)),
            "total_frames": int(total_frames),
            "armed": deck_is_armed_at_index(cfg, i, deck)
        })

    return {
        "alarm_html": make_alarm(cfg, status_map=status_map),
        "decks": decks_out
    }


def close_panel_browser():
    closed = 0
    current_pid = os.getpid()

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue

        pid = int(proc_dir.name)

        if pid == current_pid:
            continue

        try:
            comm = (proc_dir / "comm").read_text(encoding="utf-8").strip().lower()
            cmdline_raw = (proc_dir / "cmdline").read_bytes()
        except (OSError, UnicodeDecodeError):
            continue

        cmdline = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")

        if "chromium" not in comm:
            continue

        if PANEL_BROWSER_URL not in cmdline:
            continue

        try:
            os.kill(pid, signal.SIGTERM)
            closed += 1
        except OSError:
            pass

    return closed


def add_initial_settings_class(match):
    tag = match.group(0)

    if 'class="' in tag:
        return re.sub(r'class="([^"]*)"', r'class="\1 initial-settings"', tag, count=1)

    if "class='" in tag:
        return re.sub(r"class='([^']*)'", r"class='\1 initial-settings'", tag, count=1)

    return tag[:-1] + ' class="initial-settings">'


def apply_real_page_split(page, initial_view):
    deck_match = re.search(r'\n\s*<div id="viewDecks"[^>]*>', page)
    logger_match = re.search(r'\n\s*<div id="viewLogger"[^>]*>', page)
    settings_match = re.search(r'\n\s*<div id="viewSettings"[^>]*>', page)
    footer_match = re.search(r'\n\s*<div class="footer"', page)

    if not (deck_match and logger_match and settings_match and footer_match):
        return page

    deck_pos = deck_match.start()
    logger_pos = logger_match.start()
    settings_pos = settings_match.start()
    footer_pos = footer_match.start()

    if not (deck_pos < logger_pos < settings_pos < footer_pos):
        return page

    before_views = page[:deck_pos]
    deck_block = page[deck_pos:logger_pos]
    logger_block = page[logger_pos:settings_pos]
    settings_block = page[settings_pos:footer_pos]
    after_views = page[footer_pos:]

    def clear_active_buttons(text):
        text = re.sub(r'id="btnDecks" class="navbtn(?: active)?"', 'id="btnDecks" class="navbtn"', text)
        text = re.sub(r'id="btnLogger" class="navbtn(?: active)?"', 'id="btnLogger" class="navbtn"', text)
        text = re.sub(r'id="btnSettings" class="navbtn(?: active)?"', 'id="btnSettings" class="navbtn"', text)
        return text

    def set_button_active(text, button_id):
        text = clear_active_buttons(text)
        return text.replace(
            f'id="{button_id}" class="navbtn"',
            f'id="{button_id}" class="navbtn active"',
            1
        )

    def set_view_active(block):
        return re.sub(
            r'<div id="(viewDecks|viewLogger|viewSettings)" class="view(?: active)?"',
            r'<div id="\1" class="view active"',
            block,
            count=1
        )

    if initial_view == "settings":
        before_views = set_button_active(before_views, "btnSettings")
        return before_views + set_view_active(settings_block) + after_views

    if initial_view == "logger":
        before_views = set_button_active(before_views, "btnLogger")
        return before_views + set_view_active(logger_block) + after_views

    before_views = set_button_active(before_views, "btnDecks")
    return before_views + set_view_active(deck_block) + after_views



def build_page(saved=False, initial_view="hyperdeck"):
    cfg = load_config()

    if initial_view == "hyperdeck":
        resume_status_pollers()
    else:
        stop_all_status_pollers(pause_seconds=0.4)

    panel_ip = html.escape(cfg.get("panel_ip", "10.0.0.8"))
    atem_logger_ip = html.escape(cfg.get("atem_logger_ip", "10.0.0.9"))
    expected_format = html.escape(
        normalize_format(cfg.get("expected_format", "1080p50")),
        quote=True
    )

    notice = ""
    if saved:
        notice = '<div class="notice">Nastavení uloženo.</div>'

    tc_compare_checked = "checked" if cfg.get("tc_compare", True) else ""
    tc_tolerance_frames = html.escape(str(cfg.get("tc_tolerance_frames", 2)))
    tc_reference_mode = normalize_tc_reference_mode(cfg.get("tc_reference_mode", "group"))
    tc_ref_group_selected = "selected" if tc_reference_mode == "group" else ""
    tc_ref_control_room_selected = "selected" if tc_reference_mode == "control_room" else ""
    status_poll_enabled = "true"

    page = HTML_TEMPLATE

    atem_logger_ip_raw = cfg.get("atem_logger_ip", "10.0.0.9").strip() or "10.0.0.9"
    atem_logger_ip = html.escape(atem_logger_ip_raw, quote=True)
    page = page.replace("__ATEM_LOGGER_URL__", f"http://{atem_logger_ip}/")

    # initial settings body class start
    if initial_view == "settings":
        page = re.sub(
            r"<body([^>]*)>",
            add_initial_settings_class,
            page,
            count=1
        )
    # initial settings body class end
    page = page.replace("__NOTICE__", notice)
    page = page.replace("__ALARM__", make_alarm(cfg))
    page = page.replace("__ACTIVE_COUNT__", html.escape(get_active_count_text(cfg)))
    if initial_view == "hyperdeck":
        page = page.replace("__ROWS__", make_rows(cfg))
    else:
        page = page.replace("__ROWS__", "")
    page = page.replace("__SETTINGS_ROWS__", make_settings_rows(cfg, load_tc_inputs=(initial_view == "settings")))
    page = page.replace("__PANEL_IP__", panel_ip)
    page = page.replace("__ATEM_LOGGER_IP__", atem_logger_ip)
    page = page.replace("__EXPECTED_FORMAT__", expected_format)
    page = page.replace("__TC_REF_GROUP_SELECTED__", tc_ref_group_selected)
    page = page.replace("__TC_REF_CONTROL_ROOM_SELECTED__", tc_ref_control_room_selected)
    page = page.replace("__TC_COMPARE_CHECKED__", tc_compare_checked)
    page = page.replace("__TC_TOLERANCE_FRAMES__", tc_tolerance_frames)
    page = page.replace("__STATUS_POLL_ENABLED__", status_poll_enabled)

    page = apply_real_page_split(page, initial_view)
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

        initial_view = "settings" if (parsed.path == "/settings" or "view=settings" in parsed.query) else ("logger" if (parsed.path == "/atem" or "view=logger" in parsed.query) else "hyperdeck")


        page = build_page(saved=saved, initial_view=initial_view).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)

        if self.path == "/api/close-panel":
            payload = json.dumps({
                "closed": close_panel_browser()
            }, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/toggle-arm":
            cfg = load_config()

            try:
                deck_index = int(form.get("deck", ["-1"])[0])

                if 0 <= deck_index < len(cfg["decks"]):
                    deck = cfg["decks"][deck_index]

                    if not is_control_room_deck(cfg, deck_index):
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
            cmd = form.get("cmd", [""])[0]

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

            panel_ip = form.get(
                "panel_ip",
                [cfg.get("panel_ip", "10.0.0.8")]
            )[0].strip()

            cfg["panel_ip"] = normalize_ipv4(
                panel_ip,
                cfg.get("panel_ip", "10.0.0.8")
            )

            atem_logger_ip = form.get(
                "atem_logger_ip",
                [cfg.get("atem_logger_ip", "10.0.0.9")]
            )[0].strip()

            cfg["atem_logger_ip"] = normalize_ipv4(
                atem_logger_ip,
                cfg.get("atem_logger_ip", "10.0.0.9")
            )

            expected_format = form.get(
                "expected_format",
                [cfg.get("expected_format", "1080p50")]
            )[0].strip()

            cfg["expected_format"] = normalize_format(expected_format)

            tc_reference_mode = form.get(
                "tc_reference_mode",
                [cfg.get("tc_reference_mode", "group")]
            )[0].strip()

            cfg["tc_reference_mode"] = normalize_tc_reference_mode(tc_reference_mode)

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

                ip = normalize_ipv4(
                    ip,
                    old_deck.get("ip", ""),
                    allow_empty=True
                )

                if not name:
                    name = old_deck.get("name", f"REC {i + 1}")

                armed = f"deck_armed_{i}" in form

                if is_control_room_deck(cfg, i):
                    armed = True

                new_decks.append({
                    "name": name,
                    "ip": ip,
                    "armed": armed
                })

            cfg["decks"] = new_decks

            stop_all_status_pollers(pause_seconds=0.4)
            for i, deck in enumerate(cfg.get("decks", [])):
                tc_input = form.get(f"deck_tc_input_{i}", [""])[0].strip().lower()
                tc_original = form.get(f"deck_tc_original_{i}", ["---"])[0].strip().lower()
                if (
                    deck_is_armed_at_index(cfg, i, deck)
                    and tc_input in TC_INPUT_VALUES
                    and tc_input != tc_original
                ):
                    set_hyperdeck_timecode_input(deck, tc_input)

            save_config(cfg)

            self.send_response(303)
            self.send_header("Location", "/settings?saved=1")
            self.end_headers()
            return

        self.send_error(404)

    def log_message(self, format, *args):
        pass


server = HTTPServer((HOST, PORT), Handler)
print(f"HyperDeck Panel bezi na http://10.0.0.8:{PORT}")
server.serve_forever()
