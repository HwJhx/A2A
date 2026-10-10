"""Raw-TTY fake pi used only by the opt-in Herdr integration test."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path


def report(session: str, pane_id: str, state: str, sequence: int) -> None:
    subprocess.run([
        "herdr", "--session", session, "pane", "report-agent",
        "--source", "a2a_codex_fake_pi", "--agent", "pi", "--state", state,
        "--message", "test-only fake pi", "--seq", str(sequence), pane_id,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    timeout=8)


def log_event(path: str, event: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--return-idle-after-ms", type=int)
    parser.add_argument("--hold-without-working", action="store_true")
    parser.add_argument("--barrier-dir")
    parser.add_argument("--barrier-key")
    parser.add_argument("--barrier-peer-key")
    parser.add_argument("--barrier-timeout-ms", type=int, default=10000)
    args = parser.parse_args()
    if bool(args.barrier_dir) != bool(args.barrier_key and args.barrier_peer_key):
        parser.error("barrier-dir、barrier-key 和 barrier-peer-key 必须一起提供")

    pane_id = os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        raise RuntimeError("Herdr did not inject HERDR_PANE_ID into the test pane")
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        raise RuntimeError("fake pi requires a TTY stdin")
    original = termios.tcgetattr(fd)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)

    try:
        tty.setraw(fd)
        sequence = 1
        report(args.session, pane_id, "idle", sequence)
        log_event(args.log, {"event": "state", "state": "idle", "sequence": sequence})
        Path(args.ready).write_text("idle\n", encoding="utf-8")

        pending = bytearray()
        while not stopping:
            endings = [offset for offset in (pending.find(b"\r"), pending.find(b"\n"))
                       if offset >= 0]
            if not endings:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                pending.extend(chunk)
                continue

            end = min(endings)
            raw_prompt = bytes(pending[:end])
            del pending[:end + 1]
            while pending[:1] in (b"\r", b"\n"):
                del pending[:1]
            raw_prompt = raw_prompt.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
            prompt = raw_prompt.decode("utf-8", errors="replace")
            if not prompt:
                continue
            log_event(args.log, {"event": "prompt", "prompt": prompt})
            if args.hold_without_working:
                log_event(args.log, {"event": "state", "state": "working_suppressed"})
                while not stopping:
                    time.sleep(0.1)
                break
            if args.barrier_dir:
                barrier = Path(args.barrier_dir)
                barrier.mkdir(parents=True, exist_ok=True)
                arrived = barrier / (args.barrier_key + ".arrived")
                peer_arrived = barrier / (args.barrier_peer_key + ".arrived")
                arrived.write_text("arrived\n", encoding="utf-8")
                log_event(args.log, {"event": "barrier", "state": "arrived", "key": args.barrier_key})
                deadline = time.monotonic() + max(0, args.barrier_timeout_ms) / 1000.0
                while not peer_arrived.exists() and time.monotonic() < deadline and not stopping:
                    time.sleep(0.025)
                if not peer_arrived.exists():
                    log_event(args.log, {
                        "event": "barrier", "state": "timeout", "key": args.barrier_key,
                        "peer": args.barrier_peer_key,
                    })
                    return 2
                log_event(args.log, {"event": "barrier", "state": "released", "key": args.barrier_key})
            sequence += 1
            report(args.session, pane_id, "working", sequence)
            log_event(args.log, {"event": "state", "state": "working", "sequence": sequence})
            if args.return_idle_after_ms is None:
                while not stopping:
                    time.sleep(0.1)
                break

            time.sleep(max(0, args.return_idle_after_ms) / 1000.0)
            if stopping:
                break
            sequence += 1
            report(args.session, pane_id, "idle", sequence)
            log_event(args.log, {"event": "state", "state": "idle", "sequence": sequence})
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


if __name__ == "__main__":
    raise SystemExit(main())
