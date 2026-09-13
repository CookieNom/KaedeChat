## UI and Cosmetics

- Design for regular users. Use familiar controls and plain language, and keep implementation details out of the interface unless they help users make a decision.
- Make interactive elements obvious: buttons should look like buttons, and their labels should clearly describe what happens when clicked.
- Keep components visually balanced. Align related elements evenly and use consistent spacing, sizing, and padding; avoid lopsided layouts or awkwardly sized controls.
- Make the next action easy to find with a clear visual hierarchy. Avoid confusing layouts, ambiguous icons, and clutter.
- Check affected screens at desktop and mobile sizes for alignment, balance, readability, and obvious controls before considering UI work complete.

## Testing

- Add tests only when they provide meaningful coverage for new behavior, a regression, or a material risk. Do not add tests merely because a file changed or to mirror implementation details.
- Review existing coverage first. Extend an existing test when the new case fits its purpose and remains easy to understand; avoid duplicating scenarios or assertions across tests.
- Keep tests focused, fast, and deterministic. Use the smallest setup and lowest-cost test level that reliably verifies the behavior, and avoid unnecessary fixtures, mocks, or dependencies.
- Run checks appropriate to the change. Broaden or repeat testing only when changes, failures, or unresolved concerns justify it.

## Documentation

- Create or update documentation only when developers, administrators, or end users need it to build, configure, operate, maintain, or use the application. Do not create documentation merely to accompany a change.
- Do not create planning documents, decision records, implementation diaries, progress reports, or summaries of the work unless the user explicitly requests them. Keep task planning and status updates in the conversation.
- Update relevant existing documentation before adding a new file. Keep instructions concise, actionable, and appropriate for their audience; avoid duplicate content and information already clear from the code or interface.

## User Input

- When calling the `request_user_input` tool, never set `autoResolutionMs`. Wait for the user to answer explicitly.
