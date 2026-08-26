from dataclasses import dataclass
from enum import IntFlag


class Permission(IntFlag):
    CREATE_INVITE = 1 << 0
    KICK_MEMBERS = 1 << 1
    BAN_MEMBERS = 1 << 2
    ADMINISTRATOR = 1 << 3
    MANAGE_CHANNELS = 1 << 4
    MANAGE_GUILD = 1 << 5
    ADD_REACTIONS = 1 << 6
    VIEW_AUDIT_LOG = 1 << 7
    VIEW_CHANNEL = 1 << 10
    SEND_MESSAGES = 1 << 11
    MANAGE_MESSAGES = 1 << 13
    EMBED_LINKS = 1 << 14
    ATTACH_FILES = 1 << 15
    READ_MESSAGE_HISTORY = 1 << 16
    MENTION_EVERYONE = 1 << 17
    USE_EXTERNAL_EMOJIS = 1 << 18
    CONNECT = 1 << 20
    SPEAK = 1 << 21
    MUTE_MEMBERS = 1 << 22
    DEAFEN_MEMBERS = 1 << 23
    MOVE_MEMBERS = 1 << 24
    USE_VAD = 1 << 25
    CHANGE_NICKNAME = 1 << 26
    MANAGE_NICKNAMES = 1 << 27
    MANAGE_ROLES = 1 << 28
    MANAGE_WEBHOOKS = 1 << 29
    MANAGE_EMOJIS = 1 << 30
    STREAM = 1 << 31
    # Discord uses bit 31 for this permission, but Kaede already published
    # STREAM at that bit. Preserve existing stored masks and use the next free
    # stable bit instead of silently changing either permission.
    USE_APPLICATION_COMMANDS = 1 << 32
    MANAGE_THREADS = 1 << 34
    CREATE_PUBLIC_THREADS = 1 << 35
    CREATE_PRIVATE_THREADS = 1 << 36
    SEND_MESSAGES_IN_THREADS = 1 << 38
    MODERATE_MEMBERS = 1 << 40
    BAN_INSTANCES = 1 << 41
    PIN_MESSAGES = 1 << 51
    BYPASS_SLOWMODE = 1 << 52
    CREATE_TRACKER_TASKS = 1 << 53
    EDIT_OWN_TRACKER_TASKS = 1 << 54
    MANAGE_TRACKER_TASKS = 1 << 55
    ASSIGN_TRACKER_TASKS = 1 << 56
    MANAGE_TRACKER = 1 << 57


ALL_PERMISSIONS = sum(permission.value for permission in Permission)


@dataclass(frozen=True, slots=True)
class PermissionMetadata:
    permission: Permission
    label: str
    description: str
    group: str
    resource_scopes: tuple[str, ...]
    channel_types: tuple[int, ...] = ()
    dependencies: tuple[Permission, ...] = ()
    danger: str = "normal"


def _permission(
    permission: Permission,
    label: str,
    description: str,
    group: str,
    scopes: tuple[str, ...],
    *,
    channel_types: tuple[int, ...] = (),
    dependencies: tuple[Permission, ...] = (),
    danger: str = "normal",
) -> PermissionMetadata:
    return PermissionMetadata(
        permission,
        label,
        description,
        group,
        scopes,
        channel_types,
        dependencies,
        danger,
    )


PERMISSION_METADATA = (
    _permission(
        Permission.CREATE_INVITE,
        "Create invites",
        "Create guild invitation links.",
        "General",
        ("guild", "channel"),
    ),
    _permission(
        Permission.KICK_MEMBERS,
        "Kick members",
        "Remove lower-ranked members from the guild.",
        "Membership",
        ("guild",),
        danger="elevated",
    ),
    _permission(
        Permission.BAN_MEMBERS,
        "Ban members",
        "Ban lower-ranked members and optionally remove their messages.",
        "Membership",
        ("guild",),
        danger="dangerous",
    ),
    _permission(
        Permission.ADMINISTRATOR,
        "Administrator",
        "Bypass all channel permission overwrites and grant every permission.",
        "Advanced",
        ("guild",),
        danger="critical",
    ),
    _permission(
        Permission.MANAGE_CHANNELS,
        "Manage channels",
        "Create, edit, reorder, and delete channels and categories.",
        "Management",
        ("guild", "channel"),
        danger="elevated",
    ),
    _permission(
        Permission.MANAGE_GUILD,
        "Manage guild",
        "Change guild settings and federation policy.",
        "Management",
        ("guild",),
        danger="elevated",
    ),
    _permission(
        Permission.ADD_REACTIONS,
        "Add reactions",
        "Add reactions to messages.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL, Permission.READ_MESSAGE_HISTORY),
    ),
    _permission(
        Permission.VIEW_AUDIT_LOG,
        "View audit log",
        "View security-sensitive guild administration history.",
        "Management",
        ("guild",),
        danger="elevated",
    ),
    _permission(
        Permission.VIEW_CHANNEL,
        "View channel",
        "See a channel and its live activity.",
        "General",
        ("channel",),
        channel_types=(0, 2, 4, 5, 10, 11, 12, 15, 17),
    ),
    _permission(
        Permission.SEND_MESSAGES,
        "Send messages",
        "Send messages in channels and create posts in forum channels.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.MANAGE_MESSAGES,
        "Manage messages",
        "Delete and moderate other members' messages.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL, Permission.READ_MESSAGE_HISTORY),
        danger="elevated",
    ),
    _permission(
        Permission.EMBED_LINKS,
        "Embed links",
        "Expand links into rich previews.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.ATTACH_FILES,
        "Attach files",
        "Upload and attach files to messages.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.READ_MESSAGE_HISTORY,
        "Read message history",
        "Read retained messages and receive permitted federated history.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.MENTION_EVERYONE,
        "Mention everyone",
        "Notify broad guild or role audiences.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
    _permission(
        Permission.USE_EXTERNAL_EMOJIS,
        "Use external emoji and stickers",
        "Use emoji and stickers originating outside this guild.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.CONNECT,
        "Connect",
        "Join voice channels.",
        "Voice",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.SPEAK,
        "Speak",
        "Publish microphone audio in voice channels.",
        "Voice",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.CONNECT,),
    ),
    _permission(
        Permission.MUTE_MEMBERS,
        "Mute members",
        "Server-mute other voice participants.",
        "Voice moderation",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.CONNECT,),
        danger="elevated",
    ),
    _permission(
        Permission.DEAFEN_MEMBERS,
        "Deafen members",
        "Server-deafen other voice participants.",
        "Voice moderation",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.CONNECT,),
        danger="elevated",
    ),
    _permission(
        Permission.MOVE_MEMBERS,
        "Move members",
        "Move voice participants between channels.",
        "Voice moderation",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.CONNECT,),
        danger="elevated",
    ),
    _permission(
        Permission.USE_VAD,
        "Use voice activity",
        "Transmit with voice activity detection instead of push-to-talk.",
        "Voice",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.SPEAK,),
    ),
    _permission(
        Permission.CHANGE_NICKNAME,
        "Change nickname",
        "Change your own guild nickname.",
        "Membership",
        ("guild",),
    ),
    _permission(
        Permission.MANAGE_NICKNAMES,
        "Manage nicknames",
        "Change lower-ranked members' nicknames.",
        "Membership",
        ("guild",),
        danger="elevated",
    ),
    _permission(
        Permission.MANAGE_ROLES,
        "Manage roles and permissions",
        "Manage lower roles, member role assignments, and channel overwrites.",
        "Management",
        ("guild", "channel"),
        danger="critical",
    ),
    _permission(
        Permission.MANAGE_WEBHOOKS,
        "Manage webhooks",
        "Create, edit, rotate, and revoke channel webhooks.",
        "Management",
        ("guild", "channel"),
        channel_types=(0, 5, 15),
        danger="critical",
    ),
    _permission(
        Permission.MANAGE_EMOJIS,
        "Manage emoji and stickers",
        "Create and remove guild emoji and stickers.",
        "Management",
        ("guild",),
        danger="elevated",
    ),
    _permission(
        Permission.STREAM,
        "Share video and screen",
        "Publish camera video or screen shares in voice channels.",
        "Voice",
        ("channel",),
        channel_types=(2,),
        dependencies=(Permission.CONNECT,),
    ),
    _permission(
        Permission.USE_APPLICATION_COMMANDS,
        "Use application commands",
        "Use slash commands and context menu commands from applications.",
        "Applications",
        ("channel",),
        channel_types=(0, 2, 5, 10, 11, 12, 15, 17),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.MANAGE_THREADS,
        "Manage threads and posts",
        "Rename, archive, lock, delete, and view private threads and forum posts.",
        "Thread management",
        ("guild", "channel"),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
    _permission(
        Permission.CREATE_PUBLIC_THREADS,
        "Create public threads",
        "Create public and announcement threads.",
        "Threads",
        ("channel",),
        channel_types=(0, 5),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.CREATE_PRIVATE_THREADS,
        "Create private threads",
        "Create private threads.",
        "Threads",
        ("channel",),
        channel_types=(0,),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.SEND_MESSAGES_IN_THREADS,
        "Send messages in threads",
        "Send messages in public, private, announcement, and forum-post threads.",
        "Threads",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.MODERATE_MEMBERS,
        "Timeout members",
        "Temporarily or indefinitely restrict lower-ranked members.",
        "Membership",
        ("guild",),
        danger="dangerous",
    ),
    _permission(
        Permission.BAN_INSTANCES,
        "Ban federated instances",
        "Remove and block every member from a federated instance.",
        "Federation",
        ("guild",),
        danger="critical",
    ),
    _permission(
        Permission.PIN_MESSAGES,
        "Pin messages",
        "Pin and unpin messages in text channels and threads.",
        "Text moderation",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL, Permission.READ_MESSAGE_HISTORY),
        danger="elevated",
    ),
    _permission(
        Permission.BYPASS_SLOWMODE,
        "Bypass slowmode",
        "Send messages and create posts without waiting for slowmode.",
        "Text",
        ("channel",),
        channel_types=(0, 5, 10, 11, 12, 15),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
    _permission(
        Permission.CREATE_TRACKER_TASKS,
        "Create tracker tasks",
        "Add tasks to tracker channels.",
        "Tracker",
        ("channel",),
        channel_types=(17,),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.EDIT_OWN_TRACKER_TASKS,
        "Edit own tracker tasks",
        "Edit, move, complete, and delete tasks you created or are assigned.",
        "Tracker",
        ("channel",),
        channel_types=(17,),
        dependencies=(Permission.VIEW_CHANNEL,),
    ),
    _permission(
        Permission.MANAGE_TRACKER_TASKS,
        "Manage tracker tasks",
        "Edit, move, complete, and delete any task in tracker channels.",
        "Tracker moderation",
        ("channel",),
        channel_types=(17,),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
    _permission(
        Permission.ASSIGN_TRACKER_TASKS,
        "Assign tracker tasks",
        "Assign and unassign other members on tracker tasks.",
        "Tracker moderation",
        ("channel",),
        channel_types=(17,),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
    _permission(
        Permission.MANAGE_TRACKER,
        "Manage tracker",
        "Change tracker settings and create, edit, reorder, or remove lanes.",
        "Tracker management",
        ("channel",),
        channel_types=(17,),
        dependencies=(Permission.VIEW_CHANNEL,),
        danger="elevated",
    ),
)

PERMISSION_METADATA_BY_PERMISSION = {item.permission: item for item in PERMISSION_METADATA}

if set(PERMISSION_METADATA_BY_PERMISSION) != set(Permission):
    raise RuntimeError("permission metadata must describe every permission exactly once")
