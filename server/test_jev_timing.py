"""Timing survives outer process deadlines and never admits private text."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import jev_server as jev
from jev_errors import error_diagnostic
from jev_timing import helper_timing, safe_timing

PRIVATE = "synthetic-private-key-and-body"
NODE = shutil.which("node")


class TimingSafetyTests(unittest.TestCase):
    def test_only_known_phases_and_finite_nonnegative_metrics_are_retained(self):
        for invalid in (None, [], {"phase": []}, {"phase": PRIVATE}):
            self.assertEqual(safe_timing(invalid), {})
        for invalid in (True, -1, float("nan"), float("inf"), 86_400_001, PRIVATE):
            with self.subTest(invalid=repr(invalid)):
                self.assertEqual(safe_timing({"phase": "connecting", "elapsed_ms": invalid,
                    "url": PRIVATE, "key": PRIVATE, "transport": PRIVATE}), {"phase": "connecting"})

    def test_partial_timeout_stderr_keeps_last_complete_checkpoint(self):
        stderr = ('private unstructured text\n' + json.dumps({"jev_timing": {
            "phase": "waiting_headers", "connected_at_ms": 40, "private": PRIVATE}})
            + '\n{"jev_timing":').encode()
        with mock.patch("jev_timing.time.monotonic", return_value=12):
            timing = helper_timing(stderr, 10, 2)
        self.assertEqual(timing, {"phase": "waiting_headers", "connected_at_ms": 40,
            "transport": "node", "observed": True, "elapsed_ms": 2000, "budget_ms": 2000})
        self.assertNotIn(PRIVATE, json.dumps(timing))
        self.assertFalse(helper_timing("x" * 32769, time.monotonic(), 2)["observed"])

    def test_outer_timeout_attaches_checkpoints_without_changing_failure_type(self):
        for phase in ("helper_start", "connecting", "waiting_headers", "reading_body"):
            stderr = json.dumps({"jev_timing": {"phase": phase, "helper_elapsed_ms": 10,
                "body": PRIVATE}}).encode()
            failure = subprocess.TimeoutExpired(PRIVATE, .1, output=PRIVATE, stderr=stderr)
            with self.subTest(phase=phase), mock.patch.object(jev, "_node_binary", return_value="/fixture/node"), \
                 mock.patch.object(jev.subprocess, "run", side_effect=failure):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    jev.call_jev(PRIVATE, PRIVATE, timeout=.1)
            safe = error_diagnostic(caught.exception, "transport")
            self.assertEqual(safe["jev_error_reason"], "timeout")
            self.assertEqual(safe["jev_timing"]["phase"], phase)
            self.assertNotIn(PRIVATE, json.dumps(safe))

    def test_python_fallback_marks_the_phase_without_claiming_connection_detail(self):
        response = mock.MagicMock()
        response.read.side_effect = TimeoutError(PRIVATE)
        with mock.patch.object(jev, "_node_binary", return_value=None), \
             mock.patch.object(jev.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaises(TimeoutError) as caught:
                jev.call_jev(PRIVATE, PRIVATE)
        safe = error_diagnostic(caught.exception, "transport")
        self.assertEqual(safe["jev_timing"]["phase"], "reading_body")
        self.assertEqual(safe["jev_timing"]["transport"], "python")
        self.assertNotIn("connected_at_ms", safe["jev_timing"])
        self.assertNotIn(PRIVATE, json.dumps(safe))


@unittest.skipUnless(NODE, "Node.js unavailable")
class TimingLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        owner = self
        self.headers_delay = self.body_delay = 0
        class API(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(owner.headers_delay)
                try:
                    body = b'{"model":"typesafe/jev-1.13","answers":{}}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(owner.body_delay)
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), API)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.preload = Path(self.directory.name) / 'fixture.mjs'
        self.preload.write_text('const original=globalThis.fetch;\n'
            'globalThis.fetch=(url,options)=>{\n'
            'if(!["https://api.typesafe.ai/v1/systemone","https://openrouter.ai/api/alpha/decisions"].includes(url))'
            'throw Error("Nonfixture endpoint");\n'
            f'return original("http://127.0.0.1:{self.server.server_port}/decision",options);\n}};\n')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, timeout):
        with mock.patch.object(jev, '_node_binary', return_value=NODE), \
             mock.patch.dict(os.environ, {'NODE_OPTIONS': '--import=' + self.preload.as_uri()}):
            return jev.call_jev(PRIVATE, {'task': PRIVATE}, timeout=timeout)

    def test_success_records_ordered_network_milestones(self):
        self.headers_delay = self.body_delay = .06
        timing = self.call(3)['_jev_timing']
        self.assertEqual(timing['phase'], 'complete')
        self.assertTrue(timing['observed'])
        self.assertGreaterEqual(timing['startup_ms'], 0)
        milestones = [timing[k] for k in ('request_at_ms', 'connect_started_at_ms', 'connected_at_ms',
                       'headers_at_ms', 'body_done_at_ms', 'parse_done_at_ms')]
        self.assertEqual(milestones, sorted(milestones))
        self.assertGreaterEqual(timing['body_done_at_ms'] - timing['headers_at_ms'], 40)

    def test_outer_deadline_identifies_headers_and_body_stalls(self):
        for phase in ('waiting_headers', 'reading_body'):
            self.headers_delay = 2 if phase == 'waiting_headers' else 0
            self.body_delay = 2 if phase == 'reading_body' else 0
            with self.subTest(phase=phase), self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.call(.8)
            safe = error_diagnostic(caught.exception, 'transport')
            self.assertEqual(safe['jev_timing']['phase'], phase)
            self.assertIn('connected_at_ms', safe['jev_timing'])
            if phase == 'waiting_headers':
                self.assertNotIn('headers_at_ms', safe['jev_timing'])
            self.assertNotIn(PRIVATE, json.dumps(safe))

    def test_deadline_before_helper_entry_is_explicitly_unobserved(self):
        self.preload.write_text('await new Promise(resolve=>setTimeout(resolve,5000));\n')
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.call(.4)
        timing = error_diagnostic(caught.exception, 'transport')['jev_timing']
        self.assertEqual(timing['phase'], 'helper_start')
        self.assertFalse(timing['observed'])
        self.assertNotIn('connected_at_ms', timing)


if __name__ == '__main__':
    unittest.main()
