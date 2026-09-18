## UI and Cosmetics

- Design for regular users. Use familiar controls and plain language, and keep implementation details out of the interface unless they help users make a decision.
- Make interactive elements obvious: buttons should look like buttons, and their labels should clearly describe what happens when clicked.
- Make actions available only when they are valid and useful in the current state. For example, once a feature is enabled, disable or replace its Enable button with an enabled status or a Disable action. Disable actions with unmet prerequisites and prevent duplicate clicks while an action is in progress; enforce disabled behavior, not just disabled styling.
- Make control states clear, including enabled, disabled, selected, and loading states. Explain why an action is unavailable when the reason is not obvious.
- Keep components visually balanced. Align related elements evenly and use consistent spacing and padding. Give content enough breathing room, group related controls naturally, and avoid cramped content, excessive empty space, or lopsided layouts.
- Size controls, icons, text, and containers to suit their content and importance. Keep comparable controls consistent, use comfortable click and touch targets, and avoid awkwardly oversized or undersized elements. Adapt sizing and padding to smaller screens without clipping content or crowding controls.
- Make the next action easy to find with a clear visual hierarchy. Avoid confusing layouts, ambiguous icons, and clutter.
- Check affected screens at desktop and mobile sizes for alignment, balance, padding, sizing, readability, and obvious controls before considering UI work complete. Verify that available actions match the current state and that interactions feel natural.

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
