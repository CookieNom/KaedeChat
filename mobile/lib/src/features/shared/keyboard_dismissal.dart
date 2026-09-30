import 'package:flutter/material.dart';

/// Enable touch dismissal through Flutter's text-field tap regions, so editing
/// and controls explicitly grouped with a field keep their normal focus behavior.
class KeyboardDismissal extends StatelessWidget {
  const KeyboardDismissal({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) => Actions(
        actions: {
          EditableTextTapOutsideIntent:
              CallbackAction<EditableTextTapOutsideIntent>(
            onInvoke: (intent) {
              intent.focusNode.unfocus();
              return null;
            },
          ),
        },
        child: child,
      );
}
