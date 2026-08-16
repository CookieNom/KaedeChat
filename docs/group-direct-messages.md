# Group direct messages

Group DMs are private conversations for 3 to 10 people. They use the same
message, attachment, reaction, search, call, and unread-state systems as direct
messages. Web, desktop, and mobile users can create a group from their friends
list, optionally name it, manage its members, leave it, and start a voice,
video, or screen-sharing call.

## Membership and ownership

The account that creates a group is its first owner. Any member may rename the
conversation or invite an existing friend. Invitations do not create pending
requests: both homes must already have an accepted friendship before the new
member is added.

Every member can leave. When the owner leaves, ownership transfers to the
earliest remaining member. Only the current owner can remove another member,
and an owner leaves through the normal leave action rather than removing their
own account. The conversation closes after its final member leaves.

Leaving removes access immediately. It does not delete messages from homes
that were authorized to receive them while the account was a member.

## Federation

The creator's home instance is the group authority. It owns the conversation
identifier, display name, owner, participant set, and monotonic state version.
Membership and name mutations made on another home are signed and routed to
that authority. The authority rechecks the actor's current membership and, for
an invitation, asks the invitee's home to confirm the friendship.

After a successful mutation, the authority sends a signed full-state update to
all current homes and to homes whose members were removed. Replicas accept only
newer versions from the authority and reject equal-version conflicts. Removed
local users receive a channel deletion immediately; added users receive a
channel creation without needing to refresh.

Messages do not take an unnecessary hop through the group authority. A member's
home signs the message and delivers it to the other participating homes. Each
recipient verifies that the author belongs to the current participant set.
Attachments and older rolling-cache history remain requester-bound, including
when several group members share the same home instance.

## Calls

A group conversation can have one active ephemeral call. Starting a call rings
the current participant set. Members may accept and join the shared LiveKit
room, then use microphone, camera, screen sharing, and the existing device and
audio controls. Membership is rechecked when call actions and voice tokens are
authorized. A removed member cannot mint another token, and ending the call
closes it for the group.

## API summary

The authenticated client API uses composite channel and user references:

```text
POST   /api/v1/users/@me/channels/group
PATCH  /api/v1/users/@me/channels/{channel_ref}/group
POST   /api/v1/users/@me/channels/{channel_ref}/group/recipients
DELETE /api/v1/users/@me/channels/{channel_ref}/group/recipients/{user_ref}
POST   /api/v1/users/@me/channels/{channel_ref}/group/leave
```

Creation accepts two through nine unique friend handles plus an optional name.
The existing channel message, search, attachment, reaction, and call endpoints
then operate on the returned group channel.

Group channels include these additional fields:

```json
{
  "conversation_type": "group",
  "owner_id": "123456789",
  "owner_domain": "example.net",
  "recipients": []
}
```

`recipients` contains the other current members for the authenticated user.
Clients should compare both owner fields; snowflakes are not globally unique
without their origin domain.
