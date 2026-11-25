"""PC-to-PC tests over a simulated radio.   python -m unittest test_link -v"""

import os
import queue
import tempfile
import threading
import time
import unittest
import zlib

import link_core as core
import protocol as p


class Receiver:
    """Minimal stand-in for the app's receiving side: collects texts and files."""

    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self.texts: list[str] = []
        self.values: list[tuple[str, float]] = []
        self.files: dict[str, bytes] = {}
        self._incoming: dict[int, p.IncomingFile] = {}
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            if ev[0] != "rx":
                continue
            for it in ev[3]:
                if not isinstance(it, p.Frame):
                    continue
                if it.ftype == p.TYPE_TEXT:
                    self.texts.append(it.text())
                elif it.ftype == p.TYPE_VALUE:
                    self.values.append(it.value())
                elif it.ftype == p.TYPE_FILE_START:
                    f = p.IncomingFile.from_start(it.payload)
                    self._incoming[f.fid] = f
                elif it.ftype == p.TYPE_FILE_DATA:
                    f = self._incoming.get(p.file_data_id(it.payload))
                    if f:
                        f.add(it.payload)
                elif it.ftype == p.TYPE_FILE_END:
                    f = self._incoming.pop(it.payload[0])
                    self.files[f.name] = f.finish()

    def stop(self) -> None:
        self._stop.set()


def wait_for(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PairTest(unittest.TestCase):
    def setUp(self):
        a, b = core.make_pair(baud=230400, delay=0.03, loss=self.loss)
        self.ev_a, self.ev_b = queue.Queue(), queue.Queue()
        self.a, self.b = core.LinkWorker(a, self.ev_a), core.LinkWorker(b, self.ev_b)
        self.a.start()
        time.sleep(0.3)              # the receiver connects a bit later than the sender
        self.b.start()
        self.rx = Receiver(self.ev_b)
        self.assertTrue(wait_for(lambda: self.a.peer_is_app and self.b.peer_is_app, 8), "apps did not find each other")

    def tearDown(self):
        self.a.stop()
        self.b.stop()
        self.rx.stop()

    loss = 0.10


class LossyLinkTest(PairTest):
    loss = 0.10

    def test_messages_arrive_exactly_once(self):
        for i in range(40):
            self.a.send_reliable(p.TYPE_TEXT, f"msg {i}".encode(), f"msg {i}")
        self.a.send_reliable(p.TYPE_VALUE, p.value_payload("speed", 1.5), "speed")
        self.assertTrue(wait_for(lambda: self.a.pending_count() == 0, 20))
        self.assertTrue(wait_for(lambda: len(self.rx.texts) == 40, 5))
        self.assertEqual(sorted(self.rx.texts, key=lambda s: int(s.split()[1])), [f"msg {i}" for i in range(40)])
        self.assertEqual(self.rx.values, [("speed", 1.5)])
        self.assertEqual(self.a.tx.failed, 0)
        self.assertGreater(self.a.tx.retries, 0)   # with 10 % loss some packets had to be re-sent

    def test_files_survive_loss(self):
        with tempfile.TemporaryDirectory() as d:
            text = os.path.join(d, "log.csv")
            with open(text, "w") as f:
                f.write("".join(f"{i},{i * 0.5:.1f},ok\n" for i in range(4000)))
            binary = os.path.join(d, "photo.jpg")
            with open(binary, "wb") as f:
                f.write(os.urandom(12000))
            events, cancel = queue.Queue(), threading.Event()
            t0 = time.monotonic()
            core.send_files(self.a, [text, binary], events, cancel, lambda: True)
            secs = time.monotonic() - t0
            results = [e[1] for e in list(events.queue) if e[0] == "file_sent"]
            self.assertEqual([r["ok"] for r in results], [True, True], results)
            self.assertTrue(results[0]["compressed"])
            self.assertFalse(results[1]["compressed"])
            self.assertTrue(wait_for(lambda: len(self.rx.files) == 2, 5))
            self.assertEqual(self.rx.files["log.csv"], open(text, "rb").read())
            self.assertEqual(self.rx.files["photo.jpg"], open(binary, "rb").read())
            progress = [e[1] for e in list(events.queue) if e[0] == "progress"]
            self.assertTrue(progress and progress[-1]["frac"] == 1.0)
            size = os.path.getsize(text) + os.path.getsize(binary)
            print(f"\n  2 files, {size:,} bytes in {secs:.1f} s with 10% loss "
                  f"({size / secs:,.0f} B/s), re-sent {self.a.tx.retries}, failed {self.a.tx.failed}")

    def test_cancel_stops_quickly(self):
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "big.bin")
            with open(big, "wb") as f:
                f.write(os.urandom(300_000))
            events, cancel = queue.Queue(), threading.Event()
            th = threading.Thread(target=core.send_files, args=(self.a, [big], events, cancel, lambda: True))
            th.start()
            time.sleep(1.0)
            cancel.set()
            th.join(5)
            self.assertFalse(th.is_alive())
            results = [e[1] for e in list(events.queue) if e[0] == "file_sent"]
            self.assertTrue(results and results[0].get("cancelled"))


class RestartTest(PairTest):
    loss = 0.05

    def test_receiver_app_restart_still_receives(self):
        for i in range(10):
            self.a.send_reliable(p.TYPE_TEXT, f"before {i}".encode(), "x")
        self.assertTrue(wait_for(lambda: self.a.pending_count() == 0, 15))
        # the receiver closes and opens its app again: a new worker on the same port
        port = self.b.port
        self.b.stop()
        self.rx.stop()
        time.sleep(0.3)
        port.is_open = True
        ev = queue.Queue()
        self.b = core.LinkWorker(port, ev)
        self.b.start()
        self.rx = Receiver(ev)
        self.assertTrue(wait_for(lambda: self.b.peer_is_app, 8))
        for i in range(10):
            self.a.send_reliable(p.TYPE_TEXT, f"after {i}".encode(), "x")
        self.assertTrue(wait_for(lambda: self.a.pending_count() == 0, 15))
        self.assertTrue(wait_for(lambda: len(self.rx.texts) == 10, 5), self.rx.texts)
        self.assertEqual(sorted(self.rx.texts), sorted(f"after {i}" for i in range(10)))
        self.assertEqual(self.a.tx.failed, 0)


class StmCompatibilityTest(unittest.TestCase):
    def test_stm32_values_are_not_acked_or_filtered(self):
        """An STM32 never sends HELLO: its packets must pass through untouched (even repeated seq)."""
        ev = queue.Queue()
        w = core.LinkWorker(core.DemoSerial(), ev)
        shown = [x for x in (p.Parser().feed(p.encode_value("t", 1.0, 5) * 3)) if w._incoming(x)]
        self.assertEqual(len(shown), 3)
        self.assertFalse(w.peer_is_app)


if __name__ == "__main__":
    unittest.main()
