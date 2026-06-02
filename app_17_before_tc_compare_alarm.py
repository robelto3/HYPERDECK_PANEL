from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import json
import html
import time
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8080
CONFIG_FILE = Path("config.json")

SIM_STATES = {}

DEFAULT_CONFIG = {
    "panel_ip": "10.0.0.8",
    "atem_logger_ip": "10.0.0.9",
    "simulation": True,
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


def load_config():
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()

    cfg.setdefault("panel_ip", "10.0.0.8")
    cfg.setdefault("atem_logger_ip", "10.0.0.9")
    cfg.setdefault("simulation", True)
    cfg.setdefault("decks", DEFAULT_CONFIG["decks"])

    return cfg


def save_config(cfg):
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def frames_to_tc(total_frames, fps=25):
    total_frames = total_frames % (24 * 60 * 60 * fps)

    hours = total_frames // (60 * 60 * fps)
    total_frames %= 60 * 60 * fps

    minutes = total_frames // (60 * fps)
    total_frames %= 60 * fps

    seconds = total_frames // fps
    frames = total_frames % fps

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def frames_to_tcx2(total_frames, fps=25):
    total_frames = total_frames % (24 * 60 * 60 * fps)

    hours = total_frames // (60 * 60 * fps)
    total_frames %= 60 * 60 * fps

    minutes = total_frames // (60 * fps)
    total_frames %= 60 * fps

    seconds = total_frames // fps
    frames_25 = total_frames % fps
    frames_x2 = frames_25 * 2

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames_x2:02d}"


def is_pgm_deck(name):
    return "PGM" in name.upper()


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


def make_alarm(cfg):
    if not cfg.get("simulation", False):
        return '<div id="tcAlarm" class="alarm">TC MISMATCH WARNING</div>'

    init_sim_states(len(cfg["decks"]))

    bad = []
    for i, deck in enumerate(cfg["decks"]):
        state = SIM_STATES.get(i, {})
        if state.get("status") == "TC WARNING":
            bad.append(deck.get("name", f"REC {i + 1}"))

    if bad:
        names = ", ".join(html.escape(x) for x in bad)
        return f'<div id="tcAlarm" class="alarm" style="display:block;">SIMULATION MODE – TC WARNING: {names}</div>'

    return '<div id="tcAlarm" class="alarm simulation-ok" style="display:block;">SIMULATION MODE – REC/STOP tlačítka fungují lokálně</div>'


def make_rows(cfg):
    rows = ""
    simulation = cfg.get("simulation", False)

    if simulation:
        init_sim_states(len(cfg["decks"]))

    for i, deck in enumerate(cfg["decks"]):
        name = html.escape(deck.get("name", ""))
        ip = html.escape(deck.get("ip", ""))
        row_class = "pgm-row" if is_pgm_deck(name) else ""

        if simulation:
            status_text, led_class, tc, tc_valid, total_frames = get_sim_state(i)
        else:
            status_text, led_class, tc, tc_valid, total_frames = ("STOPPED", "stopped", "--:--:--:--", False, 0)

        if tc_valid:
            tc_cell = f'<td class="tc live-tc" data-base-frame="{total_frames}">{tc}</td>'
        else:
            tc_cell = f'<td class="tc">{tc}</td>'

        rows += f"""
        <tr class="{row_class}">
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
            <td class="status"><span class="led {led_class}"></span> {status_text}</td>
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
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            background: #09090d;
            color: #eeeeee;
            font-family: Arial, sans-serif;
            overflow: hidden;
        }

        .wrap {
            width: 100vw;
            height: 100vh;
            padding: 12px 18px 10px 18px;
        }

        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 10px;
        }

        h1 {
            margin: 0;
            font-size: 30px;
            letter-spacing: 2px;
            white-space: nowrap;
        }

        .topnav {
            display: flex;
            justify-content: center;
            gap: 12px;
        }

        .navbtn {
            border: 1px solid #444;
            border-radius: 10px;
            padding: 10px 22px;
            background: #242432;
            color: #eee;
            font-size: 18px;
            font-weight: bold;
            cursor: pointer;
        }

        .navbtn.active {
            background: #1b6cff;
            border-color: #7dabff;
            box-shadow: 0 0 14px #1b6cff;
        }

        .notice {
            margin-bottom: 10px;
            padding: 10px;
            border-radius: 10px;
            background: #113b20;
            color: #78ff9c;
            text-align: center;
            font-size: 22px;
            font-weight: bold;
            box-shadow: 0 0 14px #008d35;
        }

        .alarm {
            display: none;
            margin-bottom: 10px;
            padding: 10px;
            border-radius: 10px;
            background: #7a0000;
            color: #fff;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            box-shadow: 0 0 18px #ff3333;
        }

        .simulation-ok {
            background: #113b20;
            color: #78ff9c;
            box-shadow: 0 0 14px #008d35;
        }

        .view {
            display: none;
        }

        .view.active {
            display: block;
        }

        .panel-card {
            background: #15151d;
            border: 1px solid #33333d;
            border-radius: 12px;
            overflow: hidden;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th, td {
            padding: 8px 10px;
            border-bottom: 1px solid #33333d;
            text-align: center;
            font-size: 19px;
        }

        tr:last-child td {
            border-bottom: none;
        }

        th {
            color: #a8a8b5;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            background: #1d1d28;
        }

        .name {
            font-weight: bold;
            text-align: left;
            font-size: 23px;
            width: 190px;
            white-space: nowrap;
        }

        .ip {
            color: #b8b8c0;
            font-size: 16px;
            width: 150px;
        }

        .tc {
            font-family: monospace;
            font-size: 29px;
            letter-spacing: 1px;
            width: 240px;
        }

        .status {
            font-weight: bold;
            min-width: 170px;
            text-align: left;
            padding-left: 75px;
            white-space: nowrap;
        }

        .led {
            display: inline-block;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            margin-right: 10px;
            vertical-align: -3px;
            background: #777;
            box-shadow: 0 0 10px #777;
        }

        .stopped {
            background: #777;
            box-shadow: 0 0 10px #777;
        }

        .recording {
            background: #22ff66;
            box-shadow: 0 0 18px #22ff66;
        }

        .offline {
            background: #ff3333;
            box-shadow: 0 0 18px #ff3333;
        }

        .warning {
            background: #ffcc33;
            box-shadow: 0 0 18px #ffcc33;
        }

        .inline-form {
            margin: 0;
            display: inline;
        }

        .btn {
            border: none;
            border-radius: 9px;
            padding: 10px 22px;
            font-size: 19px;
            font-weight: bold;
            color: #fff;
            cursor: pointer;
            min-width: 92px;
        }

        .rec {
            background: #008d35;
        }

        .rec:hover {
            background: #00aa44;
        }

        .stop {
            background: #8d1d1d;
        }

        .stop:hover {
            background: #aa2424;
        }

        .pgm-row td {
            background: #202018;
            border-top: 3px solid #777746;
        }

        .pgm-row .name::after {
            content: "";
        }

        .bottom {
            display: flex;
            justify-content: center;
            gap: 34px;
            margin-top: 18px;
        }

        .big {
            width: 270px;
            height: 66px;
            font-size: 28px;
            border-radius: 14px;
        }

        .logger-frame {
            width: 100%;
            height: calc(100vh - 116px);
            border: 1px solid #444;
            border-radius: 12px;
            background: #000;
        }

        .logger-note {
            text-align: center;
            color: #aaa;
            margin-bottom: 8px;
            font-size: 15px;
        }

        .settings-note {
            margin-top: 14px;
            text-align: center;
            color: #aaa;
            font-size: 18px;
        }

        .settings-form {
            max-width: 760px;
            margin: 0 auto;
        }

        .settings-form table {
            margin-top: 10px;
        }

        .text-input {
            width: 100%;
            background: #0f0f15;
            color: #eeeeee;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 22px;
        }

        .name-input {
            font-weight: bold;
        }

        .ip-input {
            font-family: monospace;
            text-align: center;
        }

        .save-row {
            display: flex;
            justify-content: center;
            gap: 24px;
            margin-top: 20px;
        }

        .save-btn {
            background: #1b6cff;
            width: 260px;
            height: 62px;
            font-size: 26px;
            border-radius: 14px;
        }

        .small-label {
            color: #aaa;
            font-size: 15px;
            margin-bottom: 6px;
            text-align: center;
        }

        .footer {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 4px;
            text-align: center;
            color: #777;
            font-size: 13px;
            pointer-events: none;
        }

        @media (max-height: 760px) {
            h1 {
                font-size: 26px;
            }

            th, td {
                padding: 6px 8px;
                font-size: 17px;
            }

            .tc {
                font-size: 25px;
            }

            .name {
                font-size: 20px;
            }

            .btn {
                padding: 8px 18px;
                font-size: 17px;
            }

            .big {
                height: 58px;
                font-size: 25px;
            }
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
            <div class="logger-note">
                ATEM logger: http://__ATEM_LOGGER_IP__
            </div>
            <iframe class="logger-frame" src="http://__ATEM_LOGGER_IP__"></iframe>
        </div>

        <div id="viewSettings" class="view">
            <form class="settings-form" method="POST" action="/save-settings">
                <div class="small-label">ATEM LOGGER IP</div>
                <input class="text-input ip-input" type="text" name="atem_logger_ip" value="__ATEM_LOGGER_IP__">

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
                    IP adresa RPi panelu je pevně nastavena v systému: __PANEL_IP__
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


def build_page(saved=False):
    cfg = load_config()

    panel_ip = html.escape(cfg.get("panel_ip", "10.0.0.8"))
    atem_logger_ip = html.escape(cfg.get("atem_logger_ip", "10.0.0.9"))

    notice = ""
    if saved:
        notice = '<div class="notice">Nastavení uloženo.</div>'

    page = HTML_TEMPLATE
    page = page.replace("__NOTICE__", notice)
    page = page.replace("__ALARM__", make_alarm(cfg))
    page = page.replace("__ROWS__", make_rows(cfg))
    page = page.replace("__SETTINGS_ROWS__", make_settings_rows(cfg))
    page = page.replace("__PANEL_IP__", panel_ip)
    page = page.replace("__ATEM_LOGGER_IP__", atem_logger_ip)

    return page


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
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

        if self.path == "/action":
            cfg = load_config()

            if cfg.get("simulation", False):
                init_sim_states(len(cfg["decks"]))

                cmd = form.get("cmd", [""])[0]

                if cmd == "rec_all":
                    for i in range(len(cfg["decks"])):
                        set_sim_deck_status(i, "rec")

                elif cmd == "stop_all":
                    for i in range(len(cfg["decks"])):
                        set_sim_deck_status(i, "stop")

                elif cmd in ("rec", "stop"):
                    try:
                        deck_index = int(form.get("deck", ["-1"])[0])
                        set_sim_deck_status(deck_index, cmd)
                    except ValueError:
                        pass

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path == "/save-settings":
            cfg = load_config()
            cfg["atem_logger_ip"] = form.get(
                "atem_logger_ip",
                [cfg.get("atem_logger_ip", "10.0.0.9")]
            )[0].strip()

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

                new_decks.append({
                    "name": name,
                    "ip": ip
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
