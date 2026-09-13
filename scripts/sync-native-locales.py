#!/usr/bin/env python3
"""Generate Android/iOS advertised languages from the Flutter catalogs."""
import argparse
import json
import plistlib
from pathlib import Path
from xml.sax.saxutils import quoteattr

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
languages = sorted(json.loads(path.read_text())['@@locale'].replace('_', '-')
                   for path in (root / 'mobile/lib/l10n').glob('app_*.arb'))
android = root / 'mobile/android/app/src/main/res/xml/locales_config.xml'
xml = '<?xml version="1.0" encoding="utf-8"?>\n<locale-config xmlns:android="http://schemas.android.com/apk/res/android">\n'
xml += ''.join(f'    <locale android:name={quoteattr(locale)} />\n' for locale in languages)
xml += '</locale-config>\n'
ios = root / 'mobile/ios/Runner/Info.plist'
plist = plistlib.loads(ios.read_bytes())
if args.check:
    assert android.exists() and android.read_text() == xml, 'Run python3 scripts/sync-native-locales.py'
    assert plist.get('CFBundleLocalizations') == languages, 'Run python3 scripts/sync-native-locales.py'
else:
    android.parent.mkdir(parents=True, exist_ok=True)
    android.write_text(xml)
    # Preserve the existing plist formatting outside the language list.
    import re
    text = ios.read_text()
    section = '<key>CFBundleLocalizations</key>\n\t<array>\n' + ''.join(f'\t\t<string>{locale}</string>\n' for locale in languages) + '\t</array>'
    if 'CFBundleLocalizations' in text:
        text = re.sub(r'<key>CFBundleLocalizations</key>\s*<array>.*?</array>', section, text, flags=re.S)
    else:
        text = text.replace('<dict>', '<dict>\n\t' + section, 1)
    ios.write_text(text)
print('Native app languages: ' + ', '.join(languages))
