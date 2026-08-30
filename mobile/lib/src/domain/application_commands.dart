import 'dart:convert';

import 'package:kaede_mobile/src/core/refs.dart';

typedef CommandComposerValues = Map<String, Object?>;

final class MobileApplicationCommandChoice {
  const MobileApplicationCommandChoice({
    required this.name,
    required this.value,
    this.nameLocalizations = const <String, String>{},
  });

  factory MobileApplicationCommandChoice.fromJson(
    Map<String, Object?> json,
  ) =>
      MobileApplicationCommandChoice(
        name: '${json['name'] ?? ''}',
        nameLocalizations: _localizations(json['name_localizations']),
        value: json['value'] is num ? json['value']! : '${json['value'] ?? ''}',
      );

  final String name;
  final Map<String, String> nameLocalizations;
  final Object value;

  String displayName(String locale) =>
      mobileLocalizedCommandText(name, nameLocalizations, locale);
}

List<MobileApplicationCommandChoice> mobileAutocompleteChoices(Object? value) {
  final items = _commandObjectArray(value, 'autocomplete choices');
  return List.unmodifiable(items.map((item) {
    final name = item['name'];
    final choice = item['value'];
    if (name is! String ||
        (choice is! String && choice is! num) ||
        choice is num && !choice.isFinite) {
      throw const FormatException(
        'Application command autocomplete choice is invalid.',
      );
    }
    return MobileApplicationCommandChoice(name: name, value: choice as Object);
  }));
}

final class MobileApplicationCommand {
  const MobileApplicationCommand({
    required this.id,
    required this.application,
    required this.applicationName,
    required this.name,
    required this.type,
    required this.description,
    required this.integrationType,
    required this.interactionContext,
    this.dmCapabilityId,
    this.dmCapabilityRevision,
    this.nameLocalizations = const <String, String>{},
    this.descriptionLocalizations = const <String, String>{},
    this.options = const <MobileApplicationCommandOption>[],
  });

  factory MobileApplicationCommand.fromJson(Map<String, Object?> json) {
    final integrationType = switch (json['integration_type']) {
      'guild_install' => 'guild_install',
      'user_install' => 'user_install',
      'dm_capability' => 'dm_capability',
      _ => throw const FormatException(
          'Application command is missing its effective installation type.',
        ),
    };
    final capabilityId = json['dm_capability_id'];
    final capabilityRevision = json['dm_capability_revision'];
    final capabilityBound = integrationType == 'dm_capability';
    if (capabilityBound !=
            (capabilityId is String &&
                RegExp(r'^kbdg_[A-Za-z0-9_-]{43}$').hasMatch(capabilityId) &&
                capabilityRevision is String &&
                RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(capabilityRevision) &&
                BigInt.parse(capabilityRevision) <=
                    BigInt.parse('9223372036854775807')) ||
        !capabilityBound && capabilityId != null ||
        !capabilityBound && capabilityRevision != null) {
      throw const FormatException(
        'Application command DM capability lineage is invalid.',
      );
    }
    return MobileApplicationCommand(
      id: _requiredCommandId(json['id']),
      application: EntityRef.parse('${json['application_ref']}'),
      applicationName: '${json['application_name'] ?? 'App'}',
      integrationType: integrationType,
      dmCapabilityId: capabilityId as String?,
      dmCapabilityRevision: capabilityRevision as String?,
      interactionContext: switch (json['interaction_context']) {
        'guild' => 'guild',
        'bot_dm' => 'bot_dm',
        'private_channel' => 'private_channel',
        _ => throw const FormatException(
            'Application command is missing its effective interaction context.',
          ),
      },
      name: '${json['name']}',
      nameLocalizations: _localizations(json['name_localizations']),
      type: '${json['type']}',
      description: '${json['description'] ?? ''}'.trim(),
      descriptionLocalizations:
          _localizations(json['description_localizations']),
      options: _commandObjectArray(json['options'], 'options')
          .map(
            MobileApplicationCommandOption.fromJson,
          )
          .toList(growable: false),
    );
  }

  final String id;
  final EntityRef application;
  final String applicationName;
  final String integrationType;
  final String interactionContext;
  final String? dmCapabilityId;
  final String? dmCapabilityRevision;
  final String name;
  final Map<String, String> nameLocalizations;
  final String type;
  final String description;
  final Map<String, String> descriptionLocalizations;
  final List<MobileApplicationCommandOption> options;

  String displayName(String locale) =>
      mobileLocalizedCommandText(name, nameLocalizations, locale);
  String displayDescription(String locale) => mobileLocalizedCommandText(
        description,
        descriptionLocalizations,
        locale,
      );

  MobileApplicationCommandOption? optionAt(String path) {
    var options = this.options;
    MobileApplicationCommandOption? found;
    for (final segment in path.split('.')) {
      found = options.where((item) => item.name == segment).firstOrNull;
      if (found == null) return null;
      options = found.options;
    }
    return found;
  }
}

/// USE_APPLICATION_COMMANDS gates guild installs, not external user installs.
bool mobileApplicationIntegrationAllowedByUsePermission(
  String? integrationType,
  bool canUseGuildCommands,
) =>
    canUseGuildCommands ||
    integrationType == 'user_install' ||
    integrationType == 'dm_capability';

bool mobileApplicationCommandAllowedByUsePermission(
  MobileApplicationCommand command,
  bool canUseGuildCommands,
) =>
    mobileApplicationIntegrationAllowedByUsePermission(
      command.integrationType,
      canUseGuildCommands,
    );

bool mobileApplicationCommandAllowedByChannelPermissions(
  MobileApplicationCommand command,
  bool canUseGuildCommands,
  bool canSendUserCommands,
) =>
    mobileApplicationCommandAllowedByUsePermission(
      command,
      canUseGuildCommands,
    ) &&
    (command.type != 'user' || canSendUserCommands);

List<MobileApplicationCommand> mobileChatInputCommandMatches(
  Iterable<MobileApplicationCommand> commands,
  String invokedName,
  String locale,
) =>
    commands
        .where((command) =>
            command.type == 'chat_input' &&
            (command.name == invokedName ||
                command.displayName(locale) == invokedName))
        .toList(growable: false);

final class MobileApplicationCommandGroup {
  const MobileApplicationCommandGroup({
    required this.application,
    required this.applicationName,
    required this.commands,
  });

  final EntityRef application;
  final String applicationName;
  final List<MobileApplicationCommand> commands;
}

final class MobileContextCommandMenuModel {
  const MobileContextCommandMenuModel({
    required this.frequent,
    required this.groups,
  });

  final List<MobileApplicationCommand> frequent;
  final List<MobileApplicationCommandGroup> groups;
}

const _contextCommandHistoryLimit = 100;
const _frequentContextCommandLimit = 5;

String mobileContextCommandUsageKey(MobileApplicationCommand command) =>
    jsonEncode(<String>[
      command.application.wire,
      command.id,
      command.type,
      command.integrationType,
      command.interactionContext,
    ]);

String mobileContextCommandHistoryStorageKey(EntityRef account) =>
    'context-command-history-v1:${account.wire}';

List<String> mobileRememberContextCommand(
  Iterable<String> history,
  MobileApplicationCommand command,
) {
  final retained = history.where((item) => item.length <= 512).toList();
  retained.add(mobileContextCommandUsageKey(command));
  if (retained.length > _contextCommandHistoryLimit) {
    retained.removeRange(0, retained.length - _contextCommandHistoryLimit);
  }
  return List.unmodifiable(retained);
}

List<MobileApplicationCommandGroup> mobileApplicationCommandGroups(
  Iterable<MobileApplicationCommand> commands,
  String query,
  String locale, {
  Set<String> commandTypes = const <String>{'chat_input'},
}) {
  final needle = query.trim().toLowerCase();
  final grouped = <String, List<MobileApplicationCommand>>{};
  for (final command in commands.where(
    (item) => commandTypes.contains(item.type),
  )) {
    final search = <String>[
      command.applicationName,
      command.displayName(locale),
      command.name,
      command.displayDescription(locale),
    ].join(' ').toLowerCase();
    if (needle.isNotEmpty && !search.contains(needle)) continue;
    grouped.putIfAbsent('${command.application}', () => []).add(command);
  }
  final groups = grouped.values.map((groupCommands) {
    groupCommands.sort((left, right) =>
        left.displayName(locale).compareTo(right.displayName(locale)));
    final first = groupCommands.first;
    return MobileApplicationCommandGroup(
      application: first.application,
      applicationName: first.applicationName,
      commands: List.unmodifiable(groupCommands),
    );
  }).toList();
  groups.sort((left, right) =>
      left.applicationName.compareTo(right.applicationName) != 0
          ? left.applicationName.compareTo(right.applicationName)
          : '${left.application}'.compareTo('${right.application}'));
  return List.unmodifiable(groups);
}

List<MobileApplicationCommandGroup> mobileApplicationCommandLauncherGroups(
  Iterable<MobileApplicationCommand> commands,
  String query,
  String locale,
) =>
    mobileApplicationCommandGroups(commands, query, locale);

List<MobileApplicationCommandGroup> mobileContextCommandGroups(
  Iterable<MobileApplicationCommand> commands,
  String query,
  String locale,
) =>
    mobileApplicationCommandGroups(
      commands,
      query,
      locale,
      commandTypes: const <String>{'message', 'user'},
    );

MobileContextCommandMenuModel mobileContextCommandMenuModel(
  Iterable<MobileApplicationCommand> commands,
  String query,
  String locale,
  Iterable<String> history,
) {
  final groups = mobileContextCommandGroups(commands, query, locale);
  final matching = groups.expand((group) => group.commands).toList();
  final metrics = <String, ({int count, int last})>{};
  final boundedHistory =
      history.where((item) => item.length <= 512).toList(growable: false);
  final start = boundedHistory.length > _contextCommandHistoryLimit
      ? boundedHistory.length - _contextCommandHistoryLimit
      : 0;
  for (var index = start; index < boundedHistory.length; index += 1) {
    final key = boundedHistory[index];
    final current = metrics[key];
    metrics[key] = (
      count: (current?.count ?? 0) + 1,
      last: index,
    );
  }
  final frequent = matching
      .where((command) =>
          metrics.containsKey(mobileContextCommandUsageKey(command)))
      .toList()
    ..sort((left, right) {
      final leftKey = mobileContextCommandUsageKey(left);
      final rightKey = mobileContextCommandUsageKey(right);
      final leftMetric = metrics[leftKey]!;
      final rightMetric = metrics[rightKey]!;
      final count = rightMetric.count.compareTo(leftMetric.count);
      if (count != 0) return count;
      final recency = rightMetric.last.compareTo(leftMetric.last);
      return recency != 0 ? recency : leftKey.compareTo(rightKey);
    });
  final hoisted = frequent.take(_frequentContextCommandLimit).toList();
  final hoistedKeys = hoisted.map(mobileContextCommandUsageKey).toSet();
  final remaining = groups
      .map((group) {
        final groupCommands = group.commands
            .where((command) =>
                !hoistedKeys.contains(mobileContextCommandUsageKey(command)))
            .toList(growable: false);
        return groupCommands.isEmpty
            ? null
            : MobileApplicationCommandGroup(
                application: group.application,
                applicationName: group.applicationName,
                commands: List.unmodifiable(groupCommands),
              );
      })
      .whereType<MobileApplicationCommandGroup>()
      .toList(growable: false);
  return MobileContextCommandMenuModel(
    frequent: List.unmodifiable(hoisted),
    groups: List.unmodifiable(remaining),
  );
}

List<MobileApplicationCommand> mobileMessageContextCommands(
  Iterable<MobileApplicationCommand> commands,
) =>
    commands
        .where((command) => command.type == 'message' || command.type == 'user')
        .toList(growable: false);

List<MobileApplicationCommand> mobileUserContextCommands(
  Iterable<MobileApplicationCommand> commands,
) =>
    commands.where((command) => command.type == 'user').toList(growable: false);

EntityRef mobileContextCommandTarget(
  MobileApplicationCommand command, {
  required EntityRef user,
  EntityRef? message,
}) {
  if (command.type == 'message') {
    if (message == null) {
      throw const FormatException('A message command needs a message target.');
    }
    return message;
  }
  if (command.type != 'user') {
    throw const FormatException(
        'Only user and message commands belong in Apps menus.');
  }
  return user;
}

final class MobileApplicationCommandOption {
  const MobileApplicationCommandOption({
    required this.name,
    required this.type,
    required this.description,
    this.nameLocalizations = const <String, String>{},
    this.descriptionLocalizations = const <String, String>{},
    this.required = false,
    this.autocomplete = false,
    this.choices = const <MobileApplicationCommandChoice>[],
    this.options = const <MobileApplicationCommandOption>[],
    this.channelTypes = const <int>[],
    this.fileTypes = const <String>[],
    this.minValue,
    this.maxValue,
    this.minLength,
    this.maxLength,
  });

  factory MobileApplicationCommandOption.fromJson(
    Map<String, Object?> json,
  ) =>
      MobileApplicationCommandOption(
        name: '${json['name'] ?? ''}',
        nameLocalizations: _localizations(json['name_localizations']),
        type: '${json['type'] ?? ''}',
        description: '${json['description'] ?? ''}'.trim(),
        descriptionLocalizations:
            _localizations(json['description_localizations']),
        required: json['required'] == true,
        autocomplete: json['autocomplete'] == true,
        choices: _commandObjectArray(json['choices'], 'choices')
            .map(
              MobileApplicationCommandChoice.fromJson,
            )
            .toList(growable: false),
        options: _commandObjectArray(json['options'], 'options')
            .map(
              MobileApplicationCommandOption.fromJson,
            )
            .toList(growable: false),
        channelTypes: _commandScalarArray<int>(
          json['channel_types'],
          'channel_types',
        ),
        fileTypes: _commandScalarArray<String>(
          json['file_types'],
          'file_types',
        ).map((value) => value.toLowerCase()).toList(growable: false),
        minValue: json['min_value'] is num
            ? (json['min_value']! as num).toDouble()
            : double.tryParse('${json['min_value'] ?? ''}'),
        maxValue: json['max_value'] is num
            ? (json['max_value']! as num).toDouble()
            : double.tryParse('${json['max_value'] ?? ''}'),
        minLength: json['min_length'] is num
            ? (json['min_length']! as num).toInt()
            : int.tryParse('${json['min_length'] ?? ''}'),
        maxLength: json['max_length'] is num
            ? (json['max_length']! as num).toInt()
            : int.tryParse('${json['max_length'] ?? ''}'),
      );

  final String name;
  final Map<String, String> nameLocalizations;
  final String type;
  final String description;
  final Map<String, String> descriptionLocalizations;
  final bool required;
  final bool autocomplete;
  final List<MobileApplicationCommandChoice> choices;
  final List<MobileApplicationCommandOption> options;
  final List<int> channelTypes;
  final List<String> fileTypes;
  final double? minValue;
  final double? maxValue;
  final int? minLength;
  final int? maxLength;

  bool get isContainer => type == 'subcommand' || type == 'subcommand_group';

  String displayName(String locale) =>
      mobileLocalizedCommandText(name, nameLocalizations, locale);
  String displayDescription(String locale) => mobileLocalizedCommandText(
        description,
        descriptionLocalizations,
        locale,
      );
}

bool mobileCommandFileMatches(
  MobileApplicationCommandOption option,
  String filename,
  String contentType,
) =>
    mobileFileMatchesCommandTypes(option.fileTypes, filename, contentType);

bool mobileFileMatchesCommandTypes(
  List<String> fileTypes,
  String filename,
  String contentType,
) {
  if (fileTypes.isEmpty) return true;
  final name = filename.toLowerCase();
  final mediaType = contentType.toLowerCase().split('/').first;
  return fileTypes.any((filter) =>
      const {'image', 'video', 'audio'}.contains(filter)
          ? mediaType == filter
          : name.endsWith(filter));
}

Map<String, String> _localizations(Object? value) => value is Map
    ? Map<String, String>.unmodifiable(value.map(
        (key, item) => MapEntry('$key', '$item'),
      ))
    : const <String, String>{};

List<Map<String, Object?>> _commandObjectArray(
  Object? value,
  String field,
) {
  if (value == null) return const <Map<String, Object?>>[];
  if (value is! List) {
    throw FormatException('Application command $field must be an array.');
  }
  final result = <Map<String, Object?>>[];
  for (final item in value) {
    if (item is! Map) {
      throw FormatException(
        'Application command $field may contain only objects.',
      );
    }
    final parsed = <String, Object?>{};
    for (final entry in item.entries) {
      if (entry.key is! String) {
        throw FormatException(
          'Application command $field object keys must be strings.',
        );
      }
      parsed[entry.key as String] = entry.value;
    }
    result.add(Map.unmodifiable(parsed));
  }
  return List.unmodifiable(result);
}

List<T> _commandScalarArray<T>(Object? value, String field) {
  if (value == null) return const [];
  if (value is! List || value.any((item) => item is! T)) {
    throw FormatException(
      'Application command $field contains an invalid value.',
    );
  }
  return List<T>.unmodifiable(value.cast<T>());
}

String _requiredCommandId(Object? value) {
  final rendered = value == null ? '' : '$value'.trim();
  if (rendered.isEmpty) {
    throw const FormatException(
        'Application command is missing its stable id.');
  }
  return rendered;
}

String mobileLocalizedCommandText(
  String fallback,
  Map<String, String> localizations,
  String locale,
) {
  final normalized = locale.trim();
  final candidates = <String>[normalized];
  if (normalized == 'en-US') {
    candidates.add('en-GB');
  } else if (normalized == 'en-GB') {
    candidates.add('en-US');
  } else if (normalized == 'es-419') {
    candidates.add('es-ES');
  }
  final language = normalized.split('-').first;
  if (language.isNotEmpty && !candidates.contains(language)) {
    candidates.add(language);
  }
  for (final candidate in candidates) {
    final localized = localizations[candidate]?.trim();
    if (localized?.isNotEmpty == true) return localized!;
  }
  return fallback;
}

final class MobileCommandOptionField {
  const MobileCommandOptionField({required this.option, required this.path});

  final MobileApplicationCommandOption option;
  final String path;
}

final class MobileCommandOptionSelector {
  const MobileCommandOptionSelector({
    required this.path,
    required this.label,
    required this.options,
    required this.selected,
  });

  final String path;
  final String label;
  final List<MobileApplicationCommandOption> options;
  final String selected;
}

final class MobileCommandComposerModel {
  const MobileCommandComposerModel({
    required this.selectors,
    required this.fields,
  });

  final List<MobileCommandOptionSelector> selectors;
  final List<MobileCommandOptionField> fields;
}

const _containerPrefix = r'$container:';

String mobileCommandContainerKey(Iterable<String> path) =>
    '$_containerPrefix${path.join('.')}';

MobileCommandComposerModel mobileCommandComposerModel(
  List<MobileApplicationCommandOption> options,
  CommandComposerValues values,
) {
  final selectors = <MobileCommandOptionSelector>[];
  final path = <String>[];
  var current = options;
  for (var depth = 0; depth < 2; depth += 1) {
    final containers = current.where((option) => option.isContainer).toList();
    if (containers.isEmpty) break;
    final key = mobileCommandContainerKey(path);
    final rawSelected = values[key];
    final selected = rawSelected is String ? rawSelected : '';
    selectors.add(MobileCommandOptionSelector(
      path: key,
      label: depth == 0
          ? containers.any((option) => option.type == 'subcommand_group')
              ? 'Group or command'
              : 'Command'
          : 'Subcommand',
      options: List.unmodifiable(containers),
      selected: selected,
    ));
    final choice =
        containers.where((option) => option.name == selected).firstOrNull;
    if (choice == null) {
      return MobileCommandComposerModel(
        selectors: List.unmodifiable(selectors),
        fields: const <MobileCommandOptionField>[],
      );
    }
    path.add(choice.name);
    current = choice.options;
  }
  return MobileCommandComposerModel(
    selectors: List.unmodifiable(selectors),
    fields: List.unmodifiable(
      current.where((option) => !option.isContainer).map(
            (option) => MobileCommandOptionField(
              option: option,
              path: <String>[...path, option.name].join('.'),
            ),
          ),
    ),
  );
}

Map<String, String> mobileCommandOptionErrors(
  MobileApplicationCommand command,
  CommandComposerValues values,
) {
  final errors = <String, String>{};
  final model = mobileCommandComposerModel(command.options, values);
  for (final selector in model.selectors) {
    if (selector.selected.isEmpty) {
      errors[selector.path] = 'Choose a ${selector.label.toLowerCase()}.';
    }
  }
  for (final field in model.fields) {
    final option = field.option;
    final value = values[field.path];
    final missing = value == null || (value is String && value.trim().isEmpty);
    if (missing) {
      if (option.required) errors[field.path] = '${option.name} is required.';
      continue;
    }
    if (option.type == 'boolean') {
      if (value is! bool) errors[field.path] = 'Choose true or false.';
      continue;
    }
    if (option.type == 'integer' || option.type == 'number') {
      final parsed =
          value is num ? value.toDouble() : double.tryParse('$value');
      if (parsed == null || !parsed.isFinite) {
        errors[field.path] = 'Enter a valid number.';
        continue;
      }
      if (option.type == 'integer' && parsed != parsed.truncateToDouble()) {
        errors[field.path] = 'Enter a whole number.';
        continue;
      }
      if (option.type == 'integer' && parsed.abs() > 9007199254740991) {
        errors[field.path] = 'Enter a whole number within the supported range.';
        continue;
      }
      final minimum = option.minValue ?? -9007199254740992;
      final maximum = option.maxValue ?? 9007199254740992;
      if (parsed < minimum) {
        errors[field.path] = 'Enter ${_numberLabel(minimum)} or more.';
        continue;
      }
      if (parsed > maximum) {
        errors[field.path] = 'Enter ${_numberLabel(maximum)} or less.';
        continue;
      }
    }
    if (option.type == 'string') {
      final text = '$value'.trim();
      if (option.minLength case final minimum? when text.length < minimum) {
        errors[field.path] = 'Enter at least $minimum characters.';
        continue;
      }
      final maximum = option.maxLength ?? 6000;
      if (text.length > maximum) {
        errors[field.path] = 'Enter no more than $maximum characters.';
        continue;
      }
    }
    if (option.choices.isNotEmpty &&
        !option.choices.any((choice) => '$value' == '${choice.value}')) {
      errors[field.path] = 'Choose one of the available values.';
    }
  }

  final attachmentValues = <String>{};
  for (final field
      in model.fields.where((field) => field.option.type == 'attachment')) {
    final value = values[field.path];
    if (value is String && value.isNotEmpty && !attachmentValues.add(value)) {
      errors[field.path] =
          'Choose a different file for each attachment option.';
    }
  }
  return errors;
}

bool mobileCommandOptionsComplete(
  MobileApplicationCommand command,
  CommandComposerValues values,
) =>
    mobileCommandOptionErrors(command, values).isEmpty;

Map<String, Object?> mobileCommandOptionPayload(
  MobileApplicationCommand command,
  CommandComposerValues values,
) {
  final model = mobileCommandComposerModel(command.options, values);
  Map<String, Object?> leaf = <String, Object?>{};
  for (final field in model.fields) {
    final raw = values[field.path];
    if (raw == null || (raw is String && raw.trim().isEmpty)) continue;
    switch (field.option.type) {
      case 'boolean':
        if (raw is bool) leaf[field.option.name] = raw;
      case 'integer':
        final parsed = raw is num ? raw.toInt() : int.tryParse('$raw');
        if (parsed != null) leaf[field.option.name] = parsed;
      case 'number':
        final parsed = raw is num ? raw.toDouble() : double.tryParse('$raw');
        if (parsed != null && parsed.isFinite) leaf[field.option.name] = parsed;
      default:
        final text = '$raw'.trim();
        if (text.isNotEmpty) leaf[field.option.name] = text;
    }
  }
  for (final selector in model.selectors.reversed) {
    if (selector.selected.isNotEmpty) {
      leaf = <String, Object?>{selector.selected: leaf};
    }
  }
  return leaf;
}

CommandComposerValues mobileCommandValueChanged(
  CommandComposerValues values,
  String path,
  Object? value,
) =>
    <String, Object?>{...values, path: value};

Set<String> mobileCommandAttachmentKeys(
  MobileApplicationCommand command,
  CommandComposerValues values,
) =>
    mobileCommandComposerModel(command.options, values)
        .fields
        .where((field) => field.option.type == 'attachment')
        .map((field) => values[field.path])
        .whereType<String>()
        .where((value) => value.isNotEmpty)
        .toSet();

bool mobileCommandOptionAllowsChannelType(
  MobileApplicationCommandOption option,
  int channelType,
) =>
    option.type == 'channel' &&
    (option.channelTypes.isEmpty || option.channelTypes.contains(channelType));

String _numberLabel(double value) => value == value.truncateToDouble()
    ? value.toInt().toString()
    : value.toString();
