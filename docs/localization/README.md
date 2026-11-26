# Translating KaedeChat with Weblate

Translations ship inside the app, so Kaede never contacts Weblate at runtime.
Web and desktop share the frontend JSON catalog. Flutter uses ARB catalogs and
its generated localization classes.

## Language behavior

**Settings → Language** changes the interface immediately and saves the
choice to the account. Users can pick a language or System default, which is
stored as `system` and resolved separately on each device. Existing accounts
stay on English until they choose otherwise. Missing translations fall back to
English.

The desktop shell reads OS language preferences through `sys-locale`, the
browser uses `navigator.languages`, and Flutter uses platform locales. When the
device's **first** language is a supported language other than English, Kaede
offers to switch after account settings load. The offer is written in both
English and the suggested language, with an accessible red ✕ to decline and a
green ✓ to accept. Accepting selects System default. Declining is remembered on
that browser or installation and doesn't change the account language.

There's no offer if English comes first in the device's list, if the language
is already selected or unsupported, or if the user declined before.

## Translation workflow

Translate and review messages in the
[KaedeChat project](https://weblate.kaede.chat/projects/kaede-chat/), which
also tracks progress and review status. Weblate uses the KaedeChat GitHub App
for repository access, source updates, and translation pull requests. Web and
desktop share the frontend component. Mobile has its own component linked to
the same repository.

| Setting | Frontend component | Mobile component |
| --- | --- | --- |
| Project | `kaede-chat` | `kaede-chat` |
| Slug | `frontend` | `mobile` |
| File mask | `frontend/src/lib/locales/*.json` | `mobile/lib/l10n/app_*.arb` |
| Source file | `frontend/src/lib/locales/en.json` | `mobile/lib/l10n/app_en.arb` |
| Format | JSON, ICU message-format check | ARB |
| Source language | English (`en`) | English (`en`) |
| New-language template | Empty | Empty |
| Repository | `https://github.com/CookieNom/KaedeChat.git` | `weblate://kaede-chat/frontend` |

The mobile component shares the frontend component's repository and source
branch. That branch must contain these catalogs. When an integration branch is
merged, point the frontend component at the maintained branch, and mobile
follows.

English source changes go through normal code review. Translations come back as
Weblate pull requests; review the text and CI results before merging. The web
app picks up new translations when it's deployed, and desktop and mobile get
them in their next release.

## Add another language

1. Add the language to both Weblate components, using the corresponding language
   code: for example `fr.json` and `app_fr.arb` (ARB includes `"@@locale": "fr"`).
2. Translate `language_name` into the language's own name, plus
   `language_suggestion`, `language_accept`, `language_decline`, and
   `language_save_error`. A language can't ship without these, because the
   switch offer has to be readable in it.
3. Translate other messages as you can. Leave missing keys out instead of
   writing empty strings. CI rejects empty text, which would show as blank
   Flutter labels.
4. After pulling the translation branch, run:

```sh
python3 scripts/sync-native-locales.py
cd mobile
flutter pub get --enforce-lockfile
flutter gen-l10n
cd ../frontend
pnpm check:locales
pnpm check
```

Commit the catalogs and the updated Android/iOS language metadata.
Flutter-generated Dart files are ignored and rebuilt by `make mobile-check` and
Flutter builds. There's no language list to maintain: the web selector finds
the JSON files, the mobile selector uses the generated `supportedLocales`, and
`sync-native-locales.py` registers languages with Android and iOS.

## Add or edit interface text

Add a descriptive, stable key to the English source catalog, then use it with
`$t('key', { name })` in Svelte or `L10n.of(context).key(name)` in Flutter.
Older `ui_…` keys combine a readable prefix with a content hash. Keep existing
keys when you revise copy, so Weblate flags the translations for review.

ARB source entries include descriptions and typed placeholder metadata. Widgets
use `L10n.of(context)` so a language change rebuilds their text without
replacing routes or clearing drafts. Callbacks outside a widget can use
`L10n.current`. Never translate protocol values, identifiers, or user content.

Use ICU message syntax for placeholders and pluralization rather than joining
translated sentence fragments. For example:

```json
{
  "member_count": "{count, plural, one {# member} other {# members}}"
}
```

For Flutter, also give `count` a numeric placeholder type in the matching
`@member_count` metadata. Keep placeholder names the same across translations.
Escape literal braces with ICU quoting, and write literal apostrophes as `''`.
Translations render as text, not HTML. In CI, `pnpm check:locales` validates
the syntax of the English source and translations, and checks that placeholders
match.

Long translations and right-to-left languages need a visual check before
release. The web app sets document direction for RTL languages, and Flutter
takes directionality from its localization delegates. Only English and
Japanese ship today, so existing layouts haven't all been reviewed for RTL.
