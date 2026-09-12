#!/usr/bin/env python3
"""Create KaedeChat's Weblate project/components after the catalogs are pushed."""
import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Create missing resources (default: print configuration)')
    parser.add_argument('--url', default='https://weblate.kaede.chat')
    parser.add_argument('--branch', default='main', help='Remote branch that already contains these catalogs')
    args = parser.parse_args()
    config = json.loads((Path(__file__).resolve().parents[1] / 'docs/localization/weblate.json').read_text())
    for component in config['components']:
        component['branch'] = args.branch
    if not args.apply:
        print(json.dumps(config, indent=2))
        return
    if not args.url.startswith('https://'):
        parser.error('Use HTTPS for the Weblate API')
    token = os.environ.get('WEBLATE_API_TOKEN') or getpass.getpass('Weblate admin API token: ')
    if not token.strip():
        parser.error('An API token is required')

    def api(path, payload=None):
        request = Request(args.url.rstrip('/') + '/api/' + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Authorization': 'Token ' + token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and payload is None:
                return None
            raise SystemExit(f'Weblate HTTP {error.code} at {path}. Check API permissions, repository access, and that the remote branch contains the catalog files.') from None

    project = config['project']['slug']
    if api(f'projects/{project}/') is None:
        api('projects/', config['project'])
    for component in config['components']:
        resource = f'components/{project}/{component["slug"]}/'
        existing = api(resource)
        if existing:
            if existing['filemask'] != component['filemask'] or existing['template'] != component['template']:
                raise SystemExit(f'{resource} already exists with different translation paths; review it manually.')
            print(f'Already configured: {resource}')
        else:
            api(f'projects/{project}/components/', component)
            print(f'Created: {resource}')
    print('Configure GitHub repository credentials and PR delivery in the frontend component; mobile shares its repository.')


if __name__ == '__main__':
    main()
