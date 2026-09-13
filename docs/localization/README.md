# Translating KaedeChat with Weblate

Kaede loads translations bundled with the application. It does not contact
Weblate while people use chat. Web and desktop share the frontend JSON catalog;
Flutter uses ARB catalogs and its generated localization classes.

## Language behavior

Settings → Language changes the interface immediately and saves the account
preference. Choose a specific language or System default. The latter remains
`system` in storage and is resolved independently on each device. Existing
accounts retain English until they choose another language.

The native desktop shell reads OS language preferences through `sys-locale`;
the browser uses `navigator.languages`; Flutter uses platform locales. A
supported non-English **first** device language can trigger a suggestion after
account settings load. The pane includes English and the suggested language,
with an accessible red ✕ to decline and green ✓ to accept. English-first devices
never see this pane, even if another language appears later in their preference
list. Already-selected languages, unsupported languages, and dismissed offers
also do not produce a pane. Accepting chooses System default. Dismissal is
remembered on that browser/installation; it does not alter the account language.

English is the fallback for missing translations. Translation progress and
review status are tracked in Weblate.

## Translation workflow

Translate and review messages in the
[KaedeChat project](https://weblate.kaede.chat/projects/kaede-chat/).
Weblate uses the KaedeChat GitHub App for repository access, source updates,
and translation pull requests. Web and desktop share the frontend component;
mobile has its own component linked to the same repository.

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

The linked mobile component shares the frontend component's repository and
source branch. English source changes belong in application code reviews;
translation changes return through Weblate pull requests. Review the translated
text and CI results before merging. Web deployments receive new translations
on deployment; desktop and mobile receive them in their next app release.

The repository branch configured in Weblate must contain these catalogs. When
an integration branch is merged, update the frontend component's source branch
to the maintained application branch; mobile inherits the change.

## Add another language

1. Add the language to both Weblate components, using the corresponding language
   code: for example `fr.json` and `app_fr.arb` (ARB includes `"@@locale": "fr"`).
2. Translate `language_name` into its native name and translate
   `language_suggestion`, `language_accept`, `language_decline`, and
   `language_save_error`. These are required before a language is released so
   the bilingual offer can actually be read in that language.
3. Translate other messages as available. Leave missing keys absent rather than
   writing empty strings; CI rejects empty text to avoid blank Flutter labels.
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

Commit the catalogs and updated Android/iOS language metadata. Flutter-generated
Dart files are ignored and rebuilt by `make mobile-check` and Flutter builds.
The web selector discovers JSON files; the mobile selector uses generated
`supportedLocales`. No per-language switch statement or manually maintained
language list is needed. `sync-native-locales.py` advertises those languages to
Android's per-app language settings and iOS.

## Add or edit interface text

Use a descriptive stable key in the English source catalog, then reference it
with `$t('key', { name })` in Svelte or `L10n.of(context).key(name)` in Flutter.
The migration's `ui_…` keys include a readable prefix and stable content hash;
keep existing keys when revising copy so Weblate can flag translations for review.
ARB source entries include descriptions and typed placeholder metadata. Widgets
use `L10n.of(context)` so language changes rebuild their text without replacing
routes or clearing drafts. `L10n.current` is available for callbacks outside a
widget context; never translate protocol values, identifiers, or user content.

Use ICU message syntax for placeholders and pluralization rather than joining
translated sentence fragments. For example:

```json
{
  "member_count": "{count, plural, one {# member} other {# members}}"
}
```

For Flutter, also give `count` a numeric placeholder type in the corresponding
`@member_count` metadata. Preserve placeholder names across translations. Quote
literal braces using ICU escaping; literal apostrophes use `''`. Translations
are rendered as text, not HTML. Source English and translation syntax and
placeholder parity are validated by `pnpm check:locales` in CI.

Longer translations and right-to-left languages need visual review before
release. Web document direction is updated for RTL languages and Flutter uses
its localization delegates for directionality. Current English/Japanese support
does not imply every existing layout has been reviewed for RTL.

Reference: [Weblate API](https://docs.weblate.org/en/latest/api.html),
[Weblate JSON](https://docs.weblate.org/en/latest/formats/json.html),
[Weblate ARB](https://docs.weblate.org/en/latest/formats/arb.html),
[Flutter localization](https://docs.flutter.dev/ui/internationalization).
