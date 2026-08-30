import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/domain/permission_selection.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

Future<BigInt?> showApplicationPermissionPicker(
  BuildContext context, {
  required BigInt selected,
  String title = 'Default permissions',
}) =>
    showModalBottomSheet<BigInt>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => _ApplicationPermissionPicker(
        initial: selected,
        title: title,
      ),
    );

final class _ApplicationPermissionPicker extends StatefulWidget {
  const _ApplicationPermissionPicker({
    required this.initial,
    required this.title,
  });

  final BigInt initial;
  final String title;

  @override
  State<_ApplicationPermissionPicker> createState() =>
      _ApplicationPermissionPickerState();
}

final class _ApplicationPermissionPickerState
    extends State<_ApplicationPermissionPicker> {
  final _search = TextEditingController();
  late BigInt _selected;

  @override
  void initState() {
    super.initState();
    _selected = widget.initial;
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim().toLowerCase();
    final visible = applicationInstallPermissions
        .where((item) =>
            query.isEmpty ||
            item.label.toLowerCase().contains(query) ||
            item.description.toLowerCase().contains(query) ||
            item.group.toLowerCase().contains(query))
        .toList(growable: false);
    final groups = visible.map((item) => item.group).toSet();
    final selectedCount = applicationInstallPermissions
        .where((item) => applicationPermissionSelected(_selected, item))
        .length;
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: .9,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 12, 10),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          widget.title,
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                        Text(
                          '$selectedCount selected · exact permission mask preserved',
                          style: TextStyle(color: context.kaede.muted),
                        ),
                      ],
                    ),
                  ),
                  FilledButton(
                    onPressed: () => Navigator.pop(context, _selected),
                    child: const Text('Done'),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                controller: _search,
                decoration: const InputDecoration(
                  prefixIcon: Icon(Icons.search_rounded),
                  hintText: 'Search permissions',
                ),
                onChanged: (_) => setState(() {}),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: ListView(
                children: [
                  for (final group in groups) ...[
                    Padding(
                      padding: const EdgeInsets.fromLTRB(20, 14, 20, 4),
                      child: Text(
                        group.toUpperCase(),
                        style: TextStyle(
                          color: context.kaede.muted,
                          fontSize: 12,
                          fontWeight: FontWeight.w800,
                          letterSpacing: .5,
                        ),
                      ),
                    ),
                    for (final item
                        in visible.where((item) => item.group == group))
                      CheckboxListTile(
                        value: applicationPermissionSelected(_selected, item),
                        title: Text(item.label),
                        subtitle: Text(item.description),
                        secondary: _PermissionRiskIcon(item: item),
                        onChanged: (selected) => setState(() {
                          _selected = setApplicationPermission(
                            _selected,
                            item,
                            selected ?? false,
                          );
                        }),
                      ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

final class _PermissionRiskIcon extends StatelessWidget {
  const _PermissionRiskIcon({required this.item});

  final PermissionMetadata item;

  @override
  Widget build(BuildContext context) {
    final dangerous = item.danger == PermissionDanger.dangerous ||
        item.danger == PermissionDanger.critical;
    return Icon(
      dangerous ? Icons.warning_amber_rounded : Icons.key_rounded,
      color: dangerous ? context.kaede.danger : context.kaede.muted,
    );
  }
}
