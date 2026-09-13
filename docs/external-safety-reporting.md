# External safety reporting

In Administration → Reports, PhotoDNA cases include an external reporting panel.
Administrators with `reports.manage` can download an authenticated JSON export,
submit it manually to their reporting destination, and record the destination,
receipt/reference, submission time, and notes. Export and submission-record actions
are audited. Submission records append to the case; they do not change its review
status or send anything to a remote instance or external service.

The versioned export includes the reporting instance, exporting administrator,
export time, case timestamps, available account identities, attachment/message
references, all recorded evidence, and previous submission records. New remote
PhotoDNA matches additionally record the origin domain, unsigned federation source
URL, observation time, scanned variant, byte size, and available conversation
reference. URLs require federation authorization and contain no access tokens.

Remote DM history authorization now supplies a qualified uploader reference. Older
peers can omit it; it remains unavailable rather than being inferred from the media
origin. Remote identities are claims supplied through federation, not independently
verified real-world identities. Existing cases are not retroactively enriched.

This workflow exports existing evidence. It does not retain matched image bytes or
PhotoDNA perceptual hashes, collect remote IP addresses/account records, or implement
an authority-specific submission API. A missing field is displayed as unavailable;
the originating instance may hold additional information. Administrators must use
the destination's own submission process and record its receipt after submission.
