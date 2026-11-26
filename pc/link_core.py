"""HM30 Link - radio link logic (no window code, so it can be tested on its own).

* LinkWorker   reads the port, confirms (ACK) and re-sends packets, filters duplicates
* send_files   sends a list of files with progress (runs in its own thread)
* DemoSerial   fake STM32 for --demo
* make_pair    two connected fake ports with a simulated radio for --pair and tests
"""

from __future__ import annotations

import itertools
import math
import os
import queue
import random
import threading
import time
from collections import deque
from dataclasses import dataclass

import protocol as proto

# Types that the other side must confirm when it is an app (an STM32 confirms them itself)
APP_ACKED = (proto.TYPE_TEXT, proto.TYPE_VALUE, proto.TYPE_BYTES, proto.TYPE_CMD,
             proto.TYPE_FILE_START, proto.TYPE_FILE_DATA, proto.TYPE_FILE_END)


# --------------------------------------------------------------------------- link


@dataclass
class Pending:
    """A packet that is waiting for its ACK."""

    seq: int
    ftype: int
    frame: bytes
    desc: str
    report: bool          # show "delivered" / "not delivered" for this packet
    group: dict | None    # shared counters, e.g. for all pieces of one file
    token: int | None     # links the packet to a line in the Chat view
    first: float
    tries: int = 1
    last: float = 0.0
    max_tries: int = 6    # file pieces get many more tries: they only fail when the link is gone


@dataclass
class TxStats:
    sent: int = 0
    delivered: int = 0
    retries: int = 0
    failed: int = 0


class LinkWorker(threading.Thread):
    """Background thread: reads the port, puts parsed items into `events`, and re-sends
    packets until the other side confirms them with an ACK.

    The other side is either an STM32 (which ACKs on its own) or another copy of this app.
    Apps announce themselves with HELLO; only then do we ACK and de-duplicate their packets.
    """

    MAX_TRIES = 6
    MIN_RTO, MAX_RTO = 0.4, 6.0   # re-send timeout limits (s); adapts to the measured delay
    DRAIN_S = 0.6                 # on connect, drop data the ground unit buffered earlier
    # Congestion control (like TCP): the radio is slower than the UART and drops what does not fit
    # in its small buffer. So we start with few packets in flight, add more while everything is
    # confirmed, and halve at the first loss. The sender settles at what the radio can carry.
    CWND_START, CWND_MAX = 2.0, 24.0

    def __init__(self, port, events: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.events = events
        self.parser = proto.Parser()
        self.tx = TxStats()
        self.peer_is_app = False
        self.peer_session: int | None = None
        self.session = random.getrandbits(32)   # tells the other side "this is a new connection"
        self.hello_ok = False
        self.srtt: float | None = None  # smoothed round-trip time (s)
        self.rttvar = 0.0               # how much the round-trip time jumps around
        self.cwnd = self.CWND_START     # packets allowed in flight (congestion window)
        self.ssthresh = self.CWND_MAX / 2
        self._last_cut = 0.0
        self._stop = threading.Event()
        self._tx_lock = threading.Lock()
        self._tx_seq = random.randrange(256)
        self._pending: dict[int, Pending] = {}
        self._plock = threading.Lock()
        self._seen: dict[int, tuple[int, int]] = {}  # seq -> (type, crc of payload) recently accepted
        self._tokens = itertools.count(1)

    # ---- thread ---------------------------------------------------------------

    def run(self) -> None:
        try:
            skipped, t_end = 0, time.monotonic() + self.DRAIN_S
            while time.monotonic() < t_end and not self._stop.is_set():
                skipped += len(self.port.read(getattr(self.port, "in_waiting", 0) or 1))
            if skipped:
                self.events.put(("info", f"Skipped {skipped:,} bytes of old data buffered in the ground unit"))
            last_rx = time.monotonic()
            self.send_reliable(proto.TYPE_HELLO, proto.hello_payload(True, self.session), "hello",
                               report=False)
            while not self._stop.is_set():
                data = self.port.read(getattr(self.port, "in_waiting", 0) or 1)
                now = time.monotonic()
                if data:
                    last_rx = now
                    shown = [it for it in self.parser.feed(data) if self._incoming(it)]
                    self.events.put(("rx", time.time(), len(data), shown))
                elif now - last_rx > 0.3:
                    items = self.parser.flush_raw()
                    if items:
                        self.events.put(("rx", time.time(), 0, items))
                self._retry(now)
        except Exception as exc:  # port unplugged, ground unit turned off, ...
            if not self._stop.is_set():
                self.events.put(("error", str(exc)))
        finally:
            try:
                self.port.close()
            except Exception:
                pass
            self.events.put(("closed",))

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---- receiving ------------------------------------------------------------

    def _incoming(self, it) -> bool:
        """Handle link-level packets. Returns True if the item should be shown."""
        if not isinstance(it, proto.Frame):
            return True
        if it.ftype == proto.TYPE_ACK:
            self._on_ack(it)
            return False
        if it.ftype == proto.TYPE_HELLO:   # the other side is an app that (re)connected
            self._send_ack(it)
            please, session = proto.parse_hello(it.payload)
            if session is not None and session == self.peer_session:
                return False               # a re-sent copy of the same HELLO: nothing new
            self.peer_session = session
            self._seen.clear()             # new session: its sequence numbers start over
            first = not self.peer_is_app
            self.peer_is_app = True
            self.parser.count_lost = False   # see Parser.count_lost
            self.parser.stats.lost = 0
            if please:
                self.send_reliable(proto.TYPE_HELLO, proto.hello_payload(False, self.session), "hello",
                                   report=False)
            self.events.put(("peer", first))
            return False
        if self.peer_is_app and it.ftype in APP_ACKED:
            self._send_ack(it)
            # A re-sent copy (our ACK got lost) has the same seq AND the same content. seq is only
            # 8 bits and wraps, so the content check stops a NEW packet that reuses an old number
            # from being thrown away as a "copy".
            key = (it.ftype, proto.crc16(it.payload))
            if self._seen.get(it.seq) == key:
                return False
            self._seen[it.seq] = key
            self._seen.pop((it.seq + 128) & 0xFF, None)
        return True

    def _send_ack(self, frame: proto.Frame) -> None:
        with self._plock:
            seq = self._next_seq()
        self.send(proto.encode_ack(frame.seq, frame.ftype, seq))

    def _on_ack(self, frame: proto.Frame) -> None:
        try:
            seq, ftype = frame.acked()
        except ValueError:
            return
        now = time.monotonic()
        with self._plock:
            p = self._pending.get(seq)
            if p is None or p.ftype != ftype:
                return  # late ACK for something already confirmed
            del self._pending[seq]
            self.tx.delivered += 1
            if p.tries == 1:  # only clean samples (Karn's rule)
                rtt = now - p.first
                if self.srtt is None:
                    self.srtt, self.rttvar = rtt, rtt / 2
                else:
                    self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt)
                    self.srtt = 0.875 * self.srtt + 0.125 * rtt
            # congestion window grows while things get through: fast at first, then slowly
            if self.cwnd < self.ssthresh:
                self.cwnd = min(self.CWND_MAX, self.cwnd + 1)
            else:
                self.cwnd = min(self.CWND_MAX, self.cwnd + 1 / self.cwnd)
        if p.desc == "hello":
            self.hello_ok = True
        if p.group is not None:
            p.group["ok"] += 1
            p.group["ok_bytes"] = p.group.get("ok_bytes", 0) + p.group.get("size", {}).get(seq, 0)
        self.events.put(("delivered", p.desc, p.tries, p.report, p.token))

    # ---- sending --------------------------------------------------------------

    def _next_seq(self) -> int:
        """Next sequence number that is not still waiting for an ACK (call with _plock)."""
        seq = self._tx_seq
        for _ in range(256):
            seq = (seq + 1) & 0xFF
            if seq not in self._pending:
                break
        self._tx_seq = seq
        return seq

    def new_token(self) -> int:
        return next(self._tokens)

    def send(self, data: bytes) -> None:
        """Send raw bytes, no confirmation (used for ACKs and plain text lines)."""
        with self._tx_lock:
            self.port.write(data)

    def send_reliable(self, ftype: int, payload: bytes, desc: str, report: bool = True,
                      group: dict | None = None, token: int | None = None, size: int = 0,
                      max_tries: int | None = None) -> int:
        """Send a packet and keep re-sending it until it is ACKed. Returns its seq."""
        now = time.monotonic()
        with self._plock:
            seq = self._next_seq()
            frame = proto.encode(ftype, payload, seq)
            self._pending[seq] = Pending(seq, ftype, frame, desc, report, group, token, now, 1, now,
                                         max_tries or self.MAX_TRIES)
            if group is not None and size:
                group.setdefault("size", {})[seq] = size
            self.tx.sent += 1
        self.send(frame)
        return seq

    def pending_count(self) -> int:
        with self._plock:
            return len(self._pending)

    @property
    def window(self) -> int:
        """How many packets may wait for an ACK at once right now."""
        return max(1, int(self.cwnd))

    @property
    def rto(self) -> float:
        """Re-send timeout: measured round trip plus 4x its jitter (like TCP)."""
        base = 1.0 if self.srtt is None else self.srtt + 4 * self.rttvar
        return min(self.MAX_RTO, max(self.MIN_RTO, base))

    def _retry(self, now: float) -> None:
        rto = self.rto
        resend, failed = [], []
        with self._plock:
            for p in list(self._pending.values()):
                if now - p.last < min(rto * (1.5 ** (p.tries - 1)), self.MAX_RTO):  # back off each try
                    continue
                if p.tries >= p.max_tries:
                    del self._pending[p.seq]
                    self.tx.failed += 1
                    failed.append(p)
                else:
                    p.tries += 1
                    p.last = now
                    self.tx.retries += 1
                    resend.append(p.frame)
            if resend and now - self._last_cut > max(rto, 0.3):
                # a loss: the radio is full -> halve the window (at most once per round trip)
                self._last_cut = now
                self.ssthresh = max(self.cwnd / 2, 2.0)
                self.cwnd = max(1.0, self.cwnd / 2)
        for frame in resend:
            self.send(frame)
        for p in failed:
            if p.group is not None:
                p.group["failed"] += 1
            self.events.put(("failed", p.desc, p.tries, p.report, p.token))


# --------------------------------------------------------------------------- files


def send_files(worker: LinkWorker, paths: list[str], events: queue.Queue, cancel: threading.Event,
               is_current) -> None:
    """Send files one after another. Posts ("progress", dict) and ("file_sent", dict) events.
    Runs in its own thread. `is_current()` returns False once the user disconnected."""
    fid = random.randrange(256)
    total = len(paths)
    for index, path in enumerate(paths, 1):
        if cancel.is_set() or not is_current():
            break
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            events.put(("file_sent", {"name": name, "ok": False, "error": f"cannot read: {exc}"}))
            continue
        out = proto.prepare_file(name, data)
        fid = (fid + 1) & 0xFF
        info = {"name": name, "index": index, "count": total, "size": len(data),
                "sent_size": len(out.body), "compressed": out.compressed, "path": path}
        events.put(("file_begin", dict(info)))
        result = _send_one(worker, fid, out, info, events, cancel, is_current)
        events.put(("file_sent", {**info, **result}))
        if not result["ok"] and not result.get("cancelled"):
            continue
    events.put(("files_done",))


FILE_TRIES = 40      # a file piece is re-sent up to 40 times (with back-off) before it counts as lost
FILE_STALL_S = 60.0  # a file only fails if NOTHING got through for this long


def _wait(group: dict, want: int, cancel: threading.Event, is_current, stall: float) -> bool:
    """Wait until `want` packets of the group are answered; give up only after `stall` s without progress."""
    last, last_change = -1, time.monotonic()
    while group["ok"] + group["failed"] < want:
        if cancel.is_set() or not is_current():
            return False
        if group["ok"] != last:
            last, last_change = group["ok"], time.monotonic()
        elif time.monotonic() - last_change > stall:
            return False
        time.sleep(0.01)
    return group["failed"] == 0


def _send_one(worker, fid, out: proto.OutgoingFile, info, events, cancel, is_current) -> dict:
    t0 = time.monotonic()
    start = {"ok": 0, "failed": 0}
    worker.send_reliable(proto.TYPE_FILE_START, proto.file_start_payload(fid, out), "file start",
                         report=False, group=start, max_tries=FILE_TRIES)
    if not _wait(start, 1, cancel, is_current, FILE_STALL_S):
        return {"ok": False, "cancelled": cancel.is_set(),
                "error": "the receiver did not answer (is its app running and connected?)"}

    pieces = proto.file_pieces(out.body)
    group = {"ok": 0, "failed": 0}
    cap = 24 if worker.peer_is_app else 4  # an STM32 can only queue 8 packets
    last_report, last_ok, last_change = 0.0, -1, time.monotonic()
    for i, (offset, data) in enumerate(pieces):
        while worker.pending_count() >= min(worker.window, cap):  # adaptive: what the radio can carry
            if cancel.is_set() or not is_current():
                return {"ok": False, "cancelled": cancel.is_set(), "error": "cancelled"}
            if group["failed"]:
                return {"ok": False, "error": f"{group['failed']} pieces were not delivered (the link was lost)"}
            if group["ok"] != last_ok:
                last_ok, last_change = group["ok"], time.monotonic()
            elif time.monotonic() - last_change > FILE_STALL_S:
                return {"ok": False, "error": f"nothing got through for {FILE_STALL_S:.0f} s (the link was lost)"}
            time.sleep(0.005)
        if cancel.is_set() or not is_current():
            return {"ok": False, "cancelled": cancel.is_set(), "error": "cancelled"}
        worker.send_reliable(proto.TYPE_FILE_DATA, proto.file_data_payload(fid, offset, data), "file piece",
                             report=False, group=group, size=len(data), max_tries=FILE_TRIES)
        now = time.monotonic()
        if now - last_report > 0.2:
            last_report = now
            events.put(("progress", _progress("send", info, group.get("ok_bytes", 0), len(out.body), t0)))
    if not _wait(group, len(pieces), cancel, is_current, FILE_STALL_S):
        if cancel.is_set():
            return {"ok": False, "cancelled": True, "error": "cancelled"}
        return {"ok": False, "error": f"{len(pieces) - group['ok']} of {len(pieces)} pieces were not delivered"}
    end = {"ok": 0, "failed": 0}
    worker.send_reliable(proto.TYPE_FILE_END, bytes((fid,)), "file end", report=False, group=end,
                         max_tries=FILE_TRIES)
    if not _wait(end, 1, cancel, is_current, FILE_STALL_S):
        return {"ok": False, "cancelled": cancel.is_set(), "error": "end of file not confirmed"}
    secs = max(time.monotonic() - t0, 1e-6)
    events.put(("progress", _progress("send", info, len(out.body), len(out.body), t0)))
    return {"ok": True, "secs": secs, "speed": len(out.original) / secs}


def _progress(direction: str, info: dict, done: int, total: int, t0: float) -> dict:
    """done/total are bytes on the radio; speed and ETA are in original-file bytes."""
    frac = done / total if total else 1.0
    elapsed = max(time.monotonic() - t0, 1e-6)
    speed = frac * info["size"] / elapsed
    eta = (1 - frac) * elapsed / frac if frac > 0 else math.inf
    return {**info, "dir": direction, "frac": frac, "speed": speed, "eta": eta}


def receive_progress(info: dict, frac: float, t0: float) -> dict:
    elapsed = max(time.monotonic() - t0, 1e-6)
    speed = frac * info["size"] / elapsed
    eta = (1 - frac) * elapsed / frac if frac > 0 else math.inf
    return {**info, "dir": "recv", "frac": frac, "speed": speed, "eta": eta}


# --------------------------------------------------------------------------- fake ports


class DemoSerial:
    """Behaves like a serial.Serial with an STM32 behind it (for --demo)."""

    LOSS = 0.10  # lose 10 % of packets in each direction

    def __init__(self) -> None:
        self._seq = 0
        self._t0 = time.monotonic()
        self._next = self._t0
        self._battery = 12.4
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._rx_parser = proto.Parser()
        self._seen: set[int] = set()
        self._rx_bytes = 0
        self.is_open = True

    def read(self, _n: int = 1) -> bytes:
        now = time.monotonic()
        if now < self._next:
            time.sleep(min(0.1, self._next - now))
            return b""
        self._next += 0.1
        t = now - self._t0
        out = bytearray()
        self._battery = max(10.5, self._battery - 0.0005)
        for name, val in (
            ("temp", 24 + 3 * math.sin(t / 4)),
            ("battery", self._battery + random.uniform(-0.02, 0.02)),
            ("rssi", -60 + 8 * math.sin(t / 7) + random.uniform(-2, 2)),
        ):
            out += proto.encode_value(name, val, self._next_seq())
        if int(t * 10) % 20 == 0:
            out += proto.encode_text(f"uptime {t:.0f} s", self._next_seq())
        if random.random() < 0.02:  # simulate a radio error so the CRC counter moves
            i = random.randrange(len(out))
            out[i] ^= 0x5A
        with self._lock:
            out += self._pending
            self._pending.clear()
        return bytes(out)

    def write(self, data: bytes) -> int:
        """Acts like the STM32 firmware: ACK, ignore re-sent copies, reply to new packets."""
        out = bytearray()
        for item in self._rx_parser.feed(data):
            if isinstance(item, proto.RawLine):
                out += proto.encode_text(f"plain line: {item.text}", self._next_seq())
                continue
            if item.ftype == proto.TYPE_ACK or random.random() < self.LOSS:
                continue
            if item.ftype == proto.TYPE_HELLO:
                self._seen.clear()
            elif item.seq not in self._seen:
                self._seen.add(item.seq)
                self._seen.discard((item.seq + 128) & 0xFF)
                reply = ""
                if item.ftype == proto.TYPE_VALUE:
                    name, v = item.value()
                    out += proto.encode_value(name, v, self._next_seq())
                    reply = f"ack number: {name}"
                elif item.ftype == proto.TYPE_BYTES:
                    self._rx_bytes += len(item.payload)
                    out += proto.encode_value("rx_bytes", self._rx_bytes, self._next_seq())
                    if len(item.payload) < proto.MAX_PAYLOAD:
                        reply = f"ack bytes: {len(item.payload)}"
                elif item.ftype in (proto.TYPE_TEXT, proto.TYPE_CMD):
                    reply = f"ack {item.type_name.lower()}: {item.text()}"
                if reply:
                    out += proto.encode_text(reply, self._next_seq())
            if random.random() >= self.LOSS:  # the ACK itself may be lost too
                out += proto.encode_ack(item.seq, item.ftype, self._next_seq())
            else:
                self._next_seq()
        with self._lock:
            self._pending += out
        return len(data)

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return s

    def close(self) -> None:
        self.is_open = False


class _Radio:
    """One direction of the simulated link: the UART (bytes_per_s), then optionally a slower radio
    (air_bps) with a small buffer that drops what does not fit (like the HM30 datalink), plus delay
    and random packet loss."""

    def __init__(self, bytes_per_s: float, delay: float, loss: float,
                 air_bps: float | None = None, buffer: int | None = None) -> None:
        self.bps, self.delay, self.loss = bytes_per_s, delay, loss
        self.air_bps, self.buffer = air_bps, buffer
        self.items: deque[tuple[float, bytes]] = deque()
        self.cv = threading.Condition()
        self.busy_until = 0.0  # UART
        self.air_until = 0.0   # radio
        self.dropped = 0

    def put(self, data: bytes) -> None:
        with self.cv:
            now = time.monotonic()
            self.busy_until = max(now, self.busy_until) + len(data) / self.bps
            arrive = self.busy_until
            if self.air_bps:
                backlog = max(0.0, self.air_until - arrive) * self.air_bps
                if self.buffer is not None and backlog + len(data) > self.buffer:
                    self.dropped += 1  # radio buffer full: the packet is silently lost
                    return
                self.air_until = max(arrive, self.air_until) + len(data) / self.air_bps
                arrive = self.air_until
            if random.random() >= self.loss:
                self.items.append((arrive + self.delay, data))
                self.cv.notify()

    def get(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        out = bytearray()
        with self.cv:
            while True:
                now = time.monotonic()
                while self.items and self.items[0][0] <= now:
                    out += self.items.popleft()[1]
                if out or now >= end:
                    return bytes(out)
                wake = min(end, self.items[0][0]) if self.items else end
                self.cv.wait(max(0.0, wake - now))


class PairPort:
    """One end of a simulated HM30 link between two app windows (for --pair and tests)."""

    def __init__(self, tx: _Radio, rx: _Radio) -> None:
        self._tx, self._rx = tx, rx
        self.is_open = True
        self.in_waiting = 0

    def read(self, _n: int = 1) -> bytes:
        return self._rx.get(0.1)

    def write(self, data: bytes) -> int:
        if self.is_open:
            self._tx.put(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False


def make_pair(baud: int = 115200, delay: float = 0.06, loss: float = 0.05,
              air_bps: float | None = None, buffer: int | None = None) -> tuple[PairPort, PairPort]:
    """Two ports joined by a simulated radio: `loss` of packets lost in each direction.
    air_bps/buffer: a radio slower than the UART with a small drop-when-full buffer."""
    a_to_b = _Radio(baud / 10, delay, loss, air_bps, buffer)
    b_to_a = _Radio(baud / 10, delay, loss, air_bps, buffer)
    return PairPort(a_to_b, b_to_a), PairPort(b_to_a, a_to_b)
