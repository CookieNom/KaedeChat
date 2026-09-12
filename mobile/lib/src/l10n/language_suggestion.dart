import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/l10n/generated/app_localizations.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

class LanguageSuggestion extends ConsumerStatefulWidget {
  const LanguageSuggestion({required this.child, super.key});
  final Widget child;
  @override
  ConsumerState<LanguageSuggestion> createState() => _LanguageSuggestionState();
}

class _LanguageSuggestionState extends ConsumerState<LanguageSuggestion>
    with WidgetsBindingObserver {
  bool _busy = false;
  bool _error = false;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    languageChoiceHandled.addListener(_choiceChanged);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    languageChoiceHandled.removeListener(_choiceChanged);
    super.dispose();
  }

  @override
  void didChangeLocales(List<Locale>? locales) {
    if (mounted) setState(() {});
  }

  void _choiceChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _accept() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = false;
    });
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      final updated =
          await controller.repository.updateSettings({'locale': 'system'});
      if (!mounted) return;
      controller.applySettings(updated);
      await setAppLanguage('system', explicit: true);
    } on Object {
      if (mounted) setState(() => _error = true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final phase =
        ref.watch(mobileControllerProvider.select((state) => state.phase));
    final candidate = suggestedLanguage(
      WidgetsBinding.instance.platformDispatcher.locales,
      Localizations.localeOf(context),
      languageChoiceHandled.value,
    );
    if (candidate == null || phase != SessionPhase.ready) return widget.child;
    final english = lookupAppLocalizations(const Locale('en'));
    final native = lookupAppLocalizations(candidate);
    final name = native.language_name;
    return Stack(children: [
      widget.child,
      Positioned(
          top: 12,
          left: 16,
          right: 16,
          child: SafeArea(
            child: Align(
                alignment: Alignment.topCenter,
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                      maxWidth: 440,
                      maxHeight: MediaQuery.sizeOf(context).height * .7),
                  child: Card(
                      child: SingleChildScrollView(
                          padding: const EdgeInsets.all(20),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(english.language_suggestion(name),
                                  locale: const Locale('en'),
                                  textDirection: TextDirection.ltr),
                              const SizedBox(height: 12),
                              Text(native.language_suggestion(name),
                                  locale: candidate),
                              if (_error) ...[
                                const SizedBox(height: 12),
                                Text(
                                    '${english.language_save_error}\n${native.language_save_error}',
                                    style: TextStyle(
                                        color: Theme.of(context)
                                            .colorScheme
                                            .error)),
                              ],
                              const SizedBox(height: 12),
                              Row(
                                  mainAxisAlignment: MainAxisAlignment.end,
                                  children: [
                                    IconButton.filled(
                                      tooltip:
                                          '${english.language_decline} / ${native.language_decline}',
                                      style: IconButton.styleFrom(
                                          backgroundColor:
                                              const Color(0xffb42318),
                                          foregroundColor: Colors.white,
                                          minimumSize: const Size(48, 48)),
                                      onPressed: _busy
                                          ? null
                                          : () async {
                                              await dismissLanguageSuggestion();
                                              if (mounted) setState(() {});
                                            },
                                      icon: const Icon(Icons.close),
                                    ),
                                    const SizedBox(width: 12),
                                    IconButton.filled(
                                      tooltip:
                                          '${english.language_accept(name)} / ${native.language_accept(name)}',
                                      style: IconButton.styleFrom(
                                          backgroundColor:
                                              const Color(0xff157347),
                                          foregroundColor: Colors.white,
                                          minimumSize: const Size(48, 48)),
                                      onPressed: _busy ? null : _accept,
                                      icon: const Icon(Icons.check),
                                    ),
                                  ]),
                            ],
                          ))),
                )),
          )),
    ]);
  }
}
