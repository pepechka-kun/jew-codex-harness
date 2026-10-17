"""Provider identity belongs to the external gateway, not the local CLI."""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import jev_server as jev


class GatewayClientIdentity(unittest.TestCase):
    def handler(self, headers):
        handler = object.__new__(jev.Handler)
        handler._attempts = []
        handler._response_started = False
        handler.wfile = io.BytesIO()
        handler.headers = headers
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def test_external_identity_is_independent_of_caller_headers_and_local_cli(self):
        sent = []

        class Response:
            status = 200
            def getheader(self, name):
                return 'application/json' if name == 'Content-Type' else None
            def read(self, *args):
                return b'{"id":"fixture","status":"completed","output":[]}'

        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, body, headers):
                sent.append((method, path, json.loads(body), headers))
            def getresponse(self): return Response()
            def close(self): pass

        cases = (
            {}, {'Version': '0.1.0'}, {'vErSiOn': '4.5.6'},
            {'User-Agent': 'codex_cli_rs/0.1.0 (fixture)'},
            {'user-agent': 'codex_exec/8.9.0 (fixture)'},
            {'Version': '0.1.0', 'User-Agent': 'codex-router/1.2.3',
             'Authorization': 'Bearer caller-private', 'X-OmniRoute-Connection': 'caller-account'},
        )
        payload = {'model': 'gateway/model', 'instructions': 'fixture',
                   'input': [{'role': 'user', 'content': 'canonical fixture'}],
                   'reasoning': {'effort': 'low'}, 'prompt_cache_key': 'fixture-session'}
        for headers in cases:
            for account in (None, 'selected-account'):
                with self.subTest(headers=headers, account=account), \
                     mock.patch.object(jev.http.client, 'HTTPConnection', Connection), \
                     mock.patch.dict(jev.os.environ, {'CODEX_CLIENT_VERSION': '0.2.0',
                                                      'CODEX_BIN': '/nonexistent/old-cli'}), \
                     mock.patch.object(jev.subprocess, 'run') as cli:
                    handler = self.handler(headers)
                    status, *_ = handler._forward(payload, '/gateway/v1/responses', False,
                        False, '', 'gateway/model', external={'host': 'localhost', 'port': 1,
                        'key': 'gateway-private', 'account': account})
                    self.assertEqual(status, 200)
                    cli.assert_not_called()
                    method, path, body, outgoing = sent[-1]
                    self.assertEqual((method, path, body), ('POST', '/gateway/v1/responses', payload))
                    expected = {'Content-Type': 'application/json',
                                'Accept': 'text/event-stream', 'Authorization': 'Bearer gateway-private'}
                    if account:
                        expected['X-OmniRoute-Connection'] = account
                    self.assertEqual(outgoing, expected)

    def test_gateway_default_survives_stale_version_over_real_http(self):
        seen = []
        configured_version = '7.8.9'

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                version = self.headers.get('Version', configured_version)
                seen.append((version, self.headers.get('User-Agent'), body))
                data = json.dumps({'id': 'fixture', 'status': 'completed', 'output': [],
                                   'configured_version': version}).encode()
                self.send_response(200 if version == configured_version else 400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = gateway.server_address
            # Control: the gateway really honors an explicit stale header.
            control = jev.http.client.HTTPConnection(host, port, timeout=3)
            try:
                control.request('POST', '/v1/responses', json.dumps({'model': 'gateway/model'}),
                                {'Version': '0.1.0', 'Content-Type': 'application/json'})
                response = control.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                control.close()
            payload = {'model': 'gateway/model', 'input': 'canonical fixture'}
            for headers in ({'Version': '0.1.0'}, {'User-Agent': 'codex_cli_rs/0.1.0'}):
                with self.subTest(headers=headers):
                    handler = self.handler(headers)
                    status, *_ = handler._forward(payload, '/v1/responses', False, False,
                        '', 'gateway/model', external={'host': host, 'port': port, 'key': 'fixture'})
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(handler.wfile.getvalue())['configured_version'],
                                     configured_version)
                    self.assertEqual(seen[-1], (configured_version, None, payload))
        finally:
            gateway.shutdown()
            gateway.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
