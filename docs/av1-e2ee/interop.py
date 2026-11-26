"""Browser/native RTP matrix; requires a local SFU and a hardware-capable test host."""

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SDK = Path(os.environ.get('LIVEKIT_JS_DIST', str(ROOT.parents[1] / 'frontend/node_modules/livekit-client/dist')))


def jwt(room, identity):
    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    data = enc({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + enc({
        'iss': 'devkey', 'sub': identity, 'exp': int(time.time()) + 600,
        'video': {'roomJoin': True, 'room': room},
    })
    return (data + b'.' + base64.urlsafe_b64encode(hmac.new(b'secret', data, hashlib.sha256).digest()).rstrip(b'=')).decode()


class Handler(http.server.BaseHTTPRequestHandler):
    page = ROOT / 'interop.html'

    def do_GET(self):
        file = {'/': self.page, '/sdk.mjs': SDK / 'livekit-client.esm.mjs',
                '/worker.mjs': SDK / 'livekit-client.e2ee.worker.mjs'}.get(self.path)
        if file is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/javascript')
        self.end_headers()
        self.wfile.write(file.read_bytes())

    def log_message(self, *_args):
        pass


def start_page(browser, address, options):
    page = browser.new_page(permissions=['camera'])
    page.on('pageerror', lambda error: print('browser error:', error, flush=True))
    page.goto(address)
    page.wait_for_function('!!window.start')
    page.evaluate('(options) => window.start(options)', options)
    return page


def assert_decoded(page, codec):
    page.wait_for_function('window.decoded?.frames > 0', timeout=30000)
    result = page.evaluate('window.decoded')
    assert result['codec'].lower() == f'video/{codec}', result
    if codec == 'h265':
        assert result.get('hardware') is True and result.get('decoder'), result
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codecs', nargs='+', choices=['av1', 'h265'], default=['av1', 'h265'])
    parser.add_argument('--modes', nargs='+', choices=['L1T1', 'L1T3', 'L3T3_KEY'], default=['L1T1'])
    parser.add_argument('--headed', action='store_true', help='Run a visible browser for hardware drivers that need it')
    args = parser.parse_args()
    native_binary = os.environ['LIVEKIT_INTEROP_TEST_BINARY']
    url = os.environ.get('LIVEKIT_URL', 'ws://127.0.0.1:17880')
    assert urlparse(url).hostname in ('localhost', '127.0.0.1', '::1'), 'Use a local development SFU'
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),
                headless=not args.headed,
                args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--use-fake-device-for-media-stream'],
            )
            for codec in args.codecs:
                for mode in args.modes:
                    if codec == 'h265' and mode.startswith('L3'):
                        continue
                    for encrypted in (False, True):
                        for native_role in ('publisher', 'receiver'):
                            for late in (False, True):
                                name = f'{codec}-{mode}-{encrypted}-{native_role}-{late}'
                                room = 'codec-interop-' + uuid.uuid4().hex
                                env = dict(os.environ, INTEROP_ROOM=room, LIVEKIT_URL=url, RUST_LOG='info',
                                           INTEROP_ROLE=native_role, INTEROP_CODEC=codec,
                                           INTEROP_ENCRYPTED=str(int(encrypted)), INTEROP_SCALABILITY_MODE=mode,
                                           INTEROP_LATE_VP8=str(int(late)))
                                options = dict(url=url, token=jwt(room, 'browser'), codec=codec, mode=mode,
                                               encrypted=encrypted, late=late,
                                               role='receiver' if native_role == 'publisher' else 'publisher')
                                pages = []
                                with tempfile.NamedTemporaryFile(prefix=f'kaede-{name}-', suffix='.log', delete=False) as log:
                                    native = subprocess.Popen([native_binary, 'browser_encryption_interop', '--ignored', '--nocapture'],
                                                              stdout=log, stderr=subprocess.STDOUT, env=env)
                                    try:
                                        page = start_page(browser, address, options)
                                        pages.append(page)
                                        if native_role == 'publisher':
                                            before = assert_decoded(page, codec)
                                            if late:
                                                viewer = start_page(browser, address, dict(options, token=jwt(room, 'vp8-browser'), codec='vp8', late=False))
                                                pages.append(viewer)
                                                assert_decoded(viewer, 'vp8')
                                                page.wait_for_function('(previous) => window.decoded.frames > previous', arg=before['frames'])
                                        else:
                                            page.wait_for_function('window.encoded?.frames > 0', timeout=30000)
                                            actual = page.evaluate('window.encoded')
                                            assert actual['codec'].lower() == f'video/{codec}', actual
                                            assert actual['mode'] == mode, actual
                                            if codec == 'h265':
                                                assert actual.get('hardware') is True and actual.get('encoder'), actual
                                        deadline = time.monotonic() + 40
                                        while native.poll() is None and time.monotonic() < deadline:
                                            page.wait_for_timeout(100)
                                        assert native.wait(timeout=1) == 0, f'{name}: see {log.name}'
                                        print(name, 'PASS', flush=True)
                                    finally:
                                        if native.poll() is None:
                                            native.terminate()
                                            native.wait(timeout=5)
                                        for page in pages:
                                            page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
