import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Drag a message to the left to reply to it.
///
/// The direction matters: the shell's page view uses rightward drags to return
/// to the channel list, so reply keeps the opposite direction and the two
/// gestures never compete for the same pointer.
final class SwipeToReply extends StatefulWidget {
  const SwipeToReply({
    required this.enabled,
    required this.onReply,
    required this.child,
    super.key,
  });

  final bool enabled;
  final VoidCallback onReply;
  final Widget child;

  @override
  State<SwipeToReply> createState() => _SwipeToReplyState();
}

final class _SwipeToReplyState extends State<SwipeToReply>
    with SingleTickerProviderStateMixin {
  static const _trigger = 48.0;
  static const _disarmAt = 28.0;
  static const _limit = 76.0;

  late final AnimationController _settleController = AnimationController(
    vsync: this,
    duration: Duration(milliseconds: 190),
  );
  Animation<double>? _settle;
  var _offset = 0.0;
  var _armed = false;
  int? _pointer;
  Offset? _origin;
  var _intent = _SwipeIntent.pending;

  @override
  void initState() {
    super.initState();
    _settleController.addListener(() {
      final value = _settle?.value;
      if (value != null && mounted) setState(() => _offset = value);
    });
  }

  @override
  void dispose() {
    _settleController.dispose();
    super.dispose();
  }

  @override
  void didUpdateWidget(covariant SwipeToReply oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.enabled && !widget.enabled) {
      _settleController.stop();
      _settle = null;
      _offset = 0;
      _armed = false;
      _pointer = null;
      _origin = null;
      _intent = _SwipeIntent.pending;
    }
  }

  void _pointerDown(PointerDownEvent event) {
    if (_pointer != null || event.buttons != kPrimaryButton) return;
    _settleController.stop();
    _settle = null;
    _pointer = event.pointer;
    _origin = event.position;
    _intent = _SwipeIntent.pending;
    _armed = false;
    if (_offset != 0) setState(() => _offset = 0);
  }

  void _pointerMove(PointerMoveEvent event) {
    if (event.pointer != _pointer || _origin == null) return;
    final travelled = event.position - _origin!;
    if (_intent == _SwipeIntent.pending) {
      final horizontal = travelled.dx.abs();
      final vertical = travelled.dy.abs();
      final horizontallyIntentional = horizontal >= _intentSlop &&
          horizontal >= vertical * _horizontalIntentRatio;
      final verticallyIntentional = vertical >= _intentSlop &&
          vertical * _horizontalIntentRatio > horizontal;
      if (horizontallyIntentional) {
        _intent = travelled.dx < 0 ? _SwipeIntent.reply : _SwipeIntent.rejected;
      } else if (verticallyIntentional) {
        _intent = _SwipeIntent.rejected;
      }
    }
    if (_intent != _SwipeIntent.reply) return;

    // Use total distance from touch-down rather than drag callback deltas. No
    // competing recognizer can make us miss part of the swipe this way.
    final raw = (-travelled.dx).clamp(0.0, double.infinity);
    final next = raw <= _trigger
        ? raw.clamp(0.0, _trigger)
        : (_trigger + (raw - _trigger) * .35).clamp(0.0, _limit);
    if (!_armed && next >= _trigger) {
      _armed = true;
      HapticFeedback.mediumImpact();
    } else if (_armed && next < _disarmAt) {
      // Keep a crossed threshold latched through ordinary finger jitter. A
      // deliberate drag almost all the way back still cancels the reply.
      _armed = false;
    }
    setState(() => _offset = next);
  }

  void _pointerUp(PointerUpEvent event) {
    if (event.pointer != _pointer) return;
    final submitReply = _intent == _SwipeIntent.reply;
    _clearPointer();
    _finish(submitReply: submitReply);
  }

  void _pointerCancel(PointerCancelEvent event) {
    if (event.pointer != _pointer) return;
    _clearPointer();
    _finish(submitReply: false);
  }

  void _clearPointer() {
    _pointer = null;
    _origin = null;
    _intent = _SwipeIntent.pending;
  }

  void _finish({required bool submitReply}) {
    final fire = submitReply && widget.enabled && _armed;
    _armed = false;
    if (fire) widget.onReply();
    if (_offset == 0) return;
    _settle = Tween<double>(begin: _offset, end: 0).animate(
      CurvedAnimation(parent: _settleController, curve: Curves.easeOutCubic),
    );
    _settleController
      ..value = 0
      ..forward();
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.enabled) return widget.child;
    final progress = (_offset / _trigger).clamp(0.0, 1.0);
    return Listener(
      behavior: HitTestBehavior.opaque,
      onPointerDown: _pointerDown,
      onPointerMove: _pointerMove,
      onPointerUp: _pointerUp,
      onPointerCancel: _pointerCancel,
      child: Stack(
        children: [
          if (_offset > 0)
            Positioned.fill(
              child: Align(
                alignment: Alignment.centerRight,
                child: Padding(
                  padding: EdgeInsets.only(right: 18),
                  child: Opacity(
                    opacity: progress,
                    child: Transform.scale(
                      scale: .72 + .28 * progress,
                      child: AnimatedContainer(
                        duration: Duration(milliseconds: 90),
                        width: 36,
                        height: 36,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: _armed
                              ? context.kaede.coralSoft
                              : context.kaede.rail,
                          border: Border.all(
                            color: _armed
                                ? context.kaede.coralText
                                : context.kaede.border,
                          ),
                        ),
                        child: Icon(
                          Icons.reply_rounded,
                          size: 21,
                          color: _armed
                              ? context.kaede.coralText
                              : context.kaede.muted,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          Transform.translate(
            offset: Offset(-_offset, 0),
            child: widget.child,
          ),
        ],
      ),
    );
  }
}

const _intentSlop = 10.0;
const _horizontalIntentRatio = .65;

enum _SwipeIntent { pending, reply, rejected }
