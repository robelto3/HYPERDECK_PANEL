from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
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
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

HOST = "0.0.0.0"
PORT = 8080
CONFIG_FILE = Path("config.json")
ADVANCED_NVM_FILE = Path("advanced_nvm.json")
PANEL_BROWSER_URL = f"http://127.0.0.1:{PORT}/"
CSRF_TOKEN = secrets.token_urlsafe(32)

DEFAULT_CONFIG = {
    "panel_ip": "10.0.0.8",
    "atem_logger_ip": "10.0.0.9",
    "expected_format": "1080p50",
    "tc_reference_mode": "group",
    "tc_compare": True,
    "format_compare": True,
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
        {"name": "Cam 11", "ip": ""},
        {"name": "Cam 12", "ip": ""},
        {"name": "Režie", "ip": "10.0.0.24"}
    ]
}

DEFAULT_ADVANCED_NVM = {
    "format_ip_hd_mini": "",
    "format_ip_4k_pro": "",
    "deck_video_formats": []
}

VIDEO_FORMAT_VALUES = ("1080p25", "1080p50", "2160p50")


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


def transport_response_to_active_slot(response):
    info = parse_hyperdeck_info(response)
    slot_id = info.get("slot id", "").strip().lower()

    if slot_id in ("1", "2"):
        return int(slot_id)

    return None


def format_video_rate(format_text):
    match = re.search(r"(\d{3,4})([pi])(\d+(?:\.\d+)?)", (format_text or "").lower())

    if not match:
        return None

    height = int(match.group(1))
    scan = match.group(2)
    fps = float(match.group(3))

    if scan == "i":
        fps = fps / 2

    if height >= 2160:
        width = 3840
    elif height >= 1080:
        width = 1920
    elif height >= 720:
        width = 1280
    else:
        width = int(height * 16 / 9)

    return width * height * fps


def scale_recording_seconds(recording_seconds, source_format, target_format):
    source_rate = format_video_rate(source_format)
    target_rate = format_video_rate(target_format)

    if not source_rate or not target_rate:
        return recording_seconds

    return int(recording_seconds * source_rate / target_rate)


def format_recording_time(seconds):
    seconds = max(0, int(seconds))

    if seconds < 3600:
        minutes = seconds // 60
        seconds %= 60
        return f"{minutes:02d}:{seconds:02d}"

    minutes = seconds // 60
    hours = minutes // 60
    minutes %= 60

    if hours >= 24:
        days = hours // 24
        hours %= 24
        return f"{days}d {hours:02d}h"

    return f"{hours}h{minutes:02d}"


def parse_positive_int(value):
    try:
        number = int((value or "").strip())
    except ValueError:
        return 0

    return max(0, number)


def slot_has_space_but_no_recording_time(info):
    remaining_size = parse_positive_int(info.get("remaining size", ""))
    total_size = parse_positive_int(info.get("total size", ""))

    if remaining_size < 1_000_000_000:
        return False

    if total_size <= 0:
        return True

    return remaining_size / total_size > 0.05


def slot_is_locked(info):
    values = [
        info.get("blocked", ""),
        info.get("status", ""),
        info.get("volume name", ""),
        info.get("slot name", ""),
        info.get("device", ""),
    ]
    text = " ".join(values).lower()

    return any(
        marker in text
        for marker in ("true", "locked", "lock", "write protected", "write-protected", "read only", "readonly", "blocked")
    )


def slot_response_to_remaining_time(response, source_format, target_format):
    if not response.startswith("202 slot info"):
        return "---", "sd-empty"

    info = parse_hyperdeck_info(response)
    status = info.get("status", "").lower()

    if slot_is_locked(info):
        return "Lock", "sd-warning"

    if status and status != "mounted":
        return "---", "sd-empty"

    recording_time = info.get("recording time", "").strip()
    slot_format = info.get("video format", "").strip().lower()

    try:
        recording_seconds = int(recording_time)
    except ValueError:
        if status == "mounted":
            return "Full", "sd-full"

        return "---", "sd-empty"

    if recording_seconds <= 0:
        if status == "mounted" and slot_has_space_but_no_recording_time(info):
            return "Lock", "sd-warning"

        return "Full", "sd-full"

    if slot_format and slot_format != "none":
        source_format = slot_format

    scaled_seconds = scale_recording_seconds(
        recording_seconds,
        source_format,
        target_format
    )

    return format_recording_time(scaled_seconds), ""


def normalize_sd_state(sd_state):
    if isinstance(sd_state, (list, tuple)) and len(sd_state) >= 2:
        text = str(sd_state[0] or "---")
        css_class = str(sd_state[1] or "")
    else:
        text = str(sd_state or "---")
        css_class = ""

    if text == "---" and not css_class:
        css_class = "sd-empty"

    return text, css_class


STATUS_LOCK = threading.Lock()
CONFIG_LOCK = threading.RLock()
FORMAT_LOCKS = {
    "format_ip_hd_mini": threading.Lock(),
    "format_ip_4k_pro": threading.Lock(),
}
FORMAT_PROGRESS_LOCK = threading.Lock()
FORMAT_PROGRESS = {}
STATUS_POLLERS = {}
STATUS_POLLERS_PAUSED = False
STATUS_CACHE = {}
STATUS_POLL_INTERVAL = 0.5
STATUS_CONNECT_TIMEOUT = 1.0
STATUS_HELLO_TIMEOUT = 0.7
STATUS_TRANSPORT_TIMEOUT = 0.7
CONFIG_CONNECT_TIMEOUT = 1.0
CONFIG_HELLO_TIMEOUT = 0.7
CONFIG_RESPONSE_TIMEOUT = 0.9
CONFIG_RETRY_DELAYS = (0.2, 0.4)
CONFIGURATION_INFO_LOCK = threading.Lock()
CONFIGURATION_INFO_CACHE = {}
FORMAT_SLOT_STATUS_LOCK = threading.Lock()
FORMAT_SLOT_STATUS_CACHE = {}


def get_skipped_state():
    return ("SKIPPED", "skipped", "---", "--:--:--:--", False, 0, ("---", "sd-empty"), ("---", "sd-empty"), None)


def get_offline_state():
    return ("OFFLINE", "offline", "---", "--:--:--:--", False, 0, ("---", "sd-empty"), ("---", "sd-empty"), None)


def make_status_cache_entry(state):
    return {
        "state": state,
        "updated_monotonic": time.monotonic()
    }


def normalize_state_to_snapshot(state, updated_monotonic, snapshot_monotonic):
    status_text, led_class, format_text, tc, tc_valid, total_frames = state[:6]
    extra = state[6:]

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
    ) + extra


def read_status_cache_entry(entry, snapshot_monotonic):
    if isinstance(entry, dict):
        state = entry.get("state", get_offline_state())
        updated_monotonic = entry.get("updated_monotonic", snapshot_monotonic)
        return normalize_state_to_snapshot(state, updated_monotonic, snapshot_monotonic)

    return entry


def status_poller_loop(index, ip, expected_format, stop_event):
    sock = None
    fail_count = 0
    default_standard = None

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

    def read_default_standard(sock):
        nonlocal default_standard

        if default_standard:
            return default_standard

        try:
            sock.sendall(b"configuration\r\n")
            response = recv_hyperdeck_message(sock, timeout=STATUS_TRANSPORT_TIMEOUT)
            info = parse_hyperdeck_info(response)
            default_standard = info.get("default standard", "") or expected_format
        except OSError:
            default_standard = expected_format

        return default_standard

    def read_slot_remaining_time(sock, slot_id):
        try:
            sock.sendall(f"slot info: slot id: {slot_id}\r\n".encode("ascii"))
            response = recv_hyperdeck_message(sock, timeout=STATUS_TRANSPORT_TIMEOUT)
        except OSError:
            return "---", "sd-empty"

        return slot_response_to_remaining_time(
            response,
            read_default_standard(sock),
            expected_format
        )

    def read_transport_once():
        nonlocal sock

        if sock is None:
            sock = connect_socket()

        sock.sendall(b"transport info\r\n")
        response = recv_hyperdeck_message(sock, timeout=STATUS_TRANSPORT_TIMEOUT)
        state = transport_response_to_state(response)
        active_slot = transport_response_to_active_slot(response)
        sd1 = read_slot_remaining_time(sock, 1)
        sd2 = read_slot_remaining_time(sock, 2)
        return state + (sd1, sd2, active_slot)

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
    deck_video_formats = get_deck_video_formats(cfg)
    desired = {
        i: {
            "ip": deck.get("ip", "").strip(),
            "expected_format": deck_video_formats[i],
        }
        for i, deck in enumerate(decks)
        if deck_is_armed_at_index(cfg, i, deck) and deck.get("ip", "").strip()
    }

    with STATUS_LOCK:
        existing = list(STATUS_POLLERS.items())

    # Zastavit pollery, které už nemají běžet nebo mají jinou IP.
    for index, item in existing:
        old_ip = item["ip"]
        old_expected_format = item.get("expected_format", "")
        stop_event = item["stop"]

        if (
            index not in desired
            or desired[index]["ip"] != old_ip
            or desired[index]["expected_format"] != old_expected_format
        ):
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
    for index, poller_config in desired.items():
        ip = poller_config["ip"]
        poller_expected_format = poller_config["expected_format"]

        with STATUS_LOCK:
            already_running = index in STATUS_POLLERS

        if already_running:
            continue

        stop_event = threading.Event()
        thread = threading.Thread(
            target=status_poller_loop,
            args=(index, ip, poller_expected_format, stop_event),
            daemon=True
        )

        with STATUS_LOCK:
            STATUS_CACHE[index] = make_status_cache_entry(get_offline_state())
            STATUS_POLLERS[index] = {
                "ip": ip,
                "expected_format": poller_expected_format,
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
        status_text, led_class, format_text, tc, tc_valid, total_frames = status[:6]

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


def normalize_video_format_choice(value, fallback="1080p50"):
    value = normalize_format(value, fallback)

    if value in VIDEO_FORMAT_VALUES:
        return value

    return fallback


def get_deck_video_formats(cfg, nvm=None):
    if nvm is None:
        nvm = load_advanced_nvm()

    fallback = "1080p50"
    stored = nvm.get("deck_video_formats", [])

    if not isinstance(stored, list):
        stored = []

    stored = [
        normalize_video_format_choice(value, fallback)
        for value in stored
    ]

    while len(stored) < len(cfg.get("decks", [])):
        insert_index = max(0, len(stored) - 1)
        stored.insert(insert_index, fallback)

    formats = []

    for index, _deck in enumerate(cfg.get("decks", [])):
        value = stored[index] if index < len(stored) else fallback
        formats.append(normalize_video_format_choice(value, fallback))

    return formats


def format_is_ok(format_text, expected_format="1080p50", compare_enabled=True):
    if not compare_enabled:
        return True

    expected = normalize_format(expected_format)
    return (format_text or "").strip().lower() == expected




def save_config(cfg):
    with CONFIG_LOCK:
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


def save_json_atomic(path, data):
    with CONFIG_LOCK:
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_file = path.with_name(path.name + ".tmp")

        with tmp_file.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_file, path)

        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        except OSError:
            return

        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def load_advanced_nvm():
    data = copy.deepcopy(DEFAULT_ADVANCED_NVM)

    if ADVANCED_NVM_FILE.exists():
        try:
            loaded = json.loads(ADVANCED_NVM_FILE.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}

        if isinstance(loaded, dict):
            data["format_ip_hd_mini"] = str(loaded.get("format_ip_hd_mini", "")).strip()
            data["format_ip_4k_pro"] = str(loaded.get("format_ip_4k_pro", "")).strip()

            deck_video_formats = loaded.get("deck_video_formats", [])

            if isinstance(deck_video_formats, list):
                data["deck_video_formats"] = [
                    normalize_video_format_choice(value)
                    for value in deck_video_formats
                ]

    return data


def save_advanced_nvm(data):
    nvm = copy.deepcopy(DEFAULT_ADVANCED_NVM)
    nvm["format_ip_hd_mini"] = str(data.get("format_ip_hd_mini", "")).strip()
    nvm["format_ip_4k_pro"] = str(data.get("format_ip_4k_pro", "")).strip()
    deck_video_formats = data.get("deck_video_formats", [])

    if isinstance(deck_video_formats, list):
        nvm["deck_video_formats"] = [
            normalize_video_format_choice(value)
            for value in deck_video_formats
        ]

    save_json_atomic(ADVANCED_NVM_FILE, nvm)


def get_system_network_interfaces():
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0
        )
    except (OSError, subprocess.SubprocessError):
        return []

    interfaces = []

    for line in result.stdout.splitlines():
        parts = line.split()

        if len(parts) < 4:
            continue

        name = parts[1]
        address = parts[3].split("/", 1)[0]

        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue

        if ip.version == 4:
            interfaces.append((name, str(ip)))

    return interfaces


def make_network_interfaces_html():
    interfaces = get_system_network_interfaces()
    interface_roles = {
        "eth0": "Recorders",
        "eth1": "Atem Logger",
    }

    if not interfaces:
        return '<div class="network-interface">ETH: ---</div>'

    items = []

    for name, ip in interfaces:
        safe_name = html.escape(name)
        safe_ip = html.escape(ip)
        role = interface_roles.get(name)
        if role:
            label = f"{safe_name} ({safe_ip}) -&gt; {html.escape(role)}"
        else:
            label = f"{safe_name} ({safe_ip})"
        items.append(f'<div class="network-interface"><span>{label}</span></div>')

    return "".join(items)


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
    cfg.setdefault("format_compare", True)
    cfg.setdefault("tc_tolerance_frames", 2)
    cfg.setdefault("decks", copy.deepcopy(DEFAULT_CONFIG["decks"]))
    normalize_deck_list(cfg)

    for index, deck in enumerate(cfg["decks"]):
        deck.setdefault("armed", True)

        if is_control_room_deck(cfg, index):
            deck["armed"] = True

    return cfg


def normalize_deck_list(cfg):
    decks = cfg.get("decks", [])
    default_decks = copy.deepcopy(DEFAULT_CONFIG["decks"])
    target_count = len(default_decks)

    if not isinstance(decks, list) or not decks:
        cfg["decks"] = default_decks
        return

    while len(decks) < target_count:
        insert_index = max(0, len(decks) - 1)
        default_deck = copy.deepcopy(default_decks[insert_index])
        default_deck["armed"] = False
        decks.insert(insert_index, default_deck)

    cfg["decks"] = decks


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


def get_active_recorders_label(cfg):
    return f"ACTIVE REC: {get_active_count_text(cfg)}"


def get_active_recorders_html(cfg):
    count = html.escape(get_active_count_text(cfg))
    return f'ACTIVE REC: <span class="active-rec-value">{count}</span>'


def make_format_device_deck_badge(cfg, device_ip):
    device_ip = normalize_ipv4(device_ip, "", allow_empty=True)

    if device_ip:
        for index, deck in enumerate(cfg.get("decks", [])):
            deck_ip = normalize_ipv4(deck.get("ip", ""), "", allow_empty=True)

            if deck_ip == device_ip:
                name = deck.get("name", "").strip() or f"REC {index + 1}"
                return (
                    '<div class="format-device-deck-badge">'
                    f'DECK · {html.escape(name)}'
                    '</div>'
                )

    return '<div class="format-device-deck-badge deck-unknown">DECK NENALEZEN</div>'


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

    expected_statuses = {
        "rec": {"record", "recording"},
        "stop": {"stopped", "stop", "preview", "idle"},
    }[cmd]
    attempts = len(CONFIG_RETRY_DELAYS) + 1
    last_message = "deck neodpověděl"

    for attempt in range(attempts):
        command_sent = False

        try:
            with socket.create_connection(
                (ip, 9993),
                timeout=CONFIG_CONNECT_TIMEOUT,
            ) as sock:
                recv_hyperdeck_message(sock, timeout=CONFIG_HELLO_TIMEOUT)

                sock.sendall(b"remote: override: true\r\n")
                remote_response = receive_hyperdeck_message_type(
                    sock,
                    "200 ok",
                    CONFIG_RESPONSE_TIMEOUT,
                )

                if not remote_response:
                    last_message = "remote override nebyl potvrzen"
                else:
                    sock.sendall((command + "\r\n").encode("ascii"))
                    command_sent = True
                    command_response = receive_hyperdeck_message_type(
                        sock,
                        "200 ok",
                        CONFIG_RESPONSE_TIMEOUT,
                    )
                    last_message = (
                        command_response.strip()
                        if command_response
                        else f"příkaz {command.upper()} nebyl potvrzen"
                    )

        except OSError as e:
            last_message = str(e)

        if command_sent:
            # Potvrzení 200 ok nemusí při opožděných zprávách patřit přímo
            # příkazu REC/STOP. Rozhodující je proto až čerstvě načtený
            # transportní stav zařízení.
            for verify_delay in (0.15, 0.35):
                time.sleep(verify_delay)
                actual_status = get_hyperdeck_transport_status(deck)

                if actual_status in expected_statuses:
                    return True, f"{command.upper()} potvrzeno: {actual_status}"

                if actual_status:
                    last_message = f"deck zůstal ve stavu {actual_status}"
                else:
                    last_message = f"stav po {command.upper()} se nepodařilo ověřit"

        if attempt < len(CONFIG_RETRY_DELAYS):
            time.sleep(CONFIG_RETRY_DELAYS[attempt])

    return False, last_message


def format_hyperdeck_slot(ip, slot_id=1, filesystem="exFAT"):
    ip = normalize_ipv4(ip, "", allow_empty=True)

    if not ip:
        return False, "Chybí IP adresa."

    def wait_for_slot_mounted(sock, wait_seconds=45):
        deadline = time.monotonic() + wait_seconds
        last_response = ""

        while time.monotonic() < deadline:
            sock.sendall(f"slot info: slot id: {slot_id}\r\n".encode("ascii"))
            last_response = recv_hyperdeck_message(sock, timeout=1.0)
            info = parse_hyperdeck_info(last_response)

            if info.get("status", "").strip().lower() == "mounted":
                return True, last_response.strip()

            time.sleep(0.5)

        return False, last_response.strip() or "Čekání na dokončení formátování vypršelo."

    try:
        with socket.create_connection((ip, 9993), timeout=1.0) as sock:
            recv_hyperdeck_message(sock, timeout=0.4)

            sock.sendall(b"remote: override: true\r\n")
            recv_hyperdeck_message(sock, timeout=0.5)

            prepare_command = f"format: slot id: {slot_id} prepare: {filesystem}"
            sock.sendall((prepare_command + "\r\n").encode("ascii"))
            prepare_response = recv_hyperdeck_message(sock, timeout=2.0)
            token = parse_hyperdeck_info(prepare_response).get("token", "")

            if not token:
                match = re.search(r"216\s+format\s+ready:\s*(\S+)", prepare_response, re.IGNORECASE)
                token = match.group(1) if match else ""

            if not token:
                return False, prepare_response.strip() or "Deck nevrátil potvrzovací token."

            sock.sendall((f"format: confirm: {token}\r\n").encode("ascii"))
            confirm_response = recv_hyperdeck_message(sock, timeout=5.0)

            mounted, slot_response = wait_for_slot_mounted(sock)

            if not mounted:
                detail = confirm_response.strip() or slot_response
                return False, detail

            return True, slot_response

    except OSError as e:
        return False, str(e)


def format_hyperdeck_slots(ip, slot_ids, filesystem="exFAT", progress_key=None):
    results = []

    for slot_id in slot_ids:
        if progress_key:
            with FORMAT_PROGRESS_LOCK:
                FORMAT_PROGRESS[progress_key] = {
                    "active": True,
                    "current_slot": slot_id,
                    "slots": list(slot_ids),
                }

        ok, response = format_hyperdeck_slot(
            ip,
            slot_id=slot_id,
            filesystem=filesystem,
        )
        results.append({
            "slot": slot_id,
            "ok": ok,
            "response": response,
        })

        if not ok:
            break

    return all(item["ok"] for item in results) and len(results) == len(slot_ids), results


def receive_hyperdeck_message_type(sock, response_type, timeout, max_messages=2):
    response_type = response_type.lower()

    for _ in range(max_messages):
        response = recv_hyperdeck_message(sock, timeout=timeout)

        if response_type in response.lower():
            return response

        if not response:
            break

    return ""


def get_hyperdeck_transport_status(deck):
    ip = deck.get("ip", "").strip()

    if not ip:
        return ""

    attempts = len(CONFIG_RETRY_DELAYS) + 1

    for attempt in range(attempts):
        try:
            with socket.create_connection(
                (ip, 9993),
                timeout=CONFIG_CONNECT_TIMEOUT,
            ) as sock:
                recv_hyperdeck_message(sock, timeout=CONFIG_HELLO_TIMEOUT)
                sock.sendall(b"transport info\r\n")
                response = receive_hyperdeck_message_type(
                    sock,
                    "208 transport info",
                    CONFIG_RESPONSE_TIMEOUT,
                )
                info = parse_hyperdeck_info(response)
                status = info.get("status", "").strip().lower()

                if status:
                    return status

        except OSError:
            pass

        if attempt < len(CONFIG_RETRY_DELAYS):
            time.sleep(CONFIG_RETRY_DELAYS[attempt])

    return ""


def get_format_slots_status(ip, expected_format="1080p50", slot_ids=(1, 2)):
    ip = normalize_ipv4(ip, "", allow_empty=True)
    slot_ids = tuple(dict.fromkeys(
        int(slot_id)
        for slot_id in slot_ids
        if str(slot_id) in ("1", "2")
    ))

    if not slot_ids:
        return {}

    if not ip:
        return {
            slot_id: {
                "text": "---",
                "css_class": "sd-empty",
                "valid": True,
                "fresh": True,
            }
            for slot_id in slot_ids
        }

    results = {}
    unresolved = set(slot_ids)
    attempts = len(CONFIG_RETRY_DELAYS) + 1

    for attempt in range(attempts):
        try:
            with socket.create_connection(
                (ip, 9993),
                timeout=CONFIG_CONNECT_TIMEOUT,
            ) as sock:
                recv_hyperdeck_message(sock, timeout=CONFIG_HELLO_TIMEOUT)

                default_standard = expected_format
                sock.sendall(b"configuration\r\n")
                configuration_response = receive_hyperdeck_message_type(
                    sock,
                    "211 configuration",
                    CONFIG_RESPONSE_TIMEOUT,
                )
                configuration_info = parse_hyperdeck_info(configuration_response)
                default_standard = (
                    configuration_info.get("default standard", "")
                    or expected_format
                )

                for slot_id in list(unresolved):
                    sock.sendall(
                        f"slot info: slot id: {slot_id}\r\n".encode("ascii")
                    )
                    slot_response = receive_hyperdeck_message_type(
                        sock,
                        "202 slot info",
                        CONFIG_RESPONSE_TIMEOUT,
                    )

                    if not slot_response:
                        continue

                    text, css_class = slot_response_to_remaining_time(
                        slot_response,
                        default_standard,
                        expected_format,
                    )
                    results[slot_id] = {
                        "text": text,
                        "css_class": css_class,
                        "valid": True,
                        "fresh": True,
                    }
                    unresolved.discard(slot_id)

        except OSError:
            pass

        if not unresolved:
            break

        if attempt < len(CONFIG_RETRY_DELAYS):
            time.sleep(CONFIG_RETRY_DELAYS[attempt])

    with FORMAT_SLOT_STATUS_LOCK:
        for slot_id, result in results.items():
            cache_key = (ip, expected_format, slot_id)
            FORMAT_SLOT_STATUS_CACHE[cache_key] = (
                result["text"],
                result["css_class"],
            )

        for slot_id in unresolved:
            cache_key = (ip, expected_format, slot_id)
            cached = FORMAT_SLOT_STATUS_CACHE.get(cache_key)

            if cached:
                text, css_class = cached
                results[slot_id] = {
                    "text": text,
                    "css_class": css_class,
                    "valid": True,
                    "fresh": False,
                }
            else:
                # Neověřený stav není totéž jako skutečně prázdný slot.
                # Prohlížeč jej proto nesmí použít k deaktivaci volby média.
                results[slot_id] = {
                    "text": "---",
                    "css_class": "sd-empty",
                    "valid": False,
                    "fresh": False,
                }

    return results


def get_format_slot_status(ip, expected_format="1080p50", slot_id=1):
    result = get_format_slots_status(
        ip,
        expected_format=expected_format,
        slot_ids=(slot_id,),
    ).get(slot_id, {})

    return result.get("text", "---"), result.get("css_class", "sd-empty")


def send_hyperdeck_commands_parallel(decks, cmd):
    results = []

    if not decks:
        return results

    max_workers = min(len(decks), 12)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(send_hyperdeck_command, deck, cmd): (index, deck)
            for index, deck in enumerate(decks)
        }

        for future in as_completed(future_map):
            index, deck = future_map[future]

            try:
                ok, response = future.result()
            except Exception as e:
                ok, response = False, str(e)

            results.append({
                "index": index,
                "name": deck.get("name", ""),
                "ip": deck.get("ip", ""),
                "ok": ok,
                "response": response
            })

    return sorted(results, key=lambda item: item["index"])


def is_hyperdeck_configuration_info(info):
    configuration_keys = {
        "timecode input",
        "video input",
        "file format",
        "audio codec",
        "audio input channels",
        "default standard",
    }
    return any(key in info for key in configuration_keys)


def get_hyperdeck_configuration_info(deck, allow_cached_fallback=True):
    ip = deck.get("ip", "").strip()

    if not ip:
        return {}

    attempts = len(CONFIG_RETRY_DELAYS) + 1

    for attempt in range(attempts):
        try:
            with socket.create_connection(
                (ip, 9993),
                timeout=CONFIG_CONNECT_TIMEOUT,
            ) as sock:
                # Některé modely po ukončení stavového polleru pošlou uvítání
                # se zpožděním. I prázdné uvítání proto není samo o sobě chyba.
                recv_hyperdeck_message(sock, timeout=CONFIG_HELLO_TIMEOUT)
                sock.sendall(b"configuration\r\n")

                # Pokud opožděné uvítání dorazí až po příkazu, první přečtená
                # zpráva bude 500 connection info. Pokračujeme proto až k
                # vlastní odpovědi 211 configuration.
                for _ in range(2):
                    response = recv_hyperdeck_message(
                        sock,
                        timeout=CONFIG_RESPONSE_TIMEOUT,
                    )
                    info = parse_hyperdeck_info(response)

                    if is_hyperdeck_configuration_info(info):
                        with CONFIGURATION_INFO_LOCK:
                            CONFIGURATION_INFO_CACHE[ip] = info.copy()
                        return info

                    if not response:
                        break

        except OSError:
            pass

        if attempt < len(CONFIG_RETRY_DELAYS):
            time.sleep(CONFIG_RETRY_DELAYS[attempt])

    # Krátký síťový výpadek už nesmaže údaje, které se dříve načetly správně.
    if allow_cached_fallback:
        with CONFIGURATION_INFO_LOCK:
            return CONFIGURATION_INFO_CACHE.get(ip, {}).copy()

    return {}


def get_hyperdeck_timecode_input(deck):
    info = get_hyperdeck_configuration_info(deck)

    value = info.get("timecode input", "").strip().lower()

    if value in TC_INPUT_VALUES:
        return value

    return "---"


def get_hyperdeck_video_input(deck):
    info = get_hyperdeck_configuration_info(deck)
    value = info.get("video input", "").strip()

    if not value:
        return "---"

    return value.upper()


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


def get_video_input_map(cfg):
    result = {}

    for i, deck in enumerate(cfg.get("decks", [])):
        result[i] = get_hyperdeck_video_input(deck)

    return result


def get_configuration_info_map(cfg):
    result = {}
    decks = cfg.get("decks", [])
    jobs = []

    for i, deck in enumerate(decks):
        if deck_is_armed_at_index(cfg, i, deck) and deck.get("ip", "").strip():
            jobs.append((i, deck))
        else:
            result[i] = {}

    if not jobs:
        return result

    max_workers = min(len(jobs), 12)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(get_hyperdeck_configuration_info, deck): index
            for index, deck in jobs
        }

        for future in as_completed(future_map):
            index = future_map[future]

            try:
                result[index] = future.result()
            except Exception:
                result[index] = {}

    return result


def configuration_timecode_input(info):
    value = info.get("timecode input", "").strip().lower()

    if value in TC_INPUT_VALUES:
        return value

    return "---"


def configuration_video_input(info):
    value = info.get("video input", "").strip()

    if not value:
        return "---"

    return value.upper()


def configuration_video_codec(info):
    value = info.get("file format", "").strip()

    if not value:
        return "---"

    if value.lower().startswith("quicktime"):
        value = value[len("QuickTime"):]

    return value or "---"


def configuration_audio_channels(info):
    codec = info.get("audio codec", "").strip()
    channels = info.get("audio input channels", "").strip()

    if not channels:
        return "---"

    if codec:
        return f"{codec.upper()} {channels}"

    return channels


def set_hyperdeck_timecode_input(deck, tc_input):
    tc_input = tc_input.strip().lower()

    if tc_input not in TC_INPUT_VALUES:
        return False, "bad timecode input"

    ip = deck.get("ip", "").strip()

    if not ip:
        return False, "missing ip"

    def receive_ok(sock, timeout):
        last_response = ""

        # Opožděné 500 connection info může dorazit až po odeslání příkazu.
        # V tom případě přečteme ještě následující protokolovou zprávu.
        for _ in range(2):
            response = recv_hyperdeck_message(sock, timeout=timeout)
            last_response = response or last_response

            if "200 ok" in response.lower():
                return True, response.strip()

            if not response:
                break

        return False, last_response.strip()

    attempts = len(CONFIG_RETRY_DELAYS) + 1
    last_message = "deck neodpověděl"

    for attempt in range(attempts):
        command_accepted = False

        try:
            with socket.create_connection(
                (ip, 9993),
                timeout=CONFIG_CONNECT_TIMEOUT,
            ) as sock:
                recv_hyperdeck_message(sock, timeout=CONFIG_HELLO_TIMEOUT)

                sock.sendall(b"remote: override: true\r\n")
                remote_ok, remote_response = receive_ok(
                    sock,
                    CONFIG_RESPONSE_TIMEOUT,
                )

                if not remote_ok:
                    last_message = remote_response or "remote override nebyl potvrzen"
                else:
                    cmd = f"configuration: timecode input: {tc_input}\r\n"
                    sock.sendall(cmd.encode("ascii"))
                    command_accepted, write_response = receive_ok(
                        sock,
                        CONFIG_RESPONSE_TIMEOUT,
                    )
                    last_message = write_response or "zápis TC IN nebyl potvrzen"

        except OSError as e:
            last_message = str(e)

        if command_accepted:
            # Samotné 200 ok nestačí. Zařízení znovu načteme a ověříme, že
            # skutečně používá požadovanou hodnotu.
            for verify_delay in (0.15, 0.35):
                time.sleep(verify_delay)
                info = get_hyperdeck_configuration_info(
                    deck,
                    allow_cached_fallback=False,
                )
                actual = info.get("timecode input", "").strip().lower()

                if actual == tc_input:
                    return True, f"TC IN potvrzeno: {tc_input}"

                if actual:
                    last_message = f"deck vrátil původní hodnotu {actual}"
                else:
                    last_message = "zpětné ověření TC IN se nezdařilo"

        if attempt < len(CONFIG_RETRY_DELAYS):
            time.sleep(CONFIG_RETRY_DELAYS[attempt])

    return False, last_message


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


def make_video_format_select(index, current_value="1080p50"):
    current_value = normalize_video_format_choice(current_value)
    options = []

    for value in VIDEO_FORMAT_VALUES:
        selected = " selected" if value == current_value else ""
        options.append(f'<option value="{value}"{selected}>{value}</option>')

    return (
        f'<select class="select-input tc-select" name="deck_video_format_{index}">'
        + "".join(options)
        + '</select>'
    )


def make_alarm(cfg, status_map=None, show_active_recorders=False):
    active_recorders = get_active_recorders_html(cfg)

    if not cfg.get("tc_compare", True):
        ok_text = active_recorders if show_active_recorders else f"{active_recorders} | TC COMPARE OFF"
        return f'<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">{ok_text}</div>'

    if status_map is None:
        status_map = get_status_map(cfg)

    tc_compare = build_tc_compare(cfg, status_map)

    if tc_compare.get("reference_invalid", False):
        return '<div id="tcAlarm" class="alarm" style="display:block;">TC REFERENCE INVALID – Režie nemá platný TC</div>'

    if not tc_compare["reference"]:
        ok_text = active_recorders if show_active_recorders else f"{active_recorders} | nothing to compare"
        return f'<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">{ok_text}</div>'

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
                f'TC MISMATCH | {msg} - tolerance {tolerance}f'
                f'</div>'
            )

        return (
            f'<div id="tcAlarm" class="alarm" style="display:block;">'
            f'TC MISMATCH – reference {html.escape(ref["name"])} | {msg} - tolerance {tolerance}f'
            f'</div>'
        )

    tolerance = tc_compare["tolerance"]

    return (
        f'<div id="tcAlarm" class="alarm alarm-ok" style="display:block;">'
        f'{active_recorders if show_active_recorders else f"{active_recorders} | TC OK - tolerance {tolerance}f"}'
        f'</div>'
    )


def make_rows(cfg):
    rows = ""
    deck_video_formats = get_deck_video_formats(cfg)
    format_compare_enabled = cfg.get("format_compare", True)

    status_map = get_status_map(cfg)
    tc_compare = build_tc_compare(cfg, status_map)

    for i, deck in enumerate(cfg["decks"]):
        raw_name = deck.get("name", "")
        name = html.escape(raw_name)

        pgm = is_control_room_deck(cfg, i)
        armed = deck_is_armed_at_index(cfg, i, deck)

        if not armed:
            continue

        row_classes = []

        if pgm:
            row_classes.append("pgm-row")

        row_class = " ".join(row_classes)

        status = status_map.get(
            i,
            get_skipped_state()
        )
        status_text, led_class, format_text, tc, tc_valid, total_frames = status[:6]
        sd1, sd2 = (normalize_sd_state(item) for item in status[6:8])
        sd1_text, sd1_class = sd1
        sd2_text, sd2_class = sd2
        active_slot = status[8] if len(status) > 8 else None
        format_html = html.escape(format_text or "---")
        sd1_html = html.escape(sd1_text or "---")
        sd2_html = html.escape(sd2_text or "---")
        sd1_class_html = html.escape(sd1_class, quote=True)
        sd2_class_html = html.escape(sd2_class, quote=True)
        if active_slot == 1:
            sd1_class_html = (sd1_class_html + " sd-active").strip()
        elif active_slot == 2:
            sd2_class_html = (sd2_class_html + " sd-active").strip()
        expected_format = deck_video_formats[i]
        format_class = (
            "format"
            if format_is_ok(format_text, expected_format, format_compare_enabled)
            else "format format-warning"
        )
        tc_warning = tc_compare["by_index"].get(i, {}).get("warning", False)
        tc_class = "tc tc-warning" if tc_warning else "tc"

        if tc_valid:
            tc_cell = f'<td class="{tc_class} live-tc" data-role="tc" data-base-frame="{total_frames}">{tc}</td>'
        else:
            tc_cell = f'<td class="{tc_class}" data-role="tc">{tc}</td>'

        rows += f"""
<tr class="{row_class}" data-deck-index="{i}">
  <td class="name">{name}</td>
  <td class="sd"><span class="sd-box {sd1_class_html}" data-role="sd1">{sd1_html}</span></td>
  <td class="sd"><span class="sd-box {sd2_class_html}" data-role="sd2">{sd2_html}</span></td>
  <td>
    <form class="inline-form" method="POST" action="/action">
      <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
      <input type="hidden" name="deck" value="{i}">
      <input type="hidden" name="cmd" value="rec">
      <button class="btn rec" type="submit">REC</button>
    </form>
  </td>
  <td>
    <form class="inline-form" method="POST" action="/action">
      <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
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
    deck_video_formats = get_deck_video_formats(cfg)

    if load_tc_inputs:
        configuration_info = get_configuration_info_map(cfg)
    else:
        configuration_info = {}

    for i, deck in enumerate(cfg["decks"]):
        raw_name = deck.get("name", "")
        name = html.escape(raw_name, quote=True)
        ip = html.escape(deck.get("ip", ""), quote=True)
        control_room = is_control_room_deck(cfg, i)
        row_class = "pgm-row" if control_room else ""
        armed = deck_is_armed_at_index(cfg, i, deck)
        deck_configuration = configuration_info.get(i, {}) if armed else {}
        current_tc = configuration_timecode_input(deck_configuration)
        current_input = html.escape(configuration_video_input(deck_configuration) if armed else "---", quote=True)
        current_codec = html.escape(configuration_video_codec(deck_configuration) if armed else "---", quote=True)
        current_audio_channels = html.escape(configuration_audio_channels(deck_configuration) if armed else "---", quote=True)
        tc_select = make_tc_input_select(i, current_tc)
        video_format_select = make_video_format_select(i, deck_video_formats[i])

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
  <td>
    {video_format_select}
  </td>
  <td class="input-display">{current_input}</td>
  <td class="input-display">{current_codec}</td>
  <td class="input-display">{current_audio_channels}</td>
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
.advanced-btn{display:none}
body.settings-page .advanced-btn{display:inline-block}
.close-panel-btn{width:44px;height:44px;min-width:44px;padding:0;border:1px solid #5d2222;border-radius:4px;background:#3a1515;color:#fff;font-size:26px;font-weight:bold;line-height:1;cursor:pointer}
.close-panel-btn:hover{background:#7a0000;border-color:#ff6666;box-shadow:0 0 12px rgba(255,75,75,.55)}
.notice{
  display:block;
  width:fit-content;
  margin:8px auto 6px auto !important;
  padding:6px 14px !important;
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
.notice.notice-error{
  background:#6b1119 !important;
  color:#fff !important;
  box-shadow:0 0 14px rgba(255,51,68,.65) !important;
}
.alarm{display:none;margin-bottom:10px;padding:10px;border-radius:10px;background:#7a0000;color:#fff;text-align:center;font-size:24px;font-weight:bold;box-shadow:0 0 18px #f33}
.alarm-ok{background:#113b20;color:#78ff9c;box-shadow:0 0 14px #008d35}
.active-rec-value{color:#fff;font-weight:900;text-shadow:0 0 10px rgba(255,255,255,.55)}
.view{display:none}
.view.active{display:block}
.panel-card{background:#15151d;border:1px solid #33333d;border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th,td{padding:8px 10px;border-bottom:1px solid #33333d;text-align:center;font-size:19px}
tr:last-child td{border-bottom:none}
th{color:#a8a8b5;font-size:14px;text-transform:uppercase;letter-spacing:1px;background:#1d1d28}
/* column layout start */
.decks-table th:nth-child(1),.decks-table td:nth-child(1){width:14%}
.decks-table th:nth-child(2),.decks-table td:nth-child(2){width:11%}
.decks-table th:nth-child(3),.decks-table td:nth-child(3){width:11%}
.decks-table th:nth-child(4),.decks-table td:nth-child(4){width:11%}
.decks-table th:nth-child(5),.decks-table td:nth-child(5){width:11%}
.decks-table th:nth-child(6),.decks-table td:nth-child(6){width:14%}
.decks-table th:nth-child(7),.decks-table td:nth-child(7){width:13%}
.decks-table th:nth-child(8),.decks-table td:nth-child(8){width:15%}
.settings-table th:nth-child(1),.settings-table td:nth-child(1){width:6%}
.settings-table th:nth-child(2),.settings-table td:nth-child(2){width:16%}
.settings-table th:nth-child(3),.settings-table td:nth-child(3){width:16%}
.settings-table th:nth-child(4),.settings-table td:nth-child(4){width:14%}
.settings-table th:nth-child(5),.settings-table td:nth-child(5){width:13%}
.settings-table th:nth-child(6),.settings-table td:nth-child(6){width:11%}
.settings-table th:nth-child(7),.settings-table td:nth-child(7){width:14%}
.settings-table th:nth-child(8),.settings-table td:nth-child(8){width:10%}
.settings-table th{padding:8px 10px;font-size:16px;line-height:1.25}
.settings-table th,
.settings-table td,
.settings-table input,
.settings-table select,
.settings-table .input-display{
  font-family:Arial,sans-serif;
  font-size:20px;
  font-weight:600;
}
.settings-form .panel-card{width:70%;margin-left:auto;margin-right:auto}
/* column layout end */






.arm-cell{width:auto;padding-left:82px;padding-right:52px;text-align:center}
.settings-table .arm-cell{padding-left:6px;padding-right:6px}
.input-display{font-family:monospace;font-size:16px;color:#d8d8e4;font-weight:bold}
.arm-form{margin:0}
.arm-check{width:24px;height:24px;cursor:pointer;vertical-align:-5px}
.always-arm{font-size:14px;font-weight:bold;color:#78ff9c;letter-spacing:1px}
.name{font-weight:bold;text-align:center;font-size:23px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ip{color:#b8b8c0;font-size:16px;width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sd{width:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sd-box{display:inline-flex;align-items:center;justify-content:center;width:92%;min-width:76px;height:42px;padding:12px 14px 8px 14px;border-radius:9px;background:rgba(255,255,255,.035);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);font-family:monospace;font-size:21px;line-height:1;color:#78ff9c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sd-active{background:rgba(120,255,156,.12);box-shadow:inset 0 0 0 1px rgba(120,255,156,.28), inset 0 0 18px rgba(120,255,156,.10)}
.sd-empty{color:#d8d8e4!important;font-weight:normal;text-shadow:none}
.sd-warning{color:#ff4b4b!important;font-weight:bold;text-shadow:0 0 10px rgba(255,75,75,.45)}
.sd-full{color:#ff4b4b!important;font-weight:bold;text-shadow:0 0 10px rgba(255,75,75,.45);animation:sdFullBlink 1s steps(2,start) infinite}
@keyframes sdFullBlink{50%{opacity:.18}}
.sd-work{color:#ffd24a!important;font-weight:bold;text-shadow:0 0 10px rgba(255,210,74,.45);animation:sdWorkBlink 1.8s steps(2,start) infinite}
@keyframes sdWorkBlink{50%{opacity:.35}}
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
.decks-table tbody tr:not(.pgm-row):nth-child(odd) td{background:#111118}
.decks-table tbody tr:not(.pgm-row):nth-child(even) td{background:#1b1b25}
.decks-table tbody td{transition:background-color .14s ease}
.decks-table tbody tr:not(.pgm-row):hover td{background:#292936}
.settings-table tbody tr:not(.pgm-row):nth-child(odd) td{background:#111118}
.settings-table tbody tr:not(.pgm-row):nth-child(even) td{background:#1b1b25}
.settings-table tbody td{transition:background-color .14s ease}
.settings-table tbody tr:not(.pgm-row):hover td{background:#292936}
.pgm-row td{background:#202018;border-top:3px solid #777746}
.decks-table tbody .pgm-row:hover td{background:#303023}
.settings-table tbody .pgm-row:hover td{background:#303023}
.pgm-row .name::after{content:""}
.disarmed-row td{color:#777!important;filter:grayscale(1);opacity:.42}
.disarmed-row .btn{filter:grayscale(1)}
.bottom{display:flex;justify-content:center;gap:34px;margin-top:18px}
.big{width:270px;height:66px;font-size:28px;border-radius:14px}

/* main broadcast console start */
body.hyperdeck-page{
  background:
    radial-gradient(circle at 18% -10%,rgba(27,108,255,.22),transparent 34%),
    radial-gradient(circle at 88% 8%,rgba(120,255,156,.09),transparent 26%),
    linear-gradient(145deg,#07090e 0%,#0b0d14 48%,#08090d 100%);
}
body.hyperdeck-page .wrap{padding:14px 20px 38px}
.header{
  position:relative;
  min-height:72px;
  margin-bottom:12px;
  padding:11px 13px 11px 18px;
  border:1px solid rgba(128,151,190,.24);
  border-radius:16px;
  background:linear-gradient(135deg,rgba(27,31,43,.96),rgba(13,15,22,.96));
  box-shadow:0 18px 42px rgba(0,0,0,.32),inset 0 1px 0 rgba(255,255,255,.04);
}
h1{
  position:relative;
  padding-top:17px;
  color:#f4f7ff;
  font-size:27px;
  letter-spacing:2.8px;
  text-shadow:0 0 20px rgba(93,148,255,.25);
}
h1::before{
  content:"MULTICAM RECORDING";
  position:absolute;
  top:0;
  left:1px;
  color:#6fa2ff;
  font-size:10px;
  font-weight:800;
  letter-spacing:3.2px;
  text-shadow:none;
}
.active-count{display:none}
.topnav{margin-left:auto;gap:8px}
.navbtn{
  min-height:44px;
  padding:9px 18px;
  border-color:rgba(137,153,183,.24);
  background:rgba(37,42,56,.72);
  color:#b9c1d0;
  font-family:Arial,sans-serif;
  font-size:18px;
  font-weight:bold;
  letter-spacing:normal;
  transition:transform .14s ease,border-color .14s ease,background .14s ease,color .14s ease;
}
.navbtn:hover{
  transform:translateY(-1px);
  border-color:rgba(116,159,255,.65);
  background:#293149;
  color:#fff;
}
.navbtn.active{
  border-color:#6698ff;
  background:linear-gradient(135deg,#1556d6,#2378ff);
  color:#fff;
  box-shadow:0 0 0 1px rgba(102,152,255,.18),0 7px 22px rgba(27,108,255,.32);
}
.close-panel-btn{
  border-color:rgba(255,89,89,.34);
  border-radius:10px;
  background:rgba(85,24,29,.62);
}
body.hyperdeck-page .alarm{
  margin-bottom:12px;
  padding:8px 14px;
  border:1px solid rgba(255,83,83,.55);
  border-left:5px solid #ff4141;
  border-radius:11px;
  background:linear-gradient(90deg,rgba(133,15,22,.96),rgba(76,8,13,.92));
  font-size:24px;
  letter-spacing:.35px;
  box-shadow:0 8px 26px rgba(255,31,45,.17),inset 0 1px 0 rgba(255,255,255,.08);
}
body.hyperdeck-page .alarm-ok{
  border-color:rgba(76,230,132,.3);
  border-left-color:#4ce684;
  background:linear-gradient(90deg,rgba(17,76,43,.96),rgba(10,47,28,.9));
  box-shadow:0 8px 26px rgba(24,197,91,.13),inset 0 1px 0 rgba(255,255,255,.05);
}
body.hyperdeck-page #viewDecks .panel-card{
  border-color:rgba(129,150,187,.28);
  border-radius:16px;
  background:#10131b;
  box-shadow:0 24px 56px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.035);
}
body.hyperdeck-page .decks-table th{
  height:40px;
  padding:7px 10px;
  border-bottom:1px solid rgba(125,148,187,.25);
  background:linear-gradient(180deg,#1d2230,#171b26);
  color:#7f8da5;
  font-size:12px;
  font-weight:800;
  letter-spacing:1.7px;
}
body.hyperdeck-page .decks-table td{
  height:62px;
  padding-top:8px;
  padding-bottom:8px;
  border-bottom-color:rgba(113,130,160,.16);
}
body.hyperdeck-page .decks-table tbody tr:not(.pgm-row):nth-child(odd) td{background:rgba(15,18,26,.92)}
body.hyperdeck-page .decks-table tbody tr:not(.pgm-row):nth-child(even) td{background:rgba(21,25,35,.96)}
body.hyperdeck-page .decks-table tbody tr:not(.pgm-row):hover td{
  background:#252d3d;
  box-shadow:inset 0 1px 0 rgba(105,152,255,.14),inset 0 -1px 0 rgba(105,152,255,.14);
}
body.hyperdeck-page .name{
  color:#f4f7fc;
  font-size:23px;
  letter-spacing:.25px;
}
body.hyperdeck-page .sd-box{
  width:94%;
  height:38px;
  padding:9px 10px 7px;
  border-radius:8px;
  background:rgba(7,10,15,.58);
  box-shadow:inset 0 0 0 1px rgba(135,157,193,.13),0 5px 13px rgba(0,0,0,.13);
  font-size:22px;
}
body.hyperdeck-page .sd-active{
  background:rgba(45,170,94,.1);
  box-shadow:inset 0 0 0 1px rgba(92,231,143,.27),inset 0 0 18px rgba(56,216,117,.08);
}
body.hyperdeck-page .btn{
  border:1px solid rgba(255,255,255,.13);
  border-radius:10px;
  box-shadow:0 6px 15px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.14);
  letter-spacing:.6px;
  transition:transform .12s ease,filter .12s ease,box-shadow .12s ease;
}
body.hyperdeck-page .btn:hover{
  transform:translateY(-1px);
  filter:brightness(1.13);
}
body.hyperdeck-page .btn:active{transform:translateY(1px)}
body.hyperdeck-page .rec{background:linear-gradient(145deg,#cf1734,#8d071d)}
body.hyperdeck-page .stop{background:linear-gradient(145deg,#1673ef,#08489f)}
body.hyperdeck-page .status{
  padding-left:38px;
  color:#dce4f1;
  font-size:17px;
  letter-spacing:.3px;
}
body.hyperdeck-page .led{
  width:15px;
  height:15px;
  margin-right:11px;
  vertical-align:-1px;
  border:2px solid rgba(255,255,255,.25);
}
body.hyperdeck-page .format{color:#c2cbd9;font-size:18px}
body.hyperdeck-page .tc{
  color:#edf4ff;
  font-size:25px;
  letter-spacing:1.5px;
  text-shadow:0 0 15px rgba(109,158,255,.16);
}
body.hyperdeck-page .pgm-row td{
  background:linear-gradient(90deg,rgba(50,48,24,.98),rgba(36,37,25,.98));
  border-top:2px solid #aaa54d;
  border-bottom-color:rgba(224,211,90,.22);
}
body.hyperdeck-page .decks-table tbody .pgm-row:hover td{
  background:linear-gradient(90deg,#454326,#343625);
}
body.hyperdeck-page .pgm-row .name::after{
  content:"PGM";
  display:inline-block;
  margin-left:9px;
  padding:3px 6px 2px;
  border:1px solid rgba(255,224,86,.58);
  border-radius:5px;
  background:rgba(255,210,40,.12);
  color:#ffe76e;
  font-size:9px;
  line-height:1;
  letter-spacing:1px;
  vertical-align:3px;
}
body.hyperdeck-page .bottom{
  width:max-content;
  margin:14px auto 0;
  padding:10px;
  gap:12px;
  transform:translateX(-55px);
  border:1px solid rgba(128,151,190,.22);
  border-radius:16px;
  background:rgba(16,19,27,.84);
  box-shadow:0 14px 34px rgba(0,0,0,.28);
}
body.hyperdeck-page .big{
  width:250px;
  height:58px;
  border-radius:11px;
  font-size:24px;
}
body.hyperdeck-page .decks-table .sd-box,
body.hyperdeck-page .decks-table .btn,
body.hyperdeck-page .decks-table .status,
body.hyperdeck-page .decks-table .format{
  font-family:Arial,sans-serif;
  font-size:22px;
  font-weight:bold;
}
body.hyperdeck-page .decks-table .tc{font-family:Arial,sans-serif}
body.hyperdeck-page .decks-table .format{
  font-family:Arial,sans-serif;
  font-size:24px;
  font-weight:normal;
  letter-spacing:1.2px;
}
body.hyperdeck-page .decks-table .sd-box{
  font-size:23px;
  font-weight:normal;
  letter-spacing:1.2px;
}
body.hyperdeck-page .decks-table .btn,
body.hyperdeck-page .decks-table .status{font-size:21px}
/* main broadcast console end */

.logger-frame{width:100%;height:calc(100vh - 116px);border:1px solid #444;border-radius:12px;background:#000}
.logger-note{text-align:center;color:#aaa;margin-bottom:8px;font-size:15px}
.settings-note{margin-top:14px;text-align:center;color:#aaa;font-size:18px}
.network-interfaces{display:flex;justify-content:center;align-items:center;gap:18px;margin-top:20px;color:#d8d8e4;font-family:Arial,sans-serif;font-size:18px;font-weight:600}
.network-interface{padding:7px 14px;border:1px solid #444;border-radius:8px;background:#0f0f15;white-space:nowrap}
.network-interface span{color:#78ff9c}
.settings-form{width:100%;max-width:none;margin:0 auto}
.settings-form table{margin-top:10px}
.settings-field-row{display:grid;grid-template-columns:190px minmax(100px,17%);align-items:center;gap:10px;margin-top:8px}
.settings-field-row:first-child{margin-top:0}
.settings-field-row .small-label{margin:0;text-align:right}
.settings-field-row.format-row{grid-template-columns:190px minmax(100px,17%) max-content 78px}
.format-btn{background:#49495a;color:#ff4b4b;border:1px solid #69697a;border-radius:8px;padding:9px 16px;font-size:15px;font-weight:bold;cursor:pointer;white-space:nowrap}
.format-btn:hover{background:#5a5a6d;border-color:#8a8aa0}
.format-btn:disabled{opacity:.55;cursor:wait}
.format-slot-status{text-align:center}
.settings-inline-row{display:grid;grid-template-columns:repeat(4,max-content);justify-content:space-between;gap:18px;width:70%;margin:26px auto 24px auto;box-sizing:border-box}
.settings-inline-item{display:grid;grid-template-columns:max-content 118px;align-items:center;justify-content:stretch;gap:8px}
.settings-inline-item .small-label{margin:0;text-align:right;font-family:Arial,sans-serif;font-size:20px;font-weight:600;white-space:nowrap}
.settings-inline-item .text-input{font-family:Arial,sans-serif;font-size:20px;font-weight:600;height:34px;padding:4px 6px}
.settings-inline-item.wide-control{grid-template-columns:max-content 118px}
.text-input{width:100%;background:#0f0f15;color:#eee;border:1px solid #555;border-radius:8px;padding:10px 12px;font-size:22px}
.name-input{font-weight:bold}
.ip-input{font-family:monospace;text-align:center}
.settings-table .name-input,.settings-table .ip-input{width:82%;display:block;margin:0 auto}
.settings-table .ip-input,.settings-table .tc-select{text-align:center}
.settings-table select,
.settings-inline-item select{
  background:#0f0f15;
  color:#eee;
  border:1px solid #555;
  border-radius:8px;
}
.settings-table select option,
.settings-inline-item select option{
  background:#0f0f15;
  color:#eee;
}
.save-row{display:flex;justify-content:center;gap:24px;margin-top:42px}
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
  width:100% !important;
  max-width:none !important;
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

.settings-table th{
  padding:9px 6px !important;
  font-size:15px !important;
  line-height:1.15 !important;
}

.settings-form input[type="text"]:not(.name-input):not(.ip-input),
.settings-form select,
.settings-form .text-input:not(.name-input):not(.ip-input){
  font-size:13px !important;
  padding:3px 6px !important;
  min-height:24px !important;
  height:26px !important;
  box-sizing:border-box !important;
}

.settings-table th,
.settings-table td,
.settings-table input,
.settings-table select,
.settings-table .input-display{
  font-family:Arial,sans-serif !important;
  font-size:20px !important;
  font-weight:600 !important;
}

.settings-table input,
.settings-table select{
  height:34px !important;
  padding:4px 6px !important;
  line-height:1.2 !important;
}

.settings-field-row{
  grid-template-columns:160px minmax(90px,17%) !important;
  gap:8px !important;
  margin-top:4px !important;
}

.settings-field-row.format-row{
  grid-template-columns:160px minmax(90px,17%) max-content 70px !important;
}

.settings-inline-row{
  grid-template-columns:repeat(4,max-content) !important;
  justify-content:space-between !important;
  gap:18px !important;
  width:70% !important;
  margin:22px auto 22px auto !important;
  box-sizing:border-box !important;
}

.settings-inline-item{
  grid-template-columns:max-content 118px !important;
  gap:6px !important;
}

.settings-inline-item.wide-control{
  grid-template-columns:max-content 118px !important;
}

.settings-inline-item .text-input{
  font-family:Arial,sans-serif !important;
  font-size:20px !important;
  font-weight:600 !important;
  height:34px !important;
  padding:4px 6px !important;
  line-height:1.2 !important;
}

.settings-inline-item .small-label{
  font-family:Arial,sans-serif !important;
  font-size:20px !important;
  font-weight:600 !important;
}

.settings-table select,
.settings-inline-item select{
  background:#0f0f15 !important;
  color:#eee !important;
  border:1px solid #555 !important;
  border-radius:8px !important;
}

.settings-table select option,
.settings-inline-item select option{
  background:#0f0f15 !important;
  color:#eee !important;
}

.settings-table .name-input,
.settings-table .ip-input{
  font-family:Arial,sans-serif !important;
  font-size:20px !important;
  font-weight:600 !important;
  line-height:1.2 !important;
  height:34px !important;
  padding:4px 6px !important;
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

.network-interfaces{
  margin-top:20px !important;
  gap:12px !important;
  font-size:15px !important;
}

.network-interface{
  padding:5px 10px !important;
}

.save-row{
  margin-top:26px !important;
  gap:12px !important;
}

#viewSettings .save-row{
  width:70% !important;
  margin-left:auto !important;
  margin-right:auto !important;
  justify-content:flex-end !important;
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

/* advanced layout start */
#viewAdvanced .settings-form{
  width:min(1180px,94%) !important;
  margin:18px auto 56px auto !important;
}
.advanced-page-intro{
  margin:0 0 18px 0 !important;
  color:#a8a8b5;
  font-size:19px;
  line-height:1.45;
  text-align:center;
}
.advanced-grid{
  display:grid;
  grid-template-columns:minmax(560px,1.6fr) minmax(280px,.8fr);
  gap:18px;
  align-items:stretch;
}
.advanced-card{
  min-width:0;
  padding:22px;
  border:1px solid #33333d;
  border-radius:14px;
  background:#15151d;
  box-shadow:0 10px 28px rgba(0,0,0,.22);
}
.advanced-card-header{
  margin-bottom:18px;
  padding-bottom:14px;
  border-bottom:1px solid #33333d;
}
.advanced-card-title{
  margin:0 0 5px 0 !important;
  color:#f2f2f6;
  font-size:26px !important;
  letter-spacing:.5px;
}
.advanced-card-description{
  margin:0 !important;
  color:#9696a5;
  font-size:17px !important;
  line-height:1.4 !important;
}
.advanced-field{
  display:grid;
  gap:7px;
}
.advanced-field label{
  color:#c6c6d0;
  font-size:17px;
  font-weight:bold;
  letter-spacing:.5px;
  text-transform:uppercase;
}
#viewAdvanced .advanced-field .text-input{
  width:100%;
  height:44px !important;
  padding:8px 12px !important;
  border:1px solid #4a4a58;
  border-radius:9px;
  background:#0f0f15;
  color:#fff;
  font-family:monospace !important;
  font-size:23px !important;
  font-weight:bold !important;
}
#viewAdvanced .advanced-field .text-input:focus{
  border-color:#6c9dff;
  outline:none;
  box-shadow:0 0 0 3px rgba(27,108,255,.2);
}
.network-summary{
  margin-top:22px;
  padding-top:16px;
  border-top:1px solid #2d2d37;
}
.network-summary-title{
  margin-bottom:9px;
  color:#777786;
  font-size:14px;
  font-weight:bold;
  letter-spacing:1px;
  text-transform:uppercase;
}
#viewAdvanced .network-interfaces{
  display:grid;
  gap:8px !important;
  justify-content:stretch;
  margin:0 !important;
  font-size:18px !important;
}
#viewAdvanced .network-interface{
  display:flex;
  justify-content:space-between;
  gap:16px;
  padding:8px 10px !important;
  border-radius:8px;
  background:#101017;
  font-family:monospace;
}
.media-card{
  border-color:#4a3030;
  background:linear-gradient(180deg,#19151a 0%,#15151d 100%);
}
.media-card .advanced-card-header{
  border-bottom-color:#4a3030;
}
.danger-label{
  display:inline-block;
  margin-bottom:7px;
  padding:4px 8px;
  border:1px solid #7a3333;
  border-radius:6px;
  background:#351616;
  color:#ff7777;
  font-size:13px;
  font-weight:bold;
  letter-spacing:1px;
  text-transform:uppercase;
}
.format-device-grid{
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:14px;
}
.format-device-card{
  display:grid;
  gap:16px;
  padding:16px;
  border:1px solid #3a3a46;
  border-radius:11px;
  background:#101017;
}
.format-device-name{
  margin:0;
  color:#eee;
  font-size:23px;
}
.format-device-heading{
  display:grid;
  gap:9px;
}
.format-device-deck-badge{
  display:flex;
  align-items:center;
  justify-content:center;
  min-height:38px;
  padding:7px 12px;
  border:1px solid #319454;
  border-radius:8px;
  background:#123a20;
  color:#83ffa5;
  font-size:19px;
  font-weight:900;
  letter-spacing:.7px;
  text-align:center;
  text-transform:uppercase;
  box-shadow:0 0 14px rgba(48,190,91,.18);
}
.format-device-deck-badge.deck-unknown{
  border-color:#9a692d;
  background:#3c2812;
  color:#ffc66d;
  box-shadow:0 0 14px rgba(255,174,67,.16);
}
.format-device-actions{
  display:grid;
  grid-template-columns:1fr;
  gap:10px;
  padding-top:14px;
  border-top:1px solid #30303a;
}
.format-slot-grid{
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:10px;
}
.format-slot-block{
  display:grid;
  gap:5px;
}
.format-slot-label{
  color:#777786;
  font-size:14px;
  font-weight:bold;
  letter-spacing:.8px;
  text-transform:uppercase;
}
.format-selection-label{
  color:#777786;
  font-size:14px;
  font-weight:bold;
  letter-spacing:.8px;
  text-transform:uppercase;
}
.format-choice-grid{
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:8px;
}
.format-choice{
  display:block;
  cursor:pointer;
}
.format-choice input{
  position:absolute;
  opacity:0;
  pointer-events:none;
}
.format-choice span{
  display:flex;
  align-items:center;
  justify-content:center;
  min-height:40px;
  border:1px solid #494957;
  border-radius:8px;
  background:#191920;
  color:#aaaab6;
  font-size:17px;
  font-weight:bold;
  transition:.15s ease;
}
.format-choice input:checked + span{
  border-color:#d95757;
  background:#4b1d1d;
  color:#ff8888;
  box-shadow:inset 0 0 0 1px rgba(255,105,105,.2);
}
.format-choice input:focus-visible + span{
  outline:2px solid #7dabff;
  outline-offset:2px;
}
.format-choice input:disabled + span{
  border-color:#303038;
  background:#131319;
  color:#555560;
  box-shadow:none;
  cursor:not-allowed;
  opacity:.6;
}
#viewAdvanced .format-slot-status{
  display:flex;
  align-items:center;
  justify-content:center;
  min-height:42px;
  padding:8px 10px;
  border:1px solid #353542;
  border-radius:8px;
  background:#181820;
  font-family:monospace;
  font-size:22px;
  font-weight:bold;
}
#viewAdvanced .format-btn{
  width:100%;
  min-height:42px;
  padding:8px 12px;
  font-size:18px;
  border-color:#8a3d3d;
  background:#3a1717;
  color:#ff6b6b;
}
#viewAdvanced .format-btn:hover{
  border-color:#ff6666;
  background:#641d1d;
  box-shadow:0 0 12px rgba(255,75,75,.28);
}
.advanced-save-panel{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:24px;
  margin-top:18px;
  padding:14px 18px;
  border:1px solid #33333d;
  border-radius:12px;
  background:#15151d;
}
.advanced-save-note{
  color:#a8a8b5;
  font-size:17px;
  line-height:1.35;
}
#viewAdvanced .save-row{
  flex:0 0 auto;
  margin:0 !important;
}
#viewAdvanced .save-btn{
  width:190px !important;
  height:44px !important;
  font-size:18px !important;
}
@media(max-width:900px){
  .advanced-grid{grid-template-columns:1fr}
  .format-device-grid{grid-template-columns:1fr}
}
/* advanced layout end */


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
      <button id="btnAdvanced" class="navbtn advanced-btn" type="button" onclick="window.location.href='/advanced'">ADVANCED</button>
      <button class="close-panel-btn" type="button" title="Zavřít panel" onclick="closePanel()">×</button>
    </div>
  </div>

  __ALARM__
  __ACTION_NOTICE__

  <div id="viewDecks" class="view active">
    <div class="panel-card">
      <table class="decks-table">
        <thead>
          <tr>
            <th>Deck</th>
            <th>SLOT 1</th>
            <th>SLOT 2</th>
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
        <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
        <input type="hidden" name="cmd" value="rec_all">
        <button class="btn rec big" type="submit">REC ALL</button>
      </form>

      <form class="inline-form" method="POST" action="/action">
        <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
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
      <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
      <div class="panel-card">
        <table class="settings-table">
          <thead>
            <tr>
              <th>ARM</th>
              <th>Deck name</th>
              <th>IP address</th>
              <th>TC IN</th>
              <th>V FORMAT</th>
              <th>V INPUT</th>
              <th>V CODEC</th>
              <th>AUDIO</th>
            </tr>
          </thead>
          <tbody>
            __SETTINGS_ROWS__
          </tbody>
        </table>
      </div>

      <div class="settings-inline-row">
        <div class="settings-inline-item">
          <div class="small-label">TC COMPARE</div>
          <select class="text-input ip-input" name="tc_compare">
            <option value="yes" __TC_COMPARE_YES_SELECTED__>Ano</option>
            <option value="no" __TC_COMPARE_NO_SELECTED__>Ne</option>
          </select>
        </div>

        <div class="settings-inline-item">
          <div class="small-label">TC REFERENCE</div>
          <select class="text-input ip-input" name="tc_reference_mode">
            <option value="group" __TC_REF_GROUP_SELECTED__>Skupina</option>
            <option value="control_room" __TC_REF_CONTROL_ROOM_SELECTED__>Režie</option>
          </select>
        </div>

        <div class="settings-inline-item">
          <div class="small-label">TC TOL. FRAMES</div>
          <input class="text-input ip-input" type="number" min="0" max="50" name="tc_tolerance_frames" value="__TC_TOLERANCE_FRAMES__">
        </div>

        <div class="settings-inline-item">
          <div class="small-label">V FORMAT COMPARE</div>
          <select class="text-input ip-input" name="format_compare">
            <option value="yes" __FORMAT_COMPARE_YES_SELECTED__>Ano</option>
            <option value="no" __FORMAT_COMPARE_NO_SELECTED__>Ne</option>
          </select>
        </div>
      </div>

      __NOTICE__

      <div class="save-row">
        <button class="btn save-btn" type="submit">ULOŽIT</button>
      </div>

    </form>
  </div>

  <div id="viewAdvanced" class="view">
    <form class="settings-form" method="POST" action="/save-advanced">
      <input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
      <p class="advanced-page-intro">Síťová integrace a servisní nástroje panelu.</p>

      <div class="advanced-grid">
        <section class="advanced-card media-card">
          <div class="advanced-card-header">
            <span class="danger-label">Pozor – destruktivní operace</span>
            <h2 class="advanced-card-title">Správa médií</h2>
            <p class="advanced-card-description">Formátování smaže obsah vybraných médií. Novou IP adresu nejprve uložte.</p>
          </div>

          <div class="format-device-grid">
            <article class="format-device-card">
              <div class="format-device-heading">
                <h3 class="format-device-name">HyperDeck HD Mini</h3>
                __FORMAT_DECK_HD_MINI__
              </div>
              <div class="advanced-field">
                <label for="formatIpHdMini">IP adresa zařízení</label>
                <input id="formatIpHdMini" class="text-input ip-input" type="text" name="format_ip_hd_mini" value="__FORMAT_IP_HD_MINI__">
              </div>
              <div class="format-slot-grid">
                <div class="format-slot-block">
                  <span class="format-slot-label">SD 1 - zbývající čas</span>
                  <div id="hdMiniSlot1Status" class="sd sd-empty format-slot-status">---</div>
                </div>
                <div class="format-slot-block">
                  <span class="format-slot-label">SD 2 - zbývající čas</span>
                  <div id="hdMiniSlot2Status" class="sd sd-empty format-slot-status">---</div>
                </div>
              </div>
              <div class="format-device-actions">
                <span class="format-selection-label">Formátovat</span>
                <div class="format-choice-grid">
                  <label class="format-choice"><input id="hdMiniSlot1Choice" type="checkbox" checked onchange="updateFormatButton('hdMini')"><span>SD 1</span></label>
                  <label class="format-choice"><input id="hdMiniSlot2Choice" type="checkbox" onchange="updateFormatButton('hdMini')"><span>SD 2</span></label>
                </div>
                <button id="hdMiniFormatBtn" class="format-btn" type="button" onclick="formatDevice('hdMini')">FORMAT SD 1</button>
              </div>
            </article>

            <article class="format-device-card">
              <div class="format-device-heading">
                <h3 class="format-device-name">HyperDeck 4K Pro</h3>
                __FORMAT_DECK_4K_PRO__
              </div>
              <div class="advanced-field">
                <label for="formatIp4kPro">IP adresa zařízení</label>
                <input id="formatIp4kPro" class="text-input ip-input" type="text" name="format_ip_4k_pro" value="__FORMAT_IP_4K_PRO__">
              </div>
              <div class="format-slot-grid">
                <div class="format-slot-block">
                  <span class="format-slot-label">SSD 1 - zbývající čas</span>
                  <div id="format4kProSlot1Status" class="sd sd-empty format-slot-status">---</div>
                </div>
                <div class="format-slot-block">
                  <span class="format-slot-label">SSD 2 - zbývající čas</span>
                  <div id="format4kProSlot2Status" class="sd sd-empty format-slot-status">---</div>
                </div>
              </div>
              <div class="format-device-actions">
                <span class="format-selection-label">Formátovat</span>
                <div class="format-choice-grid">
                  <label class="format-choice"><input id="format4kProSlot1Choice" type="checkbox" checked onchange="updateFormatButton('format4kPro')"><span>SSD 1</span></label>
                  <label class="format-choice"><input id="format4kProSlot2Choice" type="checkbox" onchange="updateFormatButton('format4kPro')"><span>SSD 2</span></label>
                </div>
                <button id="format4kProBtn" class="format-btn" type="button" onclick="formatDevice('format4kPro')">FORMAT SSD 1</button>
              </div>
            </article>
          </div>
        </section>

        <section class="advanced-card">
          <div class="advanced-card-header">
            <h2 class="advanced-card-title">Síť a integrace</h2>
            <p class="advanced-card-description">Připojení externího ATEM loggeru a síťové adresy tohoto panelu.</p>
          </div>

          <div class="advanced-field">
            <label for="atemLoggerIp">ATEM Logger IP</label>
            <input id="atemLoggerIp" class="text-input ip-input" type="text" name="atem_logger_ip" value="__ATEM_LOGGER_IP__">
          </div>

          <div class="network-summary">
            <div class="network-summary-title">Aktivní rozhraní Raspberry Pi</div>
            <div class="network-interfaces">
              __NETWORK_INTERFACES__
            </div>
          </div>
        </section>
      </div>

      __NOTICE__

      <div class="advanced-save-panel">
        <div class="advanced-save-note">Uloží síťové adresy. Změny nemají vliv na probíhající záznam.</div>
        <div class="save-row">
          <button class="btn save-btn" type="submit">ULOŽIT ZMĚNY IP</button>
        </div>
      </div>

    </form>
  </div>

  <div class="footer">
    Na tomto programu se podíleli Astra (ChatGPT) a Codík (Codex), moji AI asistenti od OpenAI.
  </div>
</div>

<script>

const CSRF_TOKEN = "__CSRF_TOKEN__";

async function closePanel() {
  try {
    await fetch("/api/close-panel", {
      method: "POST",
      headers: {"X-CSRF-Token": CSRF_TOKEN},
      cache: "no-store"
    });
  } catch (err) {
    console.log("Close panel error:", err);
  }
}

const formatDevices = {
  hdMini: {
    label: "HD Mini",
    mediaLabel: "SD",
    ip: "__FORMAT_IP_HD_MINI__",
    formatUrl: "/api/format-hd-mini",
    statusUrl: "/api/format-hd-mini-status",
    buttonId: "hdMiniFormatBtn",
    choiceIds: {1: "hdMiniSlot1Choice", 2: "hdMiniSlot2Choice"},
    statusIds: {1: "hdMiniSlot1Status", 2: "hdMiniSlot2Status"},
    availability: {1: null, 2: null},
    selectedBeforeUnavailable: {1: true, 2: false},
    busy: false,
    working: false
  },
  format4kPro: {
    label: "4K Pro",
    mediaLabel: "SSD",
    ip: "__FORMAT_IP_4K_PRO__",
    formatUrl: "/api/format-4k-pro",
    statusUrl: "/api/format-4k-pro-status",
    buttonId: "format4kProBtn",
    choiceIds: {1: "format4kProSlot1Choice", 2: "format4kProSlot2Choice"},
    statusIds: {1: "format4kProSlot1Status", 2: "format4kProSlot2Status"},
    availability: {1: null, 2: null},
    selectedBeforeUnavailable: {1: true, 2: false},
    busy: false,
    working: false
  }
};

let formatStatusTimer = null;

function getSelectedFormatSlots(device) {
  return [1, 2].filter(slotId => {
    const input = document.getElementById(device.choiceIds[slotId]);
    return input && input.checked;
  });
}

function updateFormatButton(deviceKey) {
  const device = formatDevices[deviceKey];

  if (!device) {
    return;
  }

  const button = document.getElementById(device.buttonId);

  if (!button) {
    return;
  }

  const slots = getSelectedFormatSlots(device);
  const media = slots.map(slotId => `${device.mediaLabel} ${slotId}`).join(" + ");
  button.textContent = media ? `FORMAT ${media}` : "FORMAT – VYBERTE MÉDIUM";
  button.disabled = device.working || slots.length === 0;
}

function setFormatSlotAvailability(deviceKey, slotId, available) {
  const device = formatDevices[deviceKey];

  if (!device || device.working) {
    return;
  }

  const input = document.getElementById(device.choiceIds[slotId]);

  if (!input) {
    return;
  }

  const previousAvailability = device.availability[slotId];
  input.disabled = !available;

  if (!available) {
    if (previousAvailability !== false) {
      device.selectedBeforeUnavailable[slotId] = input.checked;
    }
    input.checked = false;
  } else if (
    previousAvailability === false
    && device.selectedBeforeUnavailable[slotId]
  ) {
    input.checked = true;
  }

  device.availability[slotId] = available;
}

function setFormatWorking(deviceKey, active, selectedSlots=[]) {
  const device = formatDevices[deviceKey];

  if (!device) {
    return;
  }

  device.working = active;

  [1, 2].forEach(slotId => {
    const input = document.getElementById(device.choiceIds[slotId]);

    if (input) {
      input.disabled = active || device.availability[slotId] === false;
    }
  });

  if (active) {
    selectedSlots.forEach((slotId, index) => {
      const statusEl = document.getElementById(device.statusIds[slotId]);

      if (statusEl) {
        statusEl.textContent = index === 0 ? "WORK..." : "WAIT...";
        statusEl.classList.add("sd-work");
        statusEl.classList.remove("sd-empty", "sd-warning", "sd-full");
      }
    });
  }

  updateFormatButton(deviceKey);
}

async function formatDevice(deviceKey) {
  const device = formatDevices[deviceKey];

  if (!device) {
    return;
  }

  const selectedSlots = getSelectedFormatSlots(device);

  if (!selectedSlots.length) {
    alert("Vyberte alespoň jedno médium k formátování.");
    return;
  }

  const media = selectedSlots.map(slotId => `${device.mediaLabel} ${slotId}`).join(" a ");
  const sequenceNote = selectedSlots.length > 1 ? "\\nMédia budou formátována postupně." : "";
  const ok = confirm(`Opravdu FORMÁTOVAT ${media} v ${device.label}?\\n\\nIP: ${device.ip}\\nFilesystem: exFAT${sequenceNote}\\n\\nTato akce nevratně smaže vybraná média.`);

  if (!ok) {
    return;
  }

  setFormatWorking(deviceKey, true, selectedSlots);

  try {
    const body = new URLSearchParams();
    selectedSlots.forEach(slotId => body.append("slot", String(slotId)));

    const response = await fetch(device.formatUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRF-Token": CSRF_TOKEN
      },
      body: body.toString(),
      cache: "no-store"
    });
    const result = await response.json();

    setFormatWorking(deviceKey, false);
    alert(result.message || (result.ok ? "Formátování hotovo." : "Formátování selhalo."));
    updateFormatStatuses(deviceKey);
  } catch (err) {
    alert("Formátování selhalo: " + err);
    setFormatWorking(deviceKey, false);
  }
}

function applyFormatSlotStatus(device, slotId, status) {
  const el = document.getElementById(device.statusIds[slotId]);

  if (!el) {
    return;
  }

  el.textContent = status.text || "---";
  el.classList.remove("sd-work");
  el.classList.toggle("sd-empty", !!status.empty || el.textContent === "---");
  el.classList.toggle("sd-warning", !!status.warning);
  el.classList.toggle("sd-full", !!status.full);
}

async function updateFormatStatuses(deviceKey) {
  const device = formatDevices[deviceKey];

  if (!device || device.busy) {
    return;
  }

  device.busy = true;

  try {
    const response = await fetch(device.statusUrl, {
      method: "GET",
      cache: "no-store"
    });
    const status = await response.json();
    const progress = status.formatting || {active: false};

    [1, 2].forEach(slotId => {
      const slotStatus = status.slots && status.slots[String(slotId)];

      if (device.working && progress.active && progress.slots.includes(slotId)) {
        const el = document.getElementById(device.statusIds[slotId]);

        if (el && slotId >= progress.current_slot) {
          el.textContent = slotId === progress.current_slot ? "WORK..." : "WAIT...";
          el.classList.add("sd-work");
          el.classList.remove("sd-empty", "sd-warning", "sd-full");
          return;
        }
      }

      if (slotStatus && slotStatus.valid) {
        applyFormatSlotStatus(device, slotId, slotStatus);
        setFormatSlotAvailability(deviceKey, slotId, !slotStatus.empty);
      }
    });

    updateFormatButton(deviceKey);
  } catch (err) {
    // Krátký výpadek komunikace nesmí přepsat poslední ověřený stav ani
    // zrušit výběr média. Další pravidelný dotaz stav znovu ověří.
    console.log("Format status error:", deviceKey, err);
  } finally {
    device.busy = false;
  }
}

function updateAllFormatSlotStatuses() {
  updateFormatStatuses("hdMini");
  updateFormatStatuses("format4kPro");
}

function startFormatStatusPolling() {
  updateAllFormatSlotStatuses();

  if (formatStatusTimer) {
    return;
  }

  formatStatusTimer = setInterval(updateAllFormatSlotStatuses, 2000);
}

function stopFormatStatusPolling() {
  if (!formatStatusTimer) {
    return;
  }

  clearInterval(formatStatusTimer);
  formatStatusTimer = null;
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
    document.body.classList.toggle("settings-page", name === "settings" || name === "advanced");
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

  if (name === "advanced") {
    startFormatStatusPolling();
  } else {
    stopFormatStatusPolling();
  }

  removeActive("viewDecks");
  removeActive("viewLogger");
  removeActive("viewSettings");
  removeActive("viewAdvanced");

  removeActive("btnDecks");
  removeActive("btnLogger");
  removeActive("btnSettings");
  removeActive("btnAdvanced");

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

  if (name === "advanced") {
    addActive("viewAdvanced");
    addActive("btnAdvanced");
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

    const sd1Cell = row.querySelector('[data-role="sd1"]');

    if (sd1Cell) {
      sd1Cell.textContent = deck.sd1 || "---";
      const sd1Empty = !!deck.sd1_empty || (sd1Cell.textContent === "---" && !deck.sd1_warning && !deck.sd1_full);
      sd1Cell.classList.toggle("sd-empty", sd1Empty);
      sd1Cell.classList.toggle("sd-warning", !!deck.sd1_warning);
      sd1Cell.classList.toggle("sd-full", !!deck.sd1_full);
      sd1Cell.classList.toggle("sd-active", !!deck.sd1_active);
    }

    const sd2Cell = row.querySelector('[data-role="sd2"]');

    if (sd2Cell) {
      sd2Cell.textContent = deck.sd2 || "---";
      const sd2Empty = !!deck.sd2_empty || (sd2Cell.textContent === "---" && !deck.sd2_warning && !deck.sd2_full);
      sd2Cell.classList.toggle("sd-empty", sd2Empty);
      sd2Cell.classList.toggle("sd-warning", !!deck.sd2_warning);
      sd2Cell.classList.toggle("sd-full", !!deck.sd2_full);
      sd2Cell.classList.toggle("sd-active", !!deck.sd2_active);
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

  if (path === "/advanced" || view === "advanced") {
    showView("advanced");
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

  const hideDelay = notice.classList.contains("notice-error") ? 8000 : 1700;

  setTimeout(function() {
    notice.classList.add("toast-hide");
  }, hideDelay);

  setTimeout(function() {
    if (notice.parentNode) {
      notice.parentNode.removeChild(notice);
    }
  }, hideDelay + 500);

  try {
    const url = new URL(window.location.href);

    if (
      url.searchParams.get("saved") === "1"
      || url.searchParams.has("tc_failed")
      || url.searchParams.has("action_result")
      || url.searchParams.has("action_failed")
    ) {
      url.searchParams.delete("saved");
      url.searchParams.delete("tc_failed");
      url.searchParams.delete("action_result");
      url.searchParams.delete("action_failed");
      const newUrl = url.pathname + (url.search ? url.search : "") + url.hash;
      window.history.replaceState({}, "", newUrl);
    }
  } catch (e) {
    console.log(e);
  }
})();
// saved toast end

if (document.getElementById("hdMiniSlot1Status")) {
  startFormatStatusPolling();
}

</script>
</body>
</html>
"""


def build_status_api():
    cfg = load_config()
    deck_video_formats = get_deck_video_formats(cfg)
    format_compare_enabled = cfg.get("format_compare", True)
    decks_out = []
    status_map = get_status_map(cfg)
    tc_compare = build_tc_compare(cfg, status_map)

    for i, deck in enumerate(cfg["decks"]):
        if not deck_is_armed_at_index(cfg, i, deck):
            continue

        status = status_map.get(
            i,
            get_skipped_state()
        )
        status_text, led_class, format_text, tc, tc_valid, total_frames = status[:6]
        sd1, sd2 = (normalize_sd_state(item) for item in status[6:8])
        sd1_text, sd1_class = sd1
        sd2_text, sd2_class = sd2
        active_slot = status[8] if len(status) > 8 else None
        expected_format = deck_video_formats[i]
        tc_info = tc_compare["by_index"].get(i, {})

        decks_out.append({
            "index": i,
            "name": deck.get("name", f"REC {i + 1}"),
            "ip": deck.get("ip", ""),
            "status": status_text,
            "led": led_class,
            "format": format_text,
            "format_ok": format_is_ok(
                format_text,
                expected_format,
                format_compare_enabled,
            ),
            "sd1": sd1_text,
            "sd1_empty": sd1_class == "sd-empty",
            "sd1_warning": sd1_class == "sd-warning",
            "sd1_full": sd1_class == "sd-full",
            "sd1_active": active_slot == 1,
            "sd2": sd2_text,
            "sd2_empty": sd2_class == "sd-empty",
            "sd2_warning": sd2_class == "sd-warning",
            "sd2_full": sd2_class == "sd-full",
            "sd2_active": active_slot == 2,
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


def build_format_slots_status_payload(nvm_key):
    cfg = load_config()
    advanced_nvm = load_advanced_nvm()
    ip = advanced_nvm.get(nvm_key, "")
    slots = {}
    slot_results = get_format_slots_status(
        ip,
        expected_format=cfg.get("expected_format", "1080p50"),
        slot_ids=(1, 2),
    )

    for slot_id in (1, 2):
        result = slot_results.get(slot_id, {})
        text = result.get("text", "---")
        css_class = result.get("css_class", "sd-empty")
        slots[str(slot_id)] = {
            "text": text,
            "empty": css_class == "sd-empty",
            "warning": css_class == "sd-warning",
            "full": css_class == "sd-full",
            "valid": bool(result.get("valid", False)),
            "fresh": bool(result.get("fresh", False)),
        }

    with FORMAT_PROGRESS_LOCK:
        progress = copy.deepcopy(FORMAT_PROGRESS.get(nvm_key, {"active": False}))

    return {
        "slots": slots,
        "formatting": progress,
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
        return re.sub(r'class="([^"]*)"', r'class="\1 initial-settings settings-page"', tag, count=1)

    if "class='" in tag:
        return re.sub(r"class='([^']*)'", r"class='\1 initial-settings settings-page'", tag, count=1)

    return tag[:-1] + ' class="initial-settings settings-page">'


def add_hyperdeck_page_class(match):
    tag = match.group(0)

    if 'class="' in tag:
        return re.sub(r'class="([^"]*)"', r'class="\1 hyperdeck-page"', tag, count=1)

    if "class='" in tag:
        return re.sub(r"class='([^']*)'", r"class='\1 hyperdeck-page'", tag, count=1)

    return tag[:-1] + ' class="hyperdeck-page">'


def apply_real_page_split(page, initial_view):
    deck_match = re.search(r'\n\s*<div id="viewDecks"[^>]*>', page)
    logger_match = re.search(r'\n\s*<div id="viewLogger"[^>]*>', page)
    settings_match = re.search(r'\n\s*<div id="viewSettings"[^>]*>', page)
    advanced_match = re.search(r'\n\s*<div id="viewAdvanced"[^>]*>', page)
    footer_match = re.search(r'\n\s*<div class="footer"', page)

    if not (deck_match and logger_match and settings_match and advanced_match and footer_match):
        return page

    deck_pos = deck_match.start()
    logger_pos = logger_match.start()
    settings_pos = settings_match.start()
    advanced_pos = advanced_match.start()
    footer_pos = footer_match.start()

    if not (deck_pos < logger_pos < settings_pos < advanced_pos < footer_pos):
        return page

    before_views = page[:deck_pos]
    deck_block = page[deck_pos:logger_pos]
    logger_block = page[logger_pos:settings_pos]
    settings_block = page[settings_pos:advanced_pos]
    advanced_block = page[advanced_pos:footer_pos]
    after_views = page[footer_pos:]

    def clear_active_buttons(text):
        text = re.sub(r'id="btnDecks" class="navbtn(?: active)?"', 'id="btnDecks" class="navbtn"', text)
        text = re.sub(r'id="btnLogger" class="navbtn(?: active)?"', 'id="btnLogger" class="navbtn"', text)
        text = re.sub(r'id="btnSettings" class="navbtn(?: active)?"', 'id="btnSettings" class="navbtn"', text)
        text = re.sub(r'id="btnAdvanced" class="navbtn advanced-btn(?: active)?"', 'id="btnAdvanced" class="navbtn advanced-btn"', text)
        return text

    def set_button_active(text, button_id):
        text = clear_active_buttons(text)

        if button_id == "btnAdvanced":
            return text.replace(
                'id="btnAdvanced" class="navbtn advanced-btn"',
                'id="btnAdvanced" class="navbtn advanced-btn active"',
                1
            )

        return text.replace(
            f'id="{button_id}" class="navbtn"',
            f'id="{button_id}" class="navbtn active"',
            1
        )

    def set_view_active(block):
        return re.sub(
            r'<div id="(viewDecks|viewLogger|viewSettings|viewAdvanced)" class="view(?: active)?"',
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

    if initial_view == "advanced":
        before_views = set_button_active(before_views, "btnAdvanced")
        return before_views + set_view_active(advanced_block) + after_views

    before_views = set_button_active(before_views, "btnDecks")
    return before_views + set_view_active(deck_block) + after_views



def build_page(
    saved=False,
    initial_view="hyperdeck",
    tc_failed="",
    action_result="",
    action_failed="",
):
    cfg = load_config()
    advanced_nvm = load_advanced_nvm()

    if initial_view == "hyperdeck":
        resume_status_pollers()
    else:
        stop_all_status_pollers(pause_seconds=0.4)

    panel_ip = html.escape(cfg.get("panel_ip", "10.0.0.8"))
    atem_logger_ip = html.escape(cfg.get("atem_logger_ip", "10.0.0.9"))
    notice = ""
    if tc_failed:
        notice = (
            '<div class="notice notice-error">'
            f'Změnu TC IN se nepodařilo potvrdit: {html.escape(tc_failed)}.'
            '</div>'
        )
    elif saved:
        notice = '<div class="notice">Nastavení uloženo.</div>'

    action_notice = ""
    if action_result:
        notice_class = "notice notice-error" if action_failed else "notice"
        message = html.escape(action_result)
        if action_failed:
            message += f" – nepotvrzeno: {html.escape(action_failed)}"
        action_notice = f'<div class="{notice_class}">{message}</div>'

    tc_compare_yes_selected = "selected" if cfg.get("tc_compare", True) else ""
    tc_compare_no_selected = "" if cfg.get("tc_compare", True) else "selected"
    format_compare_yes_selected = "selected" if cfg.get("format_compare", True) else ""
    format_compare_no_selected = "" if cfg.get("format_compare", True) else "selected"
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
    if initial_view in ("settings", "advanced"):
        page = re.sub(
            r"<body([^>]*)>",
            add_initial_settings_class,
            page,
            count=1
        )
    elif initial_view == "hyperdeck":
        page = re.sub(
            r"<body([^>]*)>",
            add_hyperdeck_page_class,
            page,
            count=1
        )
    # initial settings body class end
    page = page.replace("__NOTICE__", notice)
    page = page.replace("__ALARM__", make_alarm(cfg, show_active_recorders=(initial_view == "settings")))
    page = page.replace("__ACTION_NOTICE__", action_notice)
    page = page.replace("__ACTIVE_COUNT__", "")
    if initial_view == "hyperdeck":
        page = page.replace("__ROWS__", make_rows(cfg))
    else:
        page = page.replace("__ROWS__", "")
    page = page.replace("__SETTINGS_ROWS__", make_settings_rows(cfg, load_tc_inputs=(initial_view == "settings")))
    page = page.replace("__PANEL_IP__", panel_ip)
    page = page.replace("__ATEM_LOGGER_IP__", atem_logger_ip)
    page = page.replace("__FORMAT_IP_HD_MINI__", html.escape(advanced_nvm.get("format_ip_hd_mini", ""), quote=True))
    page = page.replace("__FORMAT_IP_4K_PRO__", html.escape(advanced_nvm.get("format_ip_4k_pro", ""), quote=True))
    page = page.replace(
        "__FORMAT_DECK_HD_MINI__",
        make_format_device_deck_badge(cfg, advanced_nvm.get("format_ip_hd_mini", "")),
    )
    page = page.replace(
        "__FORMAT_DECK_4K_PRO__",
        make_format_device_deck_badge(cfg, advanced_nvm.get("format_ip_4k_pro", "")),
    )
    page = page.replace("__NETWORK_INTERFACES__", make_network_interfaces_html())
    page = page.replace("__TC_REF_GROUP_SELECTED__", tc_ref_group_selected)
    page = page.replace("__TC_REF_CONTROL_ROOM_SELECTED__", tc_ref_control_room_selected)
    page = page.replace("__TC_COMPARE_YES_SELECTED__", tc_compare_yes_selected)
    page = page.replace("__TC_COMPARE_NO_SELECTED__", tc_compare_no_selected)
    page = page.replace("__FORMAT_COMPARE_YES_SELECTED__", format_compare_yes_selected)
    page = page.replace("__FORMAT_COMPARE_NO_SELECTED__", format_compare_no_selected)
    page = page.replace("__TC_TOLERANCE_FRAMES__", tc_tolerance_frames)
    page = page.replace("__STATUS_POLL_ENABLED__", status_poll_enabled)
    page = page.replace("__CSRF_TOKEN__", html.escape(CSRF_TOKEN, quote=True))

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

        format_status_paths = {
            "/api/format-hd-mini-status": "format_ip_hd_mini",
            "/api/format-4k-pro-status": "format_ip_4k_pro"
        }

        if parsed.path in format_status_paths:
            payload = json.dumps(
                build_format_slots_status_payload(format_status_paths[parsed.path]),
                ensure_ascii=False
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        query = parse_qs(parsed.query)
        saved = query.get("saved", [""])[0] == "1"
        tc_failed = query.get("tc_failed", [""])[0]
        action_result = query.get("action_result", [""])[0]
        action_failed = query.get("action_failed", [""])[0]

        initial_view = "settings" if (parsed.path == "/settings" or "view=settings" in parsed.query) else ("advanced" if (parsed.path == "/advanced" or "view=advanced" in parsed.query) else ("logger" if (parsed.path == "/atem" or "view=logger" in parsed.query) else "hyperdeck"))


        page = build_page(
            saved=saved,
            initial_view=initial_view,
            tc_failed=tc_failed,
            action_result=action_result,
            action_failed=action_failed,
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if self.path in ("/toggle-arm", "/save-advanced", "/save-settings"):
            with CONFIG_LOCK:
                return self.handle_POST()

        return self.handle_POST()

    def handle_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)

        submitted_token = self.headers.get("X-CSRF-Token", "")

        if not submitted_token:
            submitted_token = form.get("csrf_token", [""])[0]

        if not secrets.compare_digest(submitted_token, CSRF_TOKEN):
            if self.path.startswith("/api/"):
                payload = json.dumps({
                    "ok": False,
                    "message": "Neplatný nebo chybějící bezpečnostní token. Obnovte stránku."
                }, ensure_ascii=False).encode("utf-8")

                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(403, "Invalid CSRF token")

            return

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

        format_action_paths = {
            "/api/format-hd-mini": ("format_ip_hd_mini", "HD Mini"),
            "/api/format-4k-pro": ("format_ip_4k_pro", "4K Pro")
        }

        if self.path in format_action_paths:
            nvm_key, device_label = format_action_paths[self.path]
            format_lock = FORMAT_LOCKS[nvm_key]
            requested_slots = form.get("slot", [])
            slot_ids = [slot_id for slot_id in (1, 2) if str(slot_id) in requested_slots]

            if not slot_ids or any(slot not in ("1", "2") for slot in requested_slots):
                payload = json.dumps({
                    "ok": False,
                    "message": "Vyberte alespoň jedno médium k formátování.",
                    "results": [],
                }, ensure_ascii=False).encode("utf-8")

                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if not format_lock.acquire(blocking=False):
                payload = json.dumps({
                    "ok": False,
                    "message": f"Formátování {device_label} už probíhá.",
                    "response": "format already in progress"
                }, ensure_ascii=False).encode("utf-8")

                self.send_response(409)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            try:
                advanced_nvm = load_advanced_nvm()
                ip = advanced_nvm.get(nvm_key, "")
                ok, results = format_hyperdeck_slots(
                    ip,
                    slot_ids,
                    filesystem="exFAT",
                    progress_key=nvm_key,
                )
            finally:
                with FORMAT_PROGRESS_LOCK:
                    FORMAT_PROGRESS[nvm_key] = {"active": False}
                format_lock.release()

            formatted_slots = ", ".join(str(slot_id) for slot_id in slot_ids)

            if ok:
                message = f"Formátování {device_label}, slot {formatted_slots}, hotovo."
            else:
                failed = results[-1] if results else {"slot": "?", "response": "neznámá chyba"}
                message = (
                    f"Formátování {device_label}, slot {failed['slot']}, selhalo: "
                    f"{failed['response']}\nNení karta zamčená?"
                )

            payload = json.dumps({
                "ok": ok,
                "message": message,
                "results": results,
            }, ensure_ascii=False).encode("utf-8")

            self.send_response(200 if ok else 500)
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
            results = []
            action_label = ""

            if cmd == "rec_all":
                action_label = "REC ALL"
                results = send_hyperdeck_commands_parallel(
                    get_armed_decks(cfg),
                    "rec",
                )

            elif cmd == "stop_all":
                action_label = "STOP ALL"
                results = send_hyperdeck_commands_parallel(
                    get_armed_decks(cfg),
                    "stop",
                )

            elif cmd in ("rec", "stop"):
                try:
                    deck_index = int(form.get("deck", ["-1"])[0])

                    if 0 <= deck_index < len(cfg["decks"]):
                        deck = cfg["decks"][deck_index]
                        ok, response = send_hyperdeck_command(deck, cmd)
                        action_label = cmd.upper()
                        results = [{
                            "index": deck_index,
                            "name": deck.get("name", f"REC {deck_index + 1}"),
                            "ip": deck.get("ip", ""),
                            "ok": ok,
                            "response": response,
                        }]

                except ValueError:
                    pass

            redirect_query = {}
            if results:
                successful = sum(1 for result in results if result.get("ok"))
                total = len(results)
                failed_names = [
                    result.get("name", "")
                    for result in results
                    if not result.get("ok")
                ]
                if failed_names:
                    redirect_query["action_result"] = (
                        f"{action_label} potvrzeno: {successful}/{total}"
                    )
                    redirect_query["action_failed"] = ", ".join(failed_names)

            self.send_response(303)
            location = "/"
            if redirect_query:
                location += "?" + urlencode(redirect_query)
            self.send_header("Location", location)
            self.end_headers()
            return

        if self.path == "/save-advanced":
            cfg = load_config()
            advanced_nvm = load_advanced_nvm()

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

            save_config(cfg)

            format_ip_hd_mini = form.get(
                "format_ip_hd_mini",
                [advanced_nvm.get("format_ip_hd_mini", "")]
            )[0].strip()
            format_ip_4k_pro = form.get(
                "format_ip_4k_pro",
                [advanced_nvm.get("format_ip_4k_pro", "")]
            )[0].strip()

            advanced_nvm["format_ip_hd_mini"] = normalize_ipv4(
                format_ip_hd_mini,
                advanced_nvm.get("format_ip_hd_mini", ""),
                allow_empty=True
            )
            advanced_nvm["format_ip_4k_pro"] = normalize_ipv4(
                format_ip_4k_pro,
                advanced_nvm.get("format_ip_4k_pro", ""),
                allow_empty=True
            )

            save_advanced_nvm(advanced_nvm)

            self.send_response(303)
            self.send_header("Location", "/advanced?saved=1")
            self.end_headers()
            return

        if self.path == "/save-settings":
            cfg = load_config()
            advanced_nvm = load_advanced_nvm()

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

            tc_reference_mode = form.get(
                "tc_reference_mode",
                [cfg.get("tc_reference_mode", "group")]
            )[0].strip()

            cfg["tc_reference_mode"] = normalize_tc_reference_mode(tc_reference_mode)

            tc_compare_value = form.get("tc_compare", ["yes"])[0].strip().lower()
            cfg["tc_compare"] = tc_compare_value not in ("no", "0", "false", "off")

            format_compare_value = form.get("format_compare", ["yes"])[0].strip().lower()
            cfg["format_compare"] = format_compare_value not in ("no", "0", "false", "off")

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

            advanced_nvm["deck_video_formats"] = [
                normalize_video_format_choice(
                    form.get(f"deck_video_format_{i}", ["1080p50"])[0]
                )
                for i in range(len(new_decks))
            ]

            stop_all_status_pollers(pause_seconds=0.4)
            tc_failed = []
            for i, deck in enumerate(cfg.get("decks", [])):
                tc_input = form.get(f"deck_tc_input_{i}", [""])[0].strip().lower()
                tc_original = form.get(f"deck_tc_original_{i}", ["---"])[0].strip().lower()
                if (
                    deck_is_armed_at_index(cfg, i, deck)
                    and tc_input in TC_INPUT_VALUES
                    and tc_input != tc_original
                ):
                    ok, _message = set_hyperdeck_timecode_input(deck, tc_input)
                    if not ok:
                        tc_failed.append(deck.get("name", f"REC {i + 1}"))

            save_config(cfg)
            save_advanced_nvm(advanced_nvm)

            self.send_response(303)
            redirect_query = {"saved": "1"}
            if tc_failed:
                redirect_query["tc_failed"] = ", ".join(tc_failed)
            self.send_header("Location", "/settings?" + urlencode(redirect_query))
            self.end_headers()
            return

        self.send_error(404)

    def log_message(self, format, *args):
        pass


class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main():
    server = PanelHTTPServer((HOST, PORT), Handler)
    print(f"HyperDeck Panel bezi na http://10.0.0.8:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
