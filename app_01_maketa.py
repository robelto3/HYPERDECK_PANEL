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
            padding: 20px;
        }

        h1 {
            margin: 0 0 18px 0;
            text-align: center;
            font-size: 38px;
            letter-spacing: 2px;
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

        .footer {
            margin-top: 18px;
            text-align: center;
            color: #888;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="wrap">
        <h1>HYPERDECK PANEL</h1>

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

        <div class="footer">
            RPi 10.0.0.8 | ATEM logger 10.0.0.9 | Tento program napsala Astra, Bobova AI asistentka.
        </div>
    </div>
</body>
</html>
""".replace("__ROWS__", make_rows())

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
