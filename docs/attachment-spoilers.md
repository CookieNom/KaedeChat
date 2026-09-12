# Attachment spoilers

Web and desktop users can click a queued attachment preview and select **Mark as
spoiler**, or use the tray's spoiler toggle. Mobile users tap the queued attachment,
change **Mark as spoiler**, and tap **Done**. Forum post attachments use the same
controls. Images, videos, audio, and other files can be marked.

Recipients must explicitly reveal a spoiler before its media or download control
is rendered. Forwarded attachments, rich embeds, private responses, and encrypted
attachments use the same concealment. Forum list thumbnails omit spoiler images.

The wire convention is a `SPOILER_` filename prefix. This uses the existing filename
projection in storage, federation, snapshots, and local caches, so no migration is
needed. New rendering surfaces must honor the prefix. Encrypted messages keep it
inside the authenticated private filename in the file manifest; the public upload
remains named `encrypted-file`.

Web uploads begin before Send. `PATCH /api/v1/attachments/{id}/spoiler` with
`{"spoiler": true}` (or `false`) changes a queued plaintext upload's filename without
uploading its bytes again. Only its uploader can do this, before finalization or
binding to a message/response. The endpoint locks the row against finalization and
rejects encrypted uploads and non-message assets. Clients block sending that
attachment while metadata is pending or failed. Mobile applies the prefix before
its upload, which starts on Send.

The backend and updated clients must be deployed together. Older clients do not
conceal these filenames. Spoilers are a presentation choice, not access control;
a recipient with access to the attachment can reveal or download it.
