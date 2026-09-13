"""Exercise provisioning without network access or credentials."""
import contextlib
import io
import json
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).with_name('configure-weblate.py')


class WeblateSetupTest(unittest.TestCase):
    def test_create_then_repeat_preserves_components(self):
        resources = {}
        created = []

        def request(req, timeout):
            path = req.full_url.split('/api/', 1)[1]
            if req.data is not None:
                payload = json.loads(req.data)
                if path == 'projects/':
                    key = f'projects/{payload["slug"]}/'
                else:
                    key = f'components/kaede-chat/{payload["slug"]}/'
                    if payload['repo'].startswith('weblate://'):
                        self.assertNotIn('branch', payload)
                    else:
                        self.assertEqual(payload['branch'], 'localization')
                resources[key] = payload
                created.append(key)
                return io.BytesIO(json.dumps(payload).encode())
            if path not in resources:
                raise HTTPError(req.full_url, 404, 'missing', {}, None)
            return io.BytesIO(json.dumps(resources[path]).encode())

        with patch('sys.argv', [str(SCRIPT), '--apply', '--branch', 'localization']), \
                patch.dict('os.environ', {'WEBLATE_API_TOKEN': 'test-only'}), \
                patch('urllib.request.urlopen', side_effect=request), \
                contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(SCRIPT), run_name='__main__')
            runpy.run_path(str(SCRIPT), run_name='__main__')
        self.assertEqual(created, [
            'projects/kaede-chat/',
            'components/kaede-chat/frontend/',
            'components/kaede-chat/mobile/',
        ])


if __name__ == '__main__':
    unittest.main()
