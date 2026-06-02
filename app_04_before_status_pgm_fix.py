from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = 8080

DECKS = [
    ("REC 1", "10.0.0.11", False),
    ("REC 2", "10.0.0.12", False),
    ("REC 3", "10.0.0.13", False),
    ("REC 4", "10.0.0.14", False),
    ("REC 5", "10.0.0.15", False),
    ("REC 6", "10.0.0.16", False),
    ("REC 7", "10.0.0.21", False),
    ("REC 8", "10.0.0.22", False),
    ("PGM REC", "10.0.0.24", True),
]


def make_rows():
    rows = ""
    for name, ip, is_pgm in DECKS:
        row_class = "pgm-row" if is_pgm else ""
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


def make_settings_rows():
    rows = ""
    for name, ip, is_pgm in DECKS:
        row_class = "pgm-row" if is_pgm else ""
        rows += f"""
        <tr class="{row_class}">
            <td class="name">{name}</td>
            <td class="tc">{ip}</td>
        </tr>
        """
    return rows


HTML = """<!doctype html>
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
            width: 130px;
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
            padding-left: 20px;
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
            content: "  ATEM OUT";
            color: #d6d66a;
            font-size: 13px;
            font-weight: bold;
            margin-left: 8px;
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
                        __ROWS__
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
                ATEM logger: http://10.0.0.9
            </div>
            <iframe class="logger-frame" src="http://10.0.0.9"></iframe>
        </div>

        <div id="viewSettings" class="view">
            <div class="panel-card">
                <table>
                    <thead>
                        <tr>
                            <th>Deck</th>
                            <th>IP</th>
                        </tr>
                    </thead>
                    <tbody>
                        __SETTINGS_ROWS__
                    </tbody>
                </table>
            </div>

            <div class="settings-note">
                Nastaveni IP adres pres web dodelame v dalsim kroku.
            </div>
        </div>

        <div class="footer">
            RPi 10.0.0.8 | ATEM logger 10.0.0.9 | Tento program napsala Astra, Bobova AI asistentka.
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
    </script>
</body>
</html>
"""

HTML = HTML.replace("__ROWS__", make_rows())
HTML = HTML.replace("__SETTINGS_ROWS__", make_settings_rows())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format, *args):
        pass


server = HTTPServer((HOST, PORT), Handler)
print(f"HyperDeck Panel bezi na http://10.0.0.8:{PORT}")
server.serve_forever()
