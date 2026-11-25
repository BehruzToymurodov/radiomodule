"""Tests for protocol.py.   python -m unittest test_protocol -v"""

import struct
import unittest

import protocol as p


class ProtocolTest(unittest.TestCase):
    def test_crc_reference_vector(self):
        self.assertEqual(p.crc16(b"123456789"), 0x29B1)  # CRC-16/CCITT-FALSE check value

    def test_roundtrip_all_types(self):
        data = (p.encode_text("hello", 1) + p.encode_value("temp", 24.5, 2)
                + p.encode(p.TYPE_BYTES, bytes(range(10)), 3) + p.encode_cmd("LED ON", 4))
        items = p.Parser().feed(data)
        self.assertEqual([i.ftype for i in items], [p.TYPE_TEXT, p.TYPE_VALUE, p.TYPE_BYTES, p.TYPE_CMD])
        self.assertEqual(items[0].text(), "hello")
        self.assertEqual(items[1].value(), ("temp", 24.5))
        self.assertEqual(items[2].payload, bytes(range(10)))
        self.assertEqual(items[3].text(), "LED ON")

    def test_one_byte_at_a_time(self):
        parser, out = p.Parser(), []
        for b in p.encode_text("split", 0) * 3:
            out += parser.feed(bytes([b]))
        self.assertEqual([i.text() for i in out], ["split"] * 3)

    def test_corrupted_frame_dropped_and_next_frame_ok(self):
        bad = bytearray(p.encode_text("broken", 0))
        bad[7] ^= 0xFF
        parser = p.Parser()
        items = [i for i in parser.feed(bytes(bad) + p.encode_text("good", 1)) if isinstance(i, p.Frame)]
        self.assertEqual([i.text() for i in items], ["good"])
        self.assertEqual(parser.stats.crc_errors, 1)

    def test_plain_text_lines_pass_through(self):
        parser = p.Parser()
        items = parser.feed(b"hello from STM32\r\n" + p.encode_value("v", 1.0) + b"second\n")
        self.assertIsInstance(items[0], p.RawLine)
        self.assertEqual(items[0].text, "hello from STM32")
        self.assertEqual(items[1].value(), ("v", 1.0))
        self.assertEqual(items[2].text, "second")

    def test_raw_0xaa_bytes_do_not_eat_frames(self):
        items = p.Parser().feed(b"\xaa\xaa" + p.encode_text("ok", 0))
        frames = [i for i in items if isinstance(i, p.Frame)]
        self.assertEqual([f.text() for f in frames], ["ok"])

    def test_lost_frames_counted_from_seq(self):
        parser = p.Parser()
        parser.feed(p.encode_text("a", 10) + p.encode_text("b", 11) + p.encode_text("c", 14))
        self.assertEqual(parser.stats.lost, 2)

    def test_seq_wraparound_not_counted_as_loss(self):
        parser = p.Parser()
        parser.feed(p.encode_text("a", 255) + p.encode_text("b", 0))
        self.assertEqual(parser.stats.lost, 0)

    def test_payload_limits(self):
        self.assertEqual(len(p.Parser().feed(p.encode(p.TYPE_BYTES, b"x" * p.MAX_PAYLOAD))), 1)
        with self.assertRaises(ValueError):
            p.encode(p.TYPE_BYTES, b"x" * (p.MAX_PAYLOAD + 1))

    def test_value_is_float32(self):
        frame = p.encode_value("pi", 3.14159)
        self.assertEqual(frame[5:9], struct.pack("<f", 3.14159))


    def test_parse_values(self):
        self.assertEqual(p.parse_values("speed=1.5, angle = -30"), [("speed", 1.5), ("angle", -30.0)])
        self.assertEqual(p.parse_values("x=2;y=3e2"), [("x", 2.0), ("y", 300.0)])
        for bad in ("speed", "=5", "speed=fast", ""):
            with self.assertRaises(ValueError):
                p.parse_values(bad)

    def test_parse_hex(self):
        for text in ("01 02 ff", "0x01,0x02,0xFF", "0102FF", "01,02, FF"):
            self.assertEqual(p.parse_hex(text), b"\x01\x02\xff")
        for bad in ("0g", "123", ""):
            with self.assertRaises(ValueError):
                p.parse_hex(bad)

    def test_chunk_roundtrip(self):
        data = bytes(range(256)) * 3
        parts = p.chunk(data)
        self.assertTrue(all(len(c) <= p.MAX_PAYLOAD for c in parts))
        frames = p.Parser().feed(b"".join(p.encode_bytes(c, i) for i, c in enumerate(parts)))
        self.assertEqual(b"".join(f.payload for f in frames), data)


    def test_ack_roundtrip(self):
        (f,) = p.Parser().feed(p.encode_ack(42, p.TYPE_CMD, 7))
        self.assertEqual((f.ftype, f.acked()), (p.TYPE_ACK, (42, p.TYPE_CMD)))

    def test_value_payload_matches_frame(self):
        self.assertEqual(p.encode(p.TYPE_VALUE, p.value_payload("v", 2.5), 3), p.encode_value("v", 2.5, 3))


    # ---- files ----

    def _transfer(self, name, data, fid=7):
        out = p.prepare_file(name, data)
        inc = p.IncomingFile.from_start(p.file_start_payload(fid, out))
        pieces = p.file_pieces(out.body)
        for off, piece in reversed(pieces):          # order must not matter
            inc.add(p.file_data_payload(fid, off, piece))
        inc.add(p.file_data_payload(fid, *pieces[0]))  # duplicates must not matter
        return out, inc

    def test_text_file_is_compressed_and_restored(self):
        data = ("time,temp\n" + "".join(f"{i},{20 + i % 7}\n" for i in range(3000))).encode()
        out, inc = self._transfer("log.csv", data)
        self.assertTrue(out.compressed)
        self.assertLess(len(out.body), len(data) / 3)
        self.assertEqual(inc.name, "log.csv")
        self.assertEqual(inc.finish(), data)

    def test_jpg_and_random_data_not_compressed(self):
        import os
        rnd = os.urandom(5000)
        self.assertFalse(p.prepare_file("photo.jpg", b"a" * 5000).compressed)  # by extension
        self.assertFalse(p.prepare_file("data.bin", rnd).compressed)           # does not shrink
        out, inc = self._transfer("data.bin", rnd)
        self.assertEqual(inc.finish(), rnd)

    def test_missing_piece_and_corruption_detected(self):
        data = bytes(range(256)) * 10
        out = p.prepare_file("x.bin", data)
        inc = p.IncomingFile.from_start(p.file_start_payload(1, out))
        for off, piece in p.file_pieces(out.body)[1:]:
            inc.add(p.file_data_payload(1, off, piece))
        with self.assertRaises(p.FileError):
            inc.finish()
        bad = bytearray(out.body)
        bad[100] ^= 1
        inc = p.IncomingFile.from_start(p.file_start_payload(1, out))
        for off, piece in p.file_pieces(bytes(bad)):
            inc.add(p.file_data_payload(1, off, piece))
        with self.assertRaises(p.FileError):
            inc.finish()

    def test_empty_file(self):
        out = p.prepare_file("empty.txt", b"")
        inc = p.IncomingFile.from_start(p.file_start_payload(2, out))
        self.assertEqual(inc.finish(), b"")

    def test_payloads_fit(self):
        out = p.prepare_file("n" * 500 + ".txt", b"hi")
        self.assertLessEqual(len(p.file_start_payload(1, out)), p.MAX_PAYLOAD)
        self.assertTrue(all(len(p.file_data_payload(1, o, d)) <= p.MAX_PAYLOAD
                            for o, d in p.file_pieces(bytes(10000))))

    def test_safe_and_unique_names(self):
        import tempfile, os
        self.assertEqual(p.safe_filename("..\\..\\evil/a:b?.txt"), "a_b_.txt")
        self.assertEqual(p.safe_filename(""), "received_file")
        with tempfile.TemporaryDirectory() as d:
            first = p.unique_path(d, "a.txt")
            open(first, "w").close()
            self.assertEqual(os.path.basename(p.unique_path(d, "a.txt")), "a (1).txt")


if __name__ == "__main__":
    unittest.main()
