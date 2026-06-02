from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = 8080

DECKS = [
    ("REC 1", "10.0.0.11"),
    ("REC 2", "10.0.0.12"),
    ("REC 3", "10.0.0.13"),
    ("REC 4", "10.0.0.14"),
    ("REC 5", "10.0.0.15"),
    ("REC 6", "10.0.0.16"),
    ("REC 7", "10.0.0.21"),
    ("REC 8", "10.0.0.22"),
    ("PGM REC", "10.0.0.24"),
]


def make_rows():
    rows = ""
    for name, ip in DECKS:
        rows += f"""
        <tr>
            <td class="name">{name}</td>
            <td class="ip">{ip}</td>
            <td><button class="btn rec">REC</button></td>
            <td><button class="btn stop">STOP</button></td>
            <td><span class="led stopped"></span></td>
            <td class="status">STOPPED</td>
            <td class="tc">--:--:--:--</td>
        </tr>
        """
    return rows


HTML = """<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>HyperDeck Panel</title>
    <style>
        body {
            margin: 0;
            background: #101014;
            color: #eeeeee;
            font-family: Arial, sans-serif;
        }

        .wrap {
            max-width: 1180px;
            margin: 0 auto;
            padding: 16px 20px 20px 20px;
        }

        h1 {
            margin: 0 0 12px 0;
            text-align: center;
            font-size: 34px;
            letter-spacing: 2px;
        }

        .topnav {
            display: flex;
            justify-content: center;
            gap: 18px;
            margin-bottom: 16px;
        }

        .navbtn {
            border: 1px solid #444;
            border-radius: 10px;
            padding: 12px 26px;
            background: #252532;
            color: #eee;
            font-size: 20px;
            font-weight: bold;
            cursor: pointer;
        }

        .navbtn.active {
            background: #1b6cff;
            border-color: #6aa0ff;
            box-shadow: 0 0 14px #1b6cff;
        }

        .view {
            display: none;
        }

        .view.active {
            display: block;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            background: #181820;
            border: 1px solid #33333d;
        }

        th, td {
            padding: 10px 12px;
            border-bottom: 1px solid #33333d;
            text-align: center;
            font-size: 20px;
        }

        th {
            color: #aaaaaa;
            font-size: 15px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .name {
            font-weight: bold;
            text-align: left;
            font-size: 22px;
        }

        .ip {
            color: #bbbbbb;
            font-size: 16px;
        }

        .tc {
            font-family: monospace;
            font-size: 28px;
            letter-spacing: 1px;
        }

        .status {
            font-weight: bold;
            min-width: 130px;
        }

        .led {
            display: inline-block;
            width: 24px;
            height: 24px;
            border-radius: 50%;
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
            border-radius: 8px;
            padding: 12px 24px;
            font-size: 20px;
            font-weight: bold;
            color: #fff;
            cursor: pointer;
        }

        .rec {
            background: #008d35;
        }

        .stop {
            background: #8d1d1d;
        }

        .bottom {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 24px;
        }

        .big {
            width: 240px;
            height: 70px;
            font-size: 28px;
            border-radius: 12px;
        }

        .logger-frame {
            width: 100%;
            height: calc(100vh - 150px);
            border: 1px solid #444;
            border-radius: 10px;
            background: #000;
        }

        .logger-note {
            text-align: center;
            color: #aaa;
            margin-bottom: 10px;
            font-size: 16px;
        }

        .footer {
            margin-top: 16px;
            text-align: center;
            color: #888;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="wrap">
        <h1>HYPERDECK PANEL</h1>

        <div class="topnav">
            <button id="btnDecks" class="navbtn active" onclick="showView('decks')">HYPERDECK</button>
            <button id="btnLogger" class="navbtn" onclick="showView('logger')">ATEM LOGGER</button>
            <button id="btnSettings" class="navbtn" onclick="showView('settings')">SETTINGS</button>
        </div>

        <div id="viewDecks" class="view active">
            <table>
                <thead>
                    <tr>
                        <th>Deck</th>
                        <th>IP</th>
                        <th>Rec</th>
                        <th>Stop</th>
                        <th>LED</th>
                        <th>Status</th>
                        <th>TC</th>
                    </tr>
                </thead>
                <tbody>
                    __ROWS__
                </tbody>
            </table>

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

            <div class="footer">
                Nastavení IP adres přes web doděláme v dalším kroku.
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

settings_rows = ""
for name, ip in DECKS:
    settings_rows += f"""
    <tr>
        <td class="name">{name}</td>
        <td class="tc">{ip}</td>
    </tr>
    """

HTML = HTML.replace("__ROWS__", make_rows())
HTML = HTML.replace("__SETTINGS_ROWS__", settings_rows)


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
