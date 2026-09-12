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

English is the fallback for missing translations. The initial Japanese catalog
includes the language controls, suggestion, and common interface labels; it is
**not a complete Japanese translation**. The remaining English source messages
are available for translators in Weblate. Do not copy English strings into a
target catalog merely to inflate its completion percentage.

## Connect the hosted project

The hosted `kaede-chat` project now has `frontend` and linked `mobile`
components. During implementation, the frontend tracks
`codex/weblate-localization`; after merging that branch, change its repository
branch to `main` in Weblate. Mobile inherits that branch.

For the final GitHub connection, register an App in Manage → Code-hosting
connections, install it for `CookieNom/KaedeChat`, and connect it to the project's
workspace. Use Weblate's **Migrate to GitHub App** action for the existing
frontend component; mobile retains its linked repository. This enables App
authentication, incoming push notifications, and translation pull requests.
Keep the existing SSH deploy key until App access has been verified, then remove
the deploy key from GitHub if no component still uses it.

The catalogs must first exist on a branch accessible to Weblate. From the
repository root, preview the exact configuration, then apply it after pushing:

```sh
python3 scripts/configure-weblate.py --branch main
python3 scripts/configure-weblate.py --branch main --apply
```

The apply command prompts for your Weblate administrator **API token**, found
in your Weblate profile. Alternatively supply `WEBLATE_API_TOKEN` through your
local environment. Do not put credentials in this repository. The script creates
missing resources and preserves existing ones. It does not push your code or
configure GitHub credentials.

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

The linked mobile repository prevents independent working copies from creating
conflicting translation commits. Keep English source editing in the code review
workflow. The API bootstrap uses plain Git and the service's SSH deploy key to
push into `weblate-translations`; grant that key write access before enabling
delivery. It can import the public repository before App registration. Migrate
to the GitHub App as described above for automatic pull requests and incoming
notifications. A plain Git/SSH setup requires a separate GitHub push webhook
pointing to `https://weblate.kaede.chat/hooks/github/` and manual PR creation.

Review and merge translation PRs after CI passes. Web deployments receive the
new catalog on deployment; desktop/mobile receive it in their next app release.

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
