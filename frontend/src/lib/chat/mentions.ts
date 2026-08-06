interface MentionableUser {
  id: string;
  origin_domain: string;
  username: string;
  handle: string;
}

const mentionTokenCharacter = /[a-z0-9_.@-]/i;

function hasTokenEndBoundary(content: string, offset: number): boolean {
  const after = content[offset];
  if (after === undefined || !mentionTokenCharacter.test(after)) return true;
  // A final period is normally sentence punctuation; a period followed by a
  // token character is still part of a longer username or federated handle.
  return after === '.' && !mentionTokenCharacter.test(content[offset + 1] ?? '');
}

function containsMentionToken(content: string, token: string): boolean {
  const haystack = content.toLowerCase();
  const needle = token.toLowerCase();
  let offset = haystack.indexOf(needle);
  while (offset >= 0) {
    const before = haystack[offset - 1];
    if (
      (before === undefined || !mentionTokenCharacter.test(before)) &&
      hasTokenEndBoundary(haystack, offset + needle.length)
    ) {
      return true;
    }
    offset = haystack.indexOf(needle, offset + 1);
  }
  return false;
}

/** Match an explicit @username or federated @handle without prefix collisions. */
export function mentionsUser(
  content: string,
  user: MentionableUser,
  localDomain?: string
): boolean {
  return (
    content.includes(`<@${user.id}@${user.origin_domain}>`) ||
    (user.origin_domain === localDomain && content.includes(`<@${user.id}>`)) ||
    containsMentionToken(content, `@${user.handle}`) ||
    containsMentionToken(content, `@${user.username}`)
  );
}
