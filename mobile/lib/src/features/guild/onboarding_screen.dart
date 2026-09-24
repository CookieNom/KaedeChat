import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';

final onboardingChannelsProvider =
    StateProvider.family<List<String>?, String>((ref, guild) => null);
final showAllOnboardingChannelsProvider =
    StateProvider.family<bool, String>((ref, guild) => false);

enum OnboardingDestination { welcome, guide, customize }

typedef OnboardingJson = Map<String, Object?>;
OnboardingJson _object(Object? value) =>
    value is Map ? Map<String, Object?>.from(value) : {};
List<OnboardingJson> _objects(Object? value) =>
    value is List ? value.map(_object).toList() : [];
List<String> _strings(Object? value) =>
    value is List ? value.whereType<String>().toList() : [];

class OnboardingEntry extends ConsumerStatefulWidget {
  const OnboardingEntry({required this.guild, super.key});
  final KaedeGuild guild;
  @override
  ConsumerState<OnboardingEntry> createState() => _OnboardingEntryState();
}

class _OnboardingEntryState extends ConsumerState<OnboardingEntry> {
  OnboardingJson? _response;
  String? _error;
  int _generation = 0;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant OnboardingEntry oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version) {
      _load();
    }
  }

  Future<void> _load() async {
    final generation = ++_generation;
    try {
      final value = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .api
          .getJson('/api/v1/guilds/${widget.guild.ref.pathSegment}/onboarding');
      if (!mounted || generation != _generation) return;
      setState(() {
        _response = value;
        final state = _object(value['state']);
        ref
            .read(onboardingChannelsProvider(widget.guild.ref.wire).notifier)
            .state = _object(value['config'])['enabled'] == true &&
                state['completed_at'] != null
            ? _strings(state['channel_ids'])
            : null;
        _error = null;
      });
      if (value['needs_rules'] == true || value['needs_onboarding'] == true) {
        await _open();
      }
    } on Object catch (error) {
      if (mounted && generation == _generation) {
        setState(() => _error = userFacingError(error,
            summary: 'Could not load server onboarding.'));
      }
    }
  }

  Future<void> _open(
      {OnboardingDestination destination =
          OnboardingDestination.welcome}) async {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) =>
            OnboardingScreen(guild: widget.guild, destination: destination)));
    if (!mounted) return;
    try {
      final value = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .api
          .getJson('/api/v1/guilds/${widget.guild.ref.pathSegment}/onboarding');
      if (mounted) setState(() => _response = value);
    } on Object {/* The entry remains available to retry. */}
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return ListTile(
          title: Text(_error!),
          trailing: ActionButton(
              kind: ActionButtonKind.text,
              onPressed: _load,
              child: const Text('Retry')));
    }
    if (_object(_response?['config'])['enabled'] != true) {
      return const SizedBox.shrink();
    }
    final pending = _response?['needs_rules'] == true ||
        _response?['needs_onboarding'] == true;
    return Column(children: [
      ListTile(
          dense: true,
          leading: const Icon(Icons.auto_awesome_outlined),
          title: Text(pending ? 'Finish joining this server' : 'Server Guide'),
          subtitle: pending
              ? const Text('Review the rules and choose your interests')
              : null,
          trailing: const Icon(Icons.chevron_right),
          onTap: () => _open(
              destination: pending
                  ? OnboardingDestination.welcome
                  : OnboardingDestination.guide)),
      if (!pending)
        ListTile(
            dense: true,
            leading: const Icon(Icons.people_outline),
            title: const Text('Channels & Roles'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => _open(destination: OnboardingDestination.customize)),
    ]);
  }
}

class OnboardingScreen extends ConsumerStatefulWidget {
  const OnboardingScreen(
      {required this.guild,
      this.admin = false,
      this.destination = OnboardingDestination.welcome,
      super.key});
  final KaedeGuild guild;
  final bool admin;
  final OnboardingDestination destination;
  @override
  ConsumerState<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends ConsumerState<OnboardingScreen> {
  OnboardingJson? _response;
  OnboardingJson _config = {};
  Map<String, List<String>> _answers = {};
  List<String> _tasks = [];
  List<String> _extraChannels = [];
  bool _accepted = false;
  bool _busy = false;
  bool _preview = false;
  int _step = 0;
  int _editorSection = 0;
  String? _error;
  String get _path =>
      '/api/v1/guilds/${widget.guild.ref.pathSegment}/onboarding';
  String _savedConfig = '', _savedChoices = '';
  String get _choices =>
      jsonEncode([_answers, _tasks, _extraChannels, _accepted]);
  bool get _hasChanges => _editing
      ? jsonEncode(_config) != _savedConfig
      : _response?['needs_rules'] == true ||
          _response?['needs_onboarding'] == true ||
          _choices != _savedChoices;
  bool get _editing => widget.admin && !_preview;
  List<OnboardingJson> get _questions => _objects(_config['questions'])
      .where((q) =>
          _response?['needs_onboarding'] != true ||
          q['before_join'] == true ||
          _preview)
      .toList();
  List<String> get _rules => _strings(_config['rules']);
  int get _total => 2 + (_rules.isEmpty ? 0 : 1) + _questions.length;
  OnboardingJson? get _question {
    final index = _step - 1 - (_rules.isEmpty ? 0 : 1);
    return index >= 0 && index < _questions.length ? _questions[index] : null;
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final response = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .api
          .getJson(_path);
      if (!mounted) return;
      final firstLoad = _response == null;
      setState(() {
        _response = response;
        _config = _object(response['config']);
        if (firstLoad &&
            response['needs_rules'] != true &&
            response['needs_onboarding'] != true) {
          _step = switch (widget.destination) {
            OnboardingDestination.guide => _total - 1,
            OnboardingDestination.customize => 1 + (_rules.isEmpty ? 0 : 1),
            OnboardingDestination.welcome => 0,
          };
        }
        final choiceState = _object(response['state']);
        if (choiceState['completed_at'] != null) {
          ref
              .read(onboardingChannelsProvider(widget.guild.ref.wire).notifier)
              .state = _strings(choiceState['channel_ids']);
        }
        final state = _object(response['state']);
        _answers = _object(state['answers'])
            .map((key, value) => MapEntry(key, _strings(value)));
        _tasks = _strings(state['completed_tasks']);
        _extraChannels = _strings(state['extra_channel_ids']);
        _accepted = response['needs_rules'] != true;
        _savedConfig = jsonEncode(_config);
        _savedChoices = _choices;
        _error = null;
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error =
            userFacingError(error, summary: 'Could not load onboarding.'));
      }
    }
  }

  Future<bool> _save({bool close = true}) async {
    if (_busy || (!_preview && !_hasChanges)) return false;
    if (_preview) {
      setState(() {
        _preview = false;
        _step = 0;
      });
      return false;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final submittedConfig = jsonEncode(_config);
    final submittedChoices = _choices;
    try {
      final response = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .api
          .sendJson('PUT', _editing ? _path : '$_path/@me',
              data: _editing
                  ? jsonDecode(submittedConfig)
                  : {
                      'revision': _config['revision'],
                      'accept_rules': _accepted,
                      'answers': _answers,
                      'extra_channel_ids': _extraChannels,
                      'completed_tasks': _tasks
                    });
      if (!mounted) return false;
      setState(() {
        _response = response;
        if (jsonEncode(_config) == submittedConfig) {
          _config = _object(response['config']);
        } else {
          _config['revision'] = _object(response['config'])['revision'];
        }
        final choiceState = _object(response['state']);
        if (choiceState['completed_at'] != null) {
          ref
              .read(onboardingChannelsProvider(widget.guild.ref.wire).notifier)
              .state = _strings(choiceState['channel_ids']);
        }
      });
      _savedConfig = jsonEncode(_object(response['config']));
      _savedChoices = submittedChoices;
      if (!_editing) {
        final controller = ref.read(mobileControllerProvider.notifier);
        final guild = await controller.repository.guild(widget.guild.ref);
        await controller.selectGuild(guild);
        if (!mounted) return true;
      }
      if (!_editing && close && !_hasChanges) {
        Navigator.of(context).pop();
      } else {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('Onboarding saved.')));
      }
      return true;
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error =
            userFacingError(error, summary: 'Could not save onboarding.'));
      }
      return false;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _field(String label, String value, ValueChanged<String> change,
          {int lines = 1, int max = 200}) =>
      Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: TextFormField(
              initialValue: value,
              maxLines: lines,
              maxLength: max,
              decoration: InputDecoration(
                  labelText: label, border: const OutlineInputBorder()),
              onChanged: change));
  Widget _heading(String title, String description) => Padding(
      padding: const EdgeInsets.only(top: 20, bottom: 12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(title,
            style: Theme.of(context)
                .textTheme
                .headlineSmall
                ?.copyWith(fontWeight: FontWeight.w700)),
        const SizedBox(height: 6),
        Text(description,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
                height: 1.5))
      ]));
  Future<void> _pick(String title, List<String> selected,
      Map<String, String> options, ValueChanged<List<String>> changed) async {
    final result = await showDialog<List<String>>(
        context: context,
        builder: (context) {
          final values = selected.toSet();
          return StatefulBuilder(
              builder: (context, update) => AlertDialog(
                      title: Text(title),
                      content: SizedBox(
                          width: 360,
                          child: ListView(shrinkWrap: true, children: [
                            for (final option in options.entries)
                              CheckboxListTile(
                                  title: Text(option.value),
                                  value: values.contains(option.key),
                                  onChanged: (checked) => update(() {
                                        if (checked == true) {
                                          values.add(option.key);
                                        } else {
                                          values.remove(option.key);
                                        }
                                      }))
                          ])),
                      actions: [
                        ActionButton(
                            kind: ActionButtonKind.text,
                            onPressed: () => Navigator.pop(context),
                            child: const Text('Cancel')),
                        ActionButton(
                            onPressed: () =>
                                Navigator.pop(context, values.toList()),
                            child: const Text('Done'))
                      ]));
        });
    if (result != null && mounted) setState(() => changed(result));
  }

  Map<String, String> get _channelChoices => {
        for (final c in widget.guild.channels
            .where((c) => !c.isThread && c.type != ChannelType.category))
          c.ref.wire: '# ${c.name}'
      };
  String _id() => DateTime.now().microsecondsSinceEpoch.toString();
  Widget _editor() {
    final questions = _objects(_config['questions']);
    final guide = _objects(_config['guide']);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _heading('A warm welcome, a clear start',
          'Preview the experience before publishing it to your community.'),
      SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: const Text('Enable server onboarding'),
          value: _config['enabled'] == true,
          onChanged: (value) => setState(() => _config['enabled'] = value)),
      const SizedBox(height: 16),
      LayoutBuilder(
          builder: (context, constraints) => GridView.count(
                crossAxisCount: constraints.maxWidth >= 520 ? 4 : 2,
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                childAspectRatio: 3.6,
                mainAxisSpacing: 8,
                crossAxisSpacing: 8,
                children: [
                  for (final entry in [
                    'Rules',
                    'Channels',
                    'Questions',
                    'Server guide'
                  ].asMap().entries)
                    ChoiceChip(
                        showCheckmark: false,
                        label: Text('${entry.key + 1}. ${entry.value}'),
                        selected: _editorSection == entry.key,
                        onSelected: (_) =>
                            setState(() => _editorSection = entry.key))
                ],
              )),
      if (_editorSection == 0) ...[
        _heading('Server rules',
            'Members must accept these before participating. Changed rules require acceptance again.'),
        for (var i = 0; i < _rules.length; i++)
          Row(key: ValueKey('rule-$i-${_rules.length}'), children: [
            Expanded(
                child: _field('Rule ${i + 1}', _rules[i], (v) {
              final rules = _rules;
              rules[i] = v;
              _config['rules'] = rules;
            }, lines: 2, max: 1000)),
            ActionButton(
                kind: ActionButtonKind.icon,
                tooltip: 'Remove rule',
                onPressed: () => setState(() {
                      final rules = _rules..removeAt(i);
                      _config['rules'] = rules;
                    }),
                icon: const Icon(Icons.delete_outline))
          ]),
        ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: _rules.length >= 16
                ? null
                : () => setState(() => _config['rules'] = [..._rules, '']),
            icon: const Icon(Icons.add),
            label: const Text('Add a rule')),
      ],
      if (_editorSection == 1) ...[
        _heading('Default channels',
            'Choose the places every new member should start with.'),
        ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: () => _pick(
                'Default channels',
                _strings(_config['default_channel_ids']),
                _channelChoices,
                (v) => _config['default_channel_ids'] = v),
            child: Text(
                '${_strings(_config['default_channel_ids']).length} channels selected')),
      ],
      if (_editorSection == 2) ...[
        _heading('Customization questions',
            'Let people choose their interests with questions and answers.'),
        for (var i = 0; i < questions.length; i++)
          Card(
              key: ValueKey(questions[i]['id']),
              child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: _questionEditor(questions, i))),
        ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: questions.length >= 12
                ? null
                : () => setState(() {
                      _config['questions'] = [
                        ...questions,
                        {
                          'id': _id(),
                          'title': '',
                          'description': '',
                          'required': false,
                          'multiple': false,
                          'before_join': true,
                          'options': [
                            {
                              'id': 'first',
                              'title': '',
                              'description': '',
                              'emoji': '',
                              'role_ids': <String>[],
                              'channel_ids': <String>[]
                            }
                          ]
                        }
                      ];
                    }),
            icon: const Icon(Icons.add),
            label: const Text('Add a question')),
      ],
      if (_editorSection == 3) ...[
        _heading('Server guide', 'Add useful resources and first steps.'),
        _field('Welcome message', _config['welcome'] as String? ?? '',
            (v) => _config['welcome'] = v,
            lines: 3, max: 1000),
        for (var i = 0; i < guide.length; i++)
          Card(
              key: ValueKey(guide[i]['id']),
              child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(children: [
                    _field('Title', guide[i]['title'] as String? ?? '', (v) {
                      guide[i]['title'] = v;
                      _config['guide'] = guide;
                    }, max: 100),
                    _field(
                        'Description', guide[i]['description'] as String? ?? '',
                        (v) {
                      guide[i]['description'] = v;
                      _config['guide'] = guide;
                    }, max: 300),
                    DropdownButtonFormField<String>(
                        initialValue: guide[i]['kind'] as String? ?? 'task',
                        decoration: const InputDecoration(labelText: 'Type'),
                        items: const [
                          DropdownMenuItem(
                              value: 'task', child: Text('New member task')),
                          DropdownMenuItem(
                              value: 'resource', child: Text('Resource'))
                        ],
                        onChanged: (v) {
                          guide[i]['kind'] = v;
                          _config['guide'] = guide;
                        }),
                    DropdownButtonFormField<String>(
                        initialValue:
                            _channelChoices.containsKey(guide[i]['channel_id'])
                                ? guide[i]['channel_id'] as String
                                : '',
                        decoration: const InputDecoration(labelText: 'Channel'),
                        isExpanded: true,
                        items: [
                          const DropdownMenuItem(
                              value: '', child: Text('No channel')),
                          for (final c in _channelChoices.entries)
                            DropdownMenuItem(
                                value: c.key,
                                child: Text(c.value,
                                    overflow: TextOverflow.ellipsis))
                        ],
                        onChanged: (v) {
                          guide[i]['channel_id'] = v == '' ? null : v;
                          _config['guide'] = guide;
                        }),
                    ActionButton(
                        kind: ActionButtonKind.text,
                        onPressed: () => setState(() {
                              guide.removeAt(i);
                              _config['guide'] = guide;
                            }),
                        child: const Text('Remove guide item')),
                  ]))),
        ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: guide.length >= 20
                ? null
                : () => setState(() => _config['guide'] = [
                      ...guide,
                      {
                        'id': _id(),
                        'title': '',
                        'description': '',
                        'channel_id': null,
                        'kind': 'task'
                      }
                    ]),
            icon: const Icon(Icons.add),
            label: const Text('Add a guide item')),
      ],
    ]);
  }

  Widget _questionEditor(List<OnboardingJson> questions, int index) {
    final q = questions[index];
    final options = _objects(q['options']);
    void save() {
      _config['questions'] = questions;
    }

    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Row(children: [
        Expanded(
            child: Text('Question ${index + 1}',
                style: Theme.of(context).textTheme.titleMedium)),
        ActionButton(
            kind: ActionButtonKind.icon,
            tooltip: 'Move up',
            onPressed: index == 0
                ? null
                : () => setState(() {
                      final old = questions[index - 1];
                      questions[index - 1] = q;
                      questions[index] = old;
                      save();
                    }),
            icon: const Icon(Icons.arrow_upward)),
        ActionButton(
            kind: ActionButtonKind.icon,
            tooltip: 'Remove question',
            onPressed: () => setState(() {
                  questions.removeAt(index);
                  save();
                }),
            icon: const Icon(Icons.delete_outline))
      ]),
      _field('Question', q['title'] as String? ?? '', (v) {
        q['title'] = v;
        save();
      }),
      _field('Description', q['description'] as String? ?? '', (v) {
        q['description'] = v;
        save();
      }, max: 500),
      for (final flag in {
        'required': 'Required',
        'multiple': 'Allow multiple answers',
        'before_join': 'Ask new members'
      }.entries)
        CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(flag.value),
            value: q[flag.key] == true,
            onChanged: (v) => setState(() {
                  q[flag.key] = v;
                  save();
                })),
      for (var i = 0; i < options.length; i++)
        ExpansionTile(
            key: ValueKey(options[i]['id']),
            title: Text(options[i]['title'] as String? ?? 'Answer ${i + 1}'),
            initiallyExpanded: true,
            children: [
              _field('Answer', options[i]['title'] as String? ?? '', (v) {
                options[i]['title'] = v;
                q['options'] = options;
                save();
              }, max: 100),
              _field('Emoji', options[i]['emoji'] as String? ?? '', (v) {
                options[i]['emoji'] = v;
                q['options'] = options;
                save();
              }, max: 16),
              _field('Description', options[i]['description'] as String? ?? '',
                  (v) {
                options[i]['description'] = v;
                q['options'] = options;
                save();
              }, max: 300),
              ActionButton(
                  kind: ActionButtonKind.outlined,
                  onPressed: () => _pick(
                          'Channels for this answer',
                          _strings(options[i]['channel_ids']),
                          _channelChoices, (v) {
                        options[i]['channel_ids'] = v;
                        q['options'] = options;
                        save();
                      }),
                  child: Text(
                      '${_strings(options[i]['channel_ids']).length} channels')),
              ActionButton(
                  kind: ActionButtonKind.outlined,
                  onPressed: () => _pick('Participation roles',
                          _strings(options[i]['role_ids']), {
                        for (final role in widget.guild.roles.where((r) =>
                            r.ref.id != widget.guild.ref.id && !r.managed))
                          role.ref.wire: role.name
                      }, (v) {
                        options[i]['role_ids'] = v;
                        q['options'] = options;
                        save();
                      }),
                  child:
                      Text('${_strings(options[i]['role_ids']).length} roles')),
              const Text(
                  'Self-selected roles cannot grant moderation or administration permissions.'),
              ActionButton(
                  kind: ActionButtonKind.text,
                  onPressed: options.length <= 1
                      ? null
                      : () => setState(() {
                            options.removeAt(i);
                            q['options'] = options;
                            save();
                          }),
                  child: const Text('Remove answer')),
            ]),
      ActionButton(
          kind: ActionButtonKind.outlined,
          onPressed: options.length >= 20
              ? null
              : () => setState(() {
                    options.add({
                      'id': _id(),
                      'title': '',
                      'description': '',
                      'emoji': '',
                      'role_ids': <String>[],
                      'channel_ids': <String>[]
                    });
                    q['options'] = options;
                    save();
                  }),
          child: const Text('Add an answer')),
    ]);
  }

  Widget _member() {
    final question = _question;
    if (_step == 0) {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Container(
            width: double.infinity,
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(20),
                gradient: LinearGradient(colors: [
                  Theme.of(context).colorScheme.primaryContainer,
                  Theme.of(context).colorScheme.surfaceContainerHigh
                ], begin: Alignment.topLeft, end: Alignment.bottomRight)),
            child: Row(children: [
              GuildIcon(guild: widget.guild, size: 64),
              const SizedBox(width: 16),
              Expanded(
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                    Text('YOUR NEW COMMUNITY',
                        style: Theme.of(context).textTheme.labelSmall),
                    const SizedBox(height: 6),
                    Text(widget.guild.name,
                        style: Theme.of(context)
                            .textTheme
                            .titleLarge
                            ?.copyWith(fontWeight: FontWeight.bold))
                  ]))
            ])),
        _heading('Welcome to ${widget.guild.name}',
            _config['welcome'] as String? ?? ''),
        const SizedBox(height: 24),
        const Card(
            child: Padding(
                padding: EdgeInsets.all(20),
                child: ListTile(
                    leading: Icon(Icons.people_outline),
                    title: Text('Make this place yours'),
                    subtitle: Text(
                        'Review the rules, choose your interests, and find your first conversation.'))))
      ]);
    }
    if (_rules.isNotEmpty && _step == 1) {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(Icons.verified_user_outlined,
            size: 40, color: Theme.of(context).colorScheme.primary),
        _heading('A few ground rules',
            'Help keep ${widget.guild.name} welcoming for everyone.'),
        for (var i = 0; i < _rules.length; i++)
          Card(
              child: ListTile(
                  leading: CircleAvatar(
                      radius: 16,
                      backgroundColor:
                          Theme.of(context).colorScheme.surfaceContainerHighest,
                      child: Text('${i + 1}',
                          style: Theme.of(context).textTheme.labelMedium)),
                  title: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 12),
                      child: Text(_rules[i])))),
        const SizedBox(height: 16),
        CheckboxListTile(
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
                side: BorderSide(
                    color: _accepted
                        ? Theme.of(context).colorScheme.primary
                        : Theme.of(context).dividerColor)),
            controlAffinity: ListTileControlAffinity.leading,
            title: const Text('I have read and agree to the server rules.'),
            value: _accepted,
            onChanged: (v) => setState(() => _accepted = v == true))
      ]);
    }
    if (question != null) {
      final id = question['id'] as String;
      final selected = _answers[id] ?? [];
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        _heading(question['title'] as String? ?? '',
            question['description'] as String? ?? ''),
        Text(
            '${question['multiple'] == true ? 'Choose all that apply' : 'Choose one'} · ${question['required'] == true ? 'Required' : 'Optional'}'),
        const SizedBox(height: 16),
        for (final option in _objects(question['options']))
          Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Semantics(
                  selected: selected.contains(option['id']),
                  child: ActionButton(
                      kind: ActionButtonKind.outlined,
                      style: OutlinedButton.styleFrom(
                          padding: const EdgeInsets.all(20),
                          side: BorderSide(
                              width: 2,
                              color: selected.contains(option['id'])
                                  ? Theme.of(context).colorScheme.primary
                                  : Theme.of(context).dividerColor),
                          backgroundColor: selected.contains(option['id'])
                              ? Theme.of(context).colorScheme.primaryContainer
                              : null),
                      onPressed: () => setState(() {
                            final value = option['id'] as String;
                            _answers[id] = question['multiple'] == true
                                ? (selected.contains(value)
                                    ? selected.where((v) => v != value).toList()
                                    : [...selected, value])
                                : [value];
                          }),
                      child: Row(children: [
                        if ((option['emoji'] as String? ?? '').isNotEmpty)
                          Text(option['emoji'] as String,
                              style: const TextStyle(fontSize: 30))
                        else
                          Icon(Icons.people_outline,
                              size: 28,
                              color: Theme.of(context).colorScheme.primary),
                        const SizedBox(width: 16),
                        Expanded(
                            child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                              Text(option['title'] as String? ?? '',
                                  style: const TextStyle(
                                      fontWeight: FontWeight.bold)),
                              if ((option['description'] as String? ?? '')
                                  .isNotEmpty)
                                Text(option['description'] as String)
                            ])),
                        Icon(selected.contains(option['id'])
                            ? Icons.check_circle
                            : question['multiple'] == true
                                ? Icons.check_box_outline_blank
                                : Icons.radio_button_unchecked)
                      ]))))
      ]);
    }
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _heading('Server Guide',
          'Here are a few good places to start. You can revisit this guide anytime.'),
      if (_objects(_config['guide']).any((item) => item['kind'] == 'task')) ...[
        const SizedBox(height: 12),
        Row(children: [
          const Expanded(
              child: Text('Get started',
                  style: TextStyle(fontWeight: FontWeight.bold))),
          Text(
              '${_objects(_config['guide']).where((item) => item['kind'] == 'task' && _tasks.contains(item['id'])).length} of ${_objects(_config['guide']).where((item) => item['kind'] == 'task').length} complete',
              style: Theme.of(context).textTheme.labelMedium)
        ]),
        const SizedBox(height: 12),
        LinearProgressIndicator(
            value: _objects(_config['guide'])
                    .where((item) =>
                        item['kind'] == 'task' && _tasks.contains(item['id']))
                    .length /
                _objects(_config['guide'])
                    .where((item) => item['kind'] == 'task')
                    .length,
            borderRadius: BorderRadius.circular(4)),
        const SizedBox(height: 20),
      ],
      ActionButton(
          kind: ActionButtonKind.outlined,
          onPressed: () => _pick('Browse more channels', _extraChannels,
              _channelChoices, (values) => _extraChannels = values),
          child: const Text('Browse more channels')),
      for (final item in _objects(_config['guide']))
        Card(
            child: Column(children: [
          ListTile(
              leading: item['kind'] == 'task'
                  ? Checkbox(
                      value: _tasks.contains(item['id']),
                      onChanged: (v) => setState(() {
                            if (v == true) {
                              _tasks.add(item['id'] as String);
                            } else {
                              _tasks.remove(item['id']);
                            }
                          }))
                  : const Icon(Icons.menu_book_outlined),
              title: Text(item['title'] as String? ?? '',
                  style: const TextStyle(fontWeight: FontWeight.w600)),
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              subtitle: Text(item['description'] as String? ?? '')),
          if (_channelChoices.containsKey(item['channel_id']))
            ActionButton(
                kind: ActionButtonKind.text,
                onPressed: _preview
                    ? null
                    : () async {
                        final channel = widget.guild.channels.firstWhere(
                            (c) => c.ref.wire == item['channel_id']);
                        if (await _save(close: false) && mounted) {
                          await ref
                              .read(mobileControllerProvider.notifier)
                              .selectChannel(channel);
                          if (mounted) Navigator.pop(context);
                        }
                      },
                child: const Text('Open channel →'))
        ])),
      if (_objects(_config['guide']).isEmpty)
        const Text('Say hello in a channel that catches your eye.')
    ]);
  }

  @override
  Widget build(BuildContext context) {
    final q = _question;
    final canContinue = _rules.isNotEmpty && _step == 1
        ? _accepted
        : q?['required'] != true || (_answers[q?['id']]?.isNotEmpty ?? false);
    return Scaffold(
        appBar: AppBar(
            title: Text(_editing
                ? 'Rules & onboarding'
                : _preview
                    ? 'Member preview'
                    : widget.guild.name),
            actions: [
              if (_editing && _response?['can_manage'] == true)
                ActionButton(
                    kind: ActionButtonKind.text,
                    onPressed: () => setState(() {
                          _config = _object(jsonDecode(jsonEncode(_config)));
                          _preview = true;
                          _step = 0;
                          _answers = {};
                          _accepted = false;
                        }),
                    child: const Text('Preview'))
            ]),
        body: _response == null
            ? Center(
                child: _error == null
                    ? const CircularProgressIndicator()
                    : Column(mainAxisSize: MainAxisSize.min, children: [
                        Text(_error!),
                        ActionButton(
                            kind: ActionButtonKind.text,
                            onPressed: _load,
                            child: const Text('Retry'))
                      ]))
            : SafeArea(
                child: Column(children: [
                if (!_editing)
                  Padding(
                      padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
                      child: LinearProgressIndicator(
                          value: (_step + 1) / _total,
                          borderRadius: BorderRadius.circular(4))),
                Expanded(
                    child: SingleChildScrollView(
                        padding: const EdgeInsets.all(20),
                        child: Center(
                            child: ConstrainedBox(
                                constraints:
                                    const BoxConstraints(maxWidth: 720),
                                child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.stretch,
                                    children: [
                                      if (_error != null)
                                        Padding(
                                            padding: const EdgeInsets.only(
                                                bottom: 16),
                                            child: Text(_error!,
                                                style: TextStyle(
                                                    color: Theme.of(context)
                                                        .colorScheme
                                                        .error))),
                                      _editing
                                          ? (_response?['can_manage'] == true
                                              ? _editor()
                                              : const Text(
                                                  'You need Manage Server and Manage Roles permissions.'))
                                          : _member()
                                    ]))))),
                Padding(
                    padding: const EdgeInsets.all(16),
                    child: Row(children: [
                      if (!_editing)
                        ActionButton(
                            kind: ActionButtonKind.outlined,
                            onPressed: _step == 0 || _busy
                                ? null
                                : () => setState(() => _step--),
                            child: const Text('Back')),
                      const Spacer(),
                      if (!_editing)
                        Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 12),
                            child: Text('${_step + 1} / $_total')),
                      ActionButton(
                          onPressed: _busy ||
                                  (!_editing && !canContinue) ||
                                  (_editing &&
                                      _response?['can_manage'] != true) ||
                                  (!_preview &&
                                      (_editing || _step == _total - 1) &&
                                      !_hasChanges)
                              ? null
                              : () {
                                  if (!_editing && _step < _total - 1) {
                                    setState(() => _step++);
                                  } else {
                                    _save();
                                  }
                                },
                          child: Text(_busy
                              ? 'Saving…'
                              : _editing
                                  ? 'Save onboarding'
                                  : _step < _total - 1
                                      ? 'Continue'
                                      : _preview
                                          ? 'Finish preview'
                                          : 'Save & enter'))
                    ]))
              ])));
  }
}
