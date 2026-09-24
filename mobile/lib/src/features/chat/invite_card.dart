import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/features/auth/deep_link_screen.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

List<String> messageInviteReferences(String? content,
    {required bool encrypted}) {
  if (encrypted || content == null) return const [];
  final visible = content.replaceAll(RegExp(r'\|\|[\s\S]*?\|\|'), ' ');
  final references = <String>{};
  final urls = RegExp(
    r'(?:https?://[^\s<>()]+|(?:[a-z0-9-]+\.)+[a-z0-9-]+/invite/[^\s<>()]+)',
    caseSensitive: false,
  );
  for (final match in urls.allMatches(visible)) {
    final token = match[0]!.replaceFirst(RegExp(r'[.,!?;:]+$'), '');
    try {
      final uri = Uri.parse(token.contains('://') ? token : 'https://$token');
      if (uri.userInfo.isNotEmpty ||
          uri.hasPort ||
          uri.hasQuery ||
          uri.hasFragment) {
        continue;
      }
      final path = RegExp(r'^/invite/([^/]+)/?$').firstMatch(uri.path);
      if (path == null) continue;
      final domain = Domain(uri.host).value;
      if (!domain.contains('.')) continue;
      final parts = Uri.decodeComponent(path[1]!).split('@');
      if (!RegExp(r'^[A-Za-z0-9]{8}$').hasMatch(parts.first) ||
          parts.length > 2 ||
          (parts.length == 2 && Domain(parts.last).value != domain)) {
        continue;
      }
      references.add('${parts.first}@$domain');
      if (references.length == 3) break;
    } on FormatException {
      continue;
    }
  }
  return references.toList();
}

MobileDeepLink? applicationInviteLink(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.userInfo.isNotEmpty ||
      uri.hasPort ||
      uri.hasQuery ||
      uri.hasFragment ||
      !uri.host.contains('.')) {
    return null;
  }
  final link = MobileDeepLink.parse(uri);
  if (link?.kind != MobileLinkKind.applicationInstall ||
      !RegExp(r'^[a-z0-9][a-z0-9_-]{1,63}$').hasMatch(link!.templateSlug!)) {
    return null;
  }
  return link;
}

List<MobileDeepLink> messageApplicationInvites(String? content,
    {required bool encrypted}) {
  if (encrypted || content == null) return const [];
  final visible = content.replaceAll(RegExp(r'\|\|[\s\S]*?\|\|'), ' ');
  final links = <String, MobileDeepLink>{};
  for (final match in RegExp(r'https?://[^\s<>()]+').allMatches(visible)) {
    final link = applicationInviteLink(
      match[0]!.replaceFirst(RegExp(r'[.,!?;:]+$'), ''),
    );
    if (link == null) continue;
    links['${link.application!.wire}/${link.templateSlug}'] = link;
    if (links.length == 3) break;
  }
  return links.values.toList();
}

class ApplicationInviteCard extends StatelessWidget {
  const ApplicationInviteCard({super.key, required this.link});

  final MobileDeepLink link;

  @override
  Widget build(BuildContext context) => Card(
        margin: const EdgeInsets.only(top: 8),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Application invitation',
                  style: Theme.of(context).textTheme.titleMedium),
              Text(link.application!.domain.value),
              const SizedBox(height: 8),
              ActionButton(
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) =>
                        ApplicationInstallDeepLinkScreen(link: link),
                  ),
                ),
                child: Text(L10n.of(context).ui_view_invitation_1c1c650f),
              ),
            ],
          ),
        ),
      );
}

final inviteCardPreviewProvider = FutureProvider.autoDispose
    .family<Map<String, Object?>, String>((ref, reference) async {
  final preview = await ref.watch(repositoryProvider).previewInvite(reference);
  final guild = preview['guild'];
  if (guild is! Map ||
      guild['name'] is! String ||
      guild['origin_domain'] is! String) {
    throw const FormatException('Invalid invite preview');
  }
  return preview;
});

class InviteCard extends ConsumerWidget {
  const InviteCard({super.key, required this.reference});

  final String reference;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final provider = inviteCardPreviewProvider(reference);
    return Card(
      margin: const EdgeInsets.only(top: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: ref.watch(provider).when(
              loading: () => const LinearProgressIndicator(minHeight: 2),
              error: (error, stack) => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(L10n.of(context).ui_invitation_unavailable_9fc21e34),
                  ActionButton(
                    kind: ActionButtonKind.text,
                    onPressed: () => ref.invalidate(provider),
                    child: Text(L10n.of(context).ui_retry_8036af59),
                  ),
                ],
              ),
              data: (preview) {
                final guild = preview['guild']! as Map;
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(L10n.of(context).ui_guild_invitation_2a233707),
                    const SizedBox(height: 8),
                    Text(guild[L10n.of(context).ui_name_8d39bde6] as String,
                        style: Theme.of(context).textTheme.titleMedium),
                    Text(guild[L10n.of(context).ui_origin_domain_c6fbb9dc]
                        as String),
                    if (guild['description'] case final String description
                        when description.isNotEmpty)
                      Text(description,
                          maxLines: 2, overflow: TextOverflow.ellipsis),
                    ActionButton(
                      kind: ActionButtonKind.text,
                      onPressed: () =>
                          Navigator.of(context).push(MaterialPageRoute<void>(
                        builder: (_) => DeepLinkActionScreen(
                          link: MobileDeepLink(
                            kind: MobileLinkKind.invite,
                            instance: Domain(reference.split('@').last),
                            code: reference,
                          ),
                        ),
                      )),
                      child: Text(L10n.of(context).ui_view_invitation_1c1c650f),
                    ),
                  ],
                );
              },
            ),
      ),
    );
  }
}
