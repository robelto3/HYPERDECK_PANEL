from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import json
import html
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8080
CONFIG_FILE = Path("config.json")


DEFAULT_CONFIG = {
    "panel_ip": "10.0.0.8",
    "atem_logger_ip": "10.0.0.9",
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
        return DEFAULT_CONFIG

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return DEFAULT_CONFIG

    cfg.setdefault("panel_ip", "10.0.0.8")
    cfg.setdefault("atem_logger_ip", "10.0.0.9")
    cfg.setdefault("decks", DEFAULT_CONFIG["decks"])
    return cfg


def save_config(cfg):
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_pgm_deck(name):
    return "PGM" in name.upper()


def make_rows(cfg):
    rows = ""
    for deck in cfg["decks"]:
        name = html.escape(deck.get("name", ""))
        ip = html.escape(deck.get("ip", ""))
        row_class = "pgm-row" if is_pgm_deck(name) else ""

        rows += f"""
        <tr class="{row_class}">
            <td class="name">{name}</td>
            <td class="ip">{ip}</td>
            <td><button class="btn rec">REC</button></td>
            <td><button class="btn stop">STOP</button></td>
            <td class="status"><span class="led stopped"></span> STOPPED</td>
            <td class="tc">--:--:--:--</td>
        </tr>
        """
    return rows


def make_settings_rows(cfg):
    rows = ""

    for i, deck in enumerate(cfg["decks"]):
        name = html.escape(deck.get("name", ""))
        ip = html.escape(deck.get("ip", ""))
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


def build_page(saved=False):
    cfg = load_config()

    panel_ip = html.escape(cfg["panel_ip"])
    atem_logger_ip = html.escape(cfg["atem_logger_ip"])
    notice = '<div class="notice">Nastavení uloženo.</div>' if saved else ""

    html_page = f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>HyperDeck Panel</title>
    <style>
        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: #09090d;
            color: #eeeeee;
            font-family: Arial, sans-serif;
            overflow: hidden;
        }}

        .wrap {{
            width: 100vw;
            height: 100vh;
            padding: 12px 18px 10px 18px;
        }}

        .header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 10px;
        }}

        h1 {{
            margin: 0;
            font-size: 30px;
            letter-spacing: 2px;
            white-space: nowrap;
        }}

        .topnav {{
            display: flex;
            justify-content: center;
            gap: 12px;
        }}

        .navbtn {{
            border: 1px solid #444;
            border-radius: 10px;
            padding: 10px 22px;
            background: #242432;
            color: #eee;
            font-size: 18px;
            font-weight: bold;
            cursor: pointer;
        }}

        .navbtn.active {{
            background: #1b6cff;
            border-color: #7dabff;
            box-shadow: 0 0 14px #1b6cff;
        }}

        .notice {{
            margin-bottom: 10px;
            padding: 10px;
            border-radius: 10px;
            background: #113b20;
            color: #78ff9c;
            text-align: center;
            font-size: 22px;
            font-weight: bold;
            box-shadow: 0 0 14px #008d35;
        }}

        .alarm {{
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
        }}

        .view {{
            display: none;
        }}

        .view.active {{
            display: block;
        }}

        .panel-card {{
            background: #15151d;
            border: 1px solid #33333d;
            border-radius: 12px;
            overflow: hidden;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        th, td {{
            padding: 8px 10px;
            border-bottom: 1px solid #33333d;
            text-align: center;
            font-size: 19px;
        }}

        tr:last-child td {{
            border-bottom: none;
        }}

        th {{
            color: #a8a8b5;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            background: #1d1d28;
        }}

        .name {{
            font-weight: bold;
            text-align: left;
            font-size: 23px;
            width: 190px;
            white-space: nowrap;
        }}

        .ip {{
            color: #b8b8c0;
            font-size: 16px;
            width: 150px;
        }}

        .tc {{
            font-family: monospace;
            font-size: 29px;
            letter-spacing: 1px;
            width: 240px;
        }}

        .status {{
            font-weight: bold;
            min-width: 170px;
            text-align: center;
            padding-left: 0;
            white-space: nowrap;
        }}

        .led {{
            display: inline-block;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            margin-right: 10px;
            vertical-align: -3px;
            background: #777;
            box-shadow: 0 0 10px #777;
        }}

        .stopped {{
            background: #777;
            box-shadow: 0 0 10px #777;
        }}

        .recording {{
            background: #22ff66;
            box-shadow: 0 0 18px #22ff66;
        }}

        .offline {{
            background: #ff3333;
            box-shadow: 0 0 18px #ff3333;
        }}

        .warning {{
            background: #ffcc33;
            box-shadow: 0 0 18px #ffcc33;
        }}

        .btn {{
            border: none;
            border-radius: 9px;
            padding: 10px 22px;
            font-size: 19px;
            font-weight: bold;
            color: #fff;
            cursor: pointer;
            min-width: 92px;
        }}

        .rec {{
            background: #008d35;
        }}

        .rec:hover {{
            background: #00aa44;
        }}

        .stop {{
            background: #8d1d1d;
        }}

        .stop:hover {{
            background: #aa2424;
        }}

        .pgm-row td {{
            background: #202018;
            border-top: 3px solid #777746;
        }}

        .pgm-row .name::after {{
            content: "";
        }}

        .bottom {{
            display: flex;
            justify-content: center;
            gap: 34px;
            margin-top: 18px;
        }}

        .big {{
            width: 270px;
            height: 66px;
            font-size: 28px;
            border-radius: 14px;
        }}

        .logger-frame {{
            width: 100%;
            height: calc(100vh - 116px);
            border: 1px solid #444;
            border-radius: 12px;
            background: #000;
        }}

        .logger-note {{
            text-align: center;
            color: #aaa;
            margin-bottom: 8px;
            font-size: 15px;
        }}

        .settings-note {{
            margin-top: 14px;
            text-align: center;
            color: #aaa;
            font-size: 18px;
        }}

        .settings-form {{
            max-width: 760px;
            margin: 0 auto;
        }}

        .settings-form table {{
            margin-top: 10px;
        }}

        .text-input {{
            width: 100%;
            background: #0f0f15;
            color: #eeeeee;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 22px;
        }}

        .name-input {{
            font-weight: bold;
        }}

        .ip-input {{
            font-family: monospace;
            text-align: center;
        }}

        .save-row {{
            display: flex;
            justify-content: center;
            gap: 24px;
            margin-top: 20px;
        }}

        .save-btn {{
            background: #1b6cff;
            width: 260px;
            height: 62px;
            font-size: 26px;
            border-radius: 14px;
        }}

        .small-label {{
            color: #aaa;
            font-size: 15px;
            margin-bottom: 6px;
            text-align: center;
        }}

        .footer {{
            position: fixed;
            left: 0;
            right: 0;
            bottom: 4px;
            text-align: center;
            color: #777;
            font-size: 13px;
            pointer-events: none;
        }}

        @media (max-height: 760px) {{
            h1 {{
                font-size: 26px;
            }}

            th, td {{
                padding: 6px 8px;
                font-size: 17px;
            }}

            .tc {{
                font-size: 25px;
            }}

            .name {{
                font-size: 20px;
            }}

            .btn {{
                padding: 8px 18px;
                font-size: 17px;
            }}

            .big {{
                height: 58px;
                font-size: 25px;
            }}
        }}
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

        {notice}

        <div id="tcAlarm" class="alarm">
            TC MISMATCH WARNING
        </div>

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
                        {make_rows(cfg)}
                    </tbody>
                </table>
            </div>

            <div class="bottom">
                <button class="btn rec big">REC ALL</button>
                <button class="btn stop big">STOP ALL</button>
            </div>
        </div>

        <div id="viewLogger" class="view">
            <div class="logger-note">
                ATEM logger: http://{atem_logger_ip}
            </div>
            <iframe class="logger-frame" src="http://{atem_logger_ip}"></iframe>
        </div>

        <div id="viewSettings" class="view">
            <form class="settings-form" method="POST" action="/save-settings">
                <div class="small-label">ATEM LOGGER IP</div>
                <input class="text-input ip-input" type="text" name="atem_logger_ip" value="{atem_logger_ip}">

                <div class="panel-card">
                    <table>
                        <thead>
                            <tr>
                                <th>Deck name</th>
                                <th>IP address</th>
                            </tr>
                        </thead>
                        <tbody>
                            {make_settings_rows(cfg)}
                        </tbody>
                    </table>
                </div>

                <div class="save-row">
                    <button class="btn save-btn" type="submit">ULOŽIT</button>
                </div>

                <div class="settings-note">
                    IP adresa RPi panelu je pevně nastavena v systému: {panel_ip}
                </div>
            </form>
        </div>

        <div class="footer">
            RPi {panel_ip} | ATEM logger {atem_logger_ip} | Tento program napsala Astra, Bobova AI asistentka.
        </div>
    </div>

    <script>
        function showView(name) {{
            document.getElementById("viewDecks").classList.remove("active");
            document.getElementById("viewLogger").classList.remove("active");
            document.getElementById("viewSettings").classList.remove("active");

            document.getElementById("btnDecks").classList.remove("active");
            document.getElementById("btnLogger").classList.remove("active");
            document.getElementById("btnSettings").classList.remove("active");

            if (name === "decks") {{
                document.getElementById("viewDecks").classList.add("active");
                document.getElementById("btnDecks").classList.add("active");
            }}

            if (name === "logger") {{
                document.getElementById("viewLogger").classList.add("active");
                document.getElementById("btnLogger").classList.add("active");
            }}

            if (name === "settings") {{
                document.getElementById("viewSettings").classList.add("active");
                document.getElementById("btnSettings").classList.add("active");
            }}
        }}
    </script>
</body>
</html>
"""
    return html_page


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
        if self.path != "/save-settings":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)

        cfg = load_config()
        cfg["atem_logger_ip"] = form.get("atem_logger_ip", [cfg.get("atem_logger_ip", "10.0.0.9")])[0].strip()

        new_decks = []
        for i, old_deck in enumerate(cfg["decks"]):
            name = form.get(f"deck_name_{i}", [old_deck.get("name", "")])[0].strip()
            ip = form.get(f"deck_ip_{i}", [old_deck.get("ip", "")])[0].strip()

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

    def log_message(self, format, *args):
        pass


server = HTTPServer((HOST, PORT), Handler)
print(f"HyperDeck Panel bezi na http://10.0.0.8:{PORT}")
server.serve_forever()
