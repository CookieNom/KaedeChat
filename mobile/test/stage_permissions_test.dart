import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/stage_permissions.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  KaedeChannel channel(ChannelType type, int permissions) => KaedeChannel(
        ref: EntityRef.parse('13@chat.example'),
        guildRef: EntityRef.parse('1@chat.example'),
        type: type,
        position: 0,
        permissions: BigInt.from(permissions),
      );

  test('Stage lifecycle requires the complete moderator trio', () {
    expect(
      canManageStageChannel(
        channel(ChannelType.stage, stageModeratorPermissions),
      ),
      isTrue,
    );
    expect(
      canManageStageChannel(
        channel(
          ChannelType.stage,
          Permission.manageChannels | Permission.muteMembers,
        ),
      ),
      isFalse,
    );
    expect(
      canManageStageChannel(
        channel(ChannelType.stage, Permission.administrator),
      ),
      isTrue,
    );
  });

  test('scheduled-event channels use Stage and voice-specific access', () {
    expect(
      canCreateScheduledEventInChannel(
        channel(
          ChannelType.stage,
          Permission.createEvents | stageModeratorPermissions,
        ),
      ),
      isTrue,
    );
    expect(
      canCreateScheduledEventInChannel(
        channel(ChannelType.stage, Permission.createEvents),
      ),
      isFalse,
    );
    expect(
      canCreateScheduledEventInChannel(
        channel(
          ChannelType.voice,
          Permission.createEvents | Permission.viewChannel | Permission.connect,
        ),
      ),
      isTrue,
    );
  });

  test('event management keeps ownership rules plus Stage moderation', () {
    expect(
      canManageScheduledEventInChannel(
        channel(
          ChannelType.stage,
          Permission.createEvents | stageModeratorPermissions,
        ),
        ownEvent: true,
      ),
      isTrue,
    );
    expect(
      canManageScheduledEventInChannel(
        channel(
          ChannelType.stage,
          Permission.manageEvents | stageModeratorPermissions,
        ),
        ownEvent: false,
      ),
      isTrue,
    );
    expect(
      canManageScheduledEventInChannel(
        channel(ChannelType.stage, Permission.manageEvents),
        ownEvent: false,
      ),
      isFalse,
    );
  });

  test('server-deafen is hidden in Stage but retained in voice', () {
    expect(
      canServerDeafenInChannel(
        channel(ChannelType.stage, Permission.deafenMembers),
      ),
      isFalse,
    );
    expect(
      canServerDeafenInChannel(
        channel(ChannelType.voice, Permission.deafenMembers),
      ),
      isTrue,
    );
  });
}
