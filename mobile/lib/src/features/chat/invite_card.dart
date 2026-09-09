import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/features/auth/deep_link_screen.dart';

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
                  const Text('Invitation unavailable'),
                  TextButton(
                    onPressed: () => ref.invalidate(provider),
                    child: const Text('Retry'),
                  ),
                ],
              ),
              data: (preview) {
                final guild = preview['guild']! as Map;
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Text('Guild invitation'),
                    const SizedBox(height: 8),
                    Text(guild['name'] as String,
                        style: Theme.of(context).textTheme.titleMedium),
                    Text(guild['origin_domain'] as String),
                    if (guild['description'] case final String description
                        when description.isNotEmpty)
                      Text(description,
                          maxLines: 2, overflow: TextOverflow.ellipsis),
                    TextButton(
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
                      child: const Text('View invitation'),
                    ),
                  ],
                );
              },
            ),
      ),
    );
  }
}
