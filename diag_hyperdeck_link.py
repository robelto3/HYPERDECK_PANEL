#!/usr/bin/env python3
import datetime
import re
import socket
import subprocess
import time

IP = "10.0.0.23"
PORT = 9993
ROUNDS = 180
INTERVAL = 1.0

CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 1.0


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def short_text(text, max_len=80):
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def system_ping():
    start = time.monotonic()

    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", IP],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as e:
        return False, 0.0, f"PING EXC {e}"

    elapsed_ms = (time.monotonic() - start) * 1000.0

    if r.returncode != 0:
        return False, elapsed_ms, short_text(r.stderr or r.stdout)

    m = re.search(r"time[=<]([0-9.]+)\s*ms", r.stdout)
    if m:
        elapsed_ms = float(m.group(1))

    return True, elapsed_ms, "OK"


def recv_msg(sock, timeout=1.0):
    sock.settimeout(timeout)
    data = b""
    start = time.monotonic()

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

    elapsed_ms = (time.monotonic() - start) * 1000.0
    return data.decode("utf-8", errors="replace"), elapsed_ms


def parse_field(text, key):
    prefix = key.lower() + ":"

    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()

    return ""


def hyperdeck_test_once():
    result = {
        "tcp_ok": False,
        "hello_ok": False,
        "proto_ping_ok": False,
        "transport_ok": False,
        "tcp_ms": 0.0,
        "hello_ms": 0.0,
        "proto_ping_ms": 0.0,
        "transport_ms": 0.0,
        "status": "",
        "input": "",
        "tc": "",
        "error": "",
    }

    try:
        start = time.monotonic()

        with socket.create_connection((IP, PORT), timeout=CONNECT_TIMEOUT) as sock:
            result["tcp_ms"] = (time.monotonic() - start) * 1000.0
            result["tcp_ok"] = True

            hello, hello_ms = recv_msg(sock, timeout=READ_TIMEOUT)
            result["hello_ms"] = hello_ms
            result["hello_ok"] = hello.startswith("500 connection info")

            if not result["hello_ok"]:
                result["error"] = "BAD_HELLO " + short_text(hello)
                return result

            sock.sendall(b"ping\r\n")
            proto_ping, proto_ping_ms = recv_msg(sock, timeout=READ_TIMEOUT)
            result["proto_ping_ms"] = proto_ping_ms
            result["proto_ping_ok"] = "200 ok" in proto_ping.lower()

            if not result["proto_ping_ok"]:
                result["error"] = "BAD_PROTO_PING " + short_text(proto_ping)
                return result

            sock.sendall(b"transport info\r\n")
            transport, transport_ms = recv_msg(sock, timeout=READ_TIMEOUT)
            result["transport_ms"] = transport_ms
            result["transport_ok"] = transport.startswith("208 transport info")

            if not result["transport_ok"]:
                result["error"] = "BAD_TRANSPORT " + short_text(transport)
                return result

            result["status"] = parse_field(transport, "status")
            result["input"] = parse_field(transport, "input video format")
            result["tc"] = parse_field(transport, "display timecode") or parse_field(transport, "timecode")

    except Exception as e:
        result["error"] = type(e).__name__ + ": " + str(e)

    return result


def main():
    print(f"Diagnostika HyperDeck {IP}:{PORT}")
    print(f"Kol: {ROUNDS}, interval: {INTERVAL}s")
    print("Panel nech klidně běžet. Ukončení: Ctrl+C")
    print()

    fail_count = 0
    last_state = None

    try:
        for i in range(1, ROUNDS + 1):
            ping_ok, ping_ms, ping_msg = system_ping()
            h = hyperdeck_test_once()

            ok = (
                ping_ok
                and h["tcp_ok"]
                and h["hello_ok"]
                and h["proto_ping_ok"]
                and h["transport_ok"]
            )

            state = (
                ok,
                h["status"],
                h["input"],
                h["tc"],
                h["error"],
            )

            if not ok:
                fail_count += 1

            should_print = (
                i == 1
                or not ok
                or state != last_state
                or i % 10 == 0
            )

            if should_print:
                if ok:
                    print(
                        f"{now()} #{i:03d} OK  "
                        f"ping={ping_ms:.1f}ms tcp={h['tcp_ms']:.1f}ms "
                        f"hello={h['hello_ms']:.1f}ms proto={h['proto_ping_ms']:.1f}ms "
                        f"transport={h['transport_ms']:.1f}ms  "
                        f"status={h['status']} input={h['input']} tc={h['tc']}"
                    )
                else:
                    print(
                        f"{now()} #{i:03d} FAIL "
                        f"ping={'OK' if ping_ok else 'FAIL'}({ping_ms:.1f}ms) "
                        f"tcp={'OK' if h['tcp_ok'] else 'FAIL'}({h['tcp_ms']:.1f}ms) "
                        f"hello={'OK' if h['hello_ok'] else 'FAIL'}({h['hello_ms']:.1f}ms) "
                        f"proto_ping={'OK' if h['proto_ping_ok'] else 'FAIL'}({h['proto_ping_ms']:.1f}ms) "
                        f"transport={'OK' if h['transport_ok'] else 'FAIL'}({h['transport_ms']:.1f}ms) "
                        f"ERR={h['error'] or ping_msg}"
                    )

            last_state = state
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print()
        print("Přerušeno ručně.")

    print()
    print(f"Hotovo. Chybné cykly: {fail_count}/{ROUNDS}")


if __name__ == "__main__":
    main()
