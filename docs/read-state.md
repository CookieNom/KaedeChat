# Read positions and history navigation

The web client (also used by the Tauri desktop client) and the native Android/iOS client open unread channels and DMs around the saved read cursor. The **Jump to where you left off** button preserves the cursor from the start of the visit, even after reading newer messages. **Jump to latest** fetches the latest history page directly when browsing older history. Within the message region, Shift+PageUp jumps to the saved position and Ctrl+End jumps to latest.

Read positions use the existing server-side per-user/per-channel state and composite message references. Visible messages advance the cursor after a 250 ms scroll debounce, only while the document is visible and focused. The visit's divider stays fixed while the persistent cursor advances. A historical page's bottom does not acknowledge unloaded newer messages. Live messages do not get appended across a gap in historical pages. Partial acknowledgements retain later projected mentions; stale device updates cannot move the cursor backward.

History jumps use the existing `around` query and encryption/decryption path, preserve composer drafts, and handle deleted anchors by displaying the nearest retained history. Retention and access permissions still limit available history. A conversation without a previous read cursor opens around its first retained unread message. The first-message boundary is inclusive, so marking the first message unread still displays the divider in the correct place.

## Native mobile

The native timeline keeps the visit's saved-position button and new-message divider while the persistent cursor advances. Its existing **Jump to present** control now fetches the latest page directly when viewing historical pages. **Load newer messages** moves forward through the backlog without jumping to the end. Explicit jumps supersede automatic history requests and preserve the composer draft.

Only visible rows reported by the resumed, visible chat screen acknowledge history. Background apps, covered routes, loading history, and target-reveal animations do not advance the cursor. Acknowledgements use the existing retry queue and keep unread badges until the server confirms the read state. Composite cursors are cached with account-scoped preferences and synchronized through REST and gateway snapshots. The existing encrypted-history path is shared by both pagination directions and saved-position jumps.

## Manual actions and inbox

Message menus provide **Mark unread**, below the four quick reactions. This moves the durable cursor to the selected message's predecessor, increments `read_version`, and pauses automatic acknowledgements for the open visit until navigation or an explicit history jump. Automatic acknowledgements carry the version captured when queued; a stale version returns 409 and refreshes state rather than undoing a manual rewind. Gateway and REST snapshots carry the version, including mention projection updates. Cursor updates populate the ORM identity map before publishing the committed result.

Web/desktop and mobile provide conversation, server, and global **Mark as read** actions. Bulk operations acknowledge a fixed snapshot of each accessible conversation's latest message. New messages arriving afterward remain unread. The native inbox is available from the conversation browser; the web inbox is in the server rail.

The inbox separates unread conversations from recent mentions, supports server and mention-category filters, opens the selected conversation/message, and offers individual mention dismissal. Muted servers are excluded from Unreads unless **Show muted servers** is enabled. Mention dismissal is stored per account independently of the read cursor, synchronized on refresh, and mention queries are paginated and limited to seven days. Inbox metadata never contains decrypted message bodies; jumping uses the existing authorized history/decryption path. Open inbox dialogs do not acknowledge the covered conversation.

Apply migration `a27e6d9b4c81` before running the updated backend. It adds the read-state version and account-owned mention dismissal table. No new keyboard shortcuts were added in this expansion.

## Research

- [Discord: Why Discord is switching from Go to Rust](https://discord.com/blog/why-discord-is-switching-from-go-to-rust): Discord stores one read state per user per channel, including atomic counters. Its cache/service architecture addresses Discord's scale; Kaede reuses its existing SQL state and acknowledgement queue.
- [Discord: Keyboard Navigation FAQ](https://support.discord.com/hc/en-us/articles/1500000056121-Keyboard-Navigation-FAQ): documents oldest-unread navigation and read/unread actions. documents the navigation and manual read/unread behavior used as a reference.
- [Discord: How Discord Reduced Websocket Traffic by 40%](https://discord.com/blog/how-discord-reduced-websocket-traffic-by-40-percent): describes read states in READY. Kaede already loads and synchronizes read states through its gateway.
- [Reddit: “50+ messages since” not working as intended](https://www.reddit.com/r/discordapp/comments/wz09dc): users report repeated page-by-page loading instead of a direct jump. This is anecdotal UX feedback, not evidence of Discord's internal implementation. Kaede requests the cursor's window directly.

## Manual verification

1. Read part of a channel or DM with more than 50 newer messages. Leave and reopen it; verify it opens around the saved position and displays the new-message divider.
2. Jump to latest, then back to where you left off. Check that a composer draft survives both jumps.
3. Scroll through older history while another user sends messages. Verify the viewport stays in place and newer activity remains unread.
4. Background the tab, receive messages, then focus it. Only visible history should be acknowledged. Repeat with two devices and delayed acknowledgements.
5. Repeat in an encrypted conversation and with a deleted saved-position message.

- [Discord: Inbox FAQ](https://support.discord.com/hc/en-us/articles/360045027712-Inbox-FAQ): unread and mention tabs, filters, dismissal, jump actions, and seven-day mention retention.
