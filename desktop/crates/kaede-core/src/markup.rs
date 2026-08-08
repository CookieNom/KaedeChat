//! Structural chat markup parsing for native renderers.
//!
//! The parser never emits HTML. Slint receives typed spans and decides how to
//! paint them, so links, mentions, spoilers and code cannot escape into an
//! embedded browser context.

use kaede_protocol::EntityRef;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SpanKind {
    Text,
    Bold,
    Italic,
    Strike,
    InlineCode,
    CodeBlock,
    Spoiler,
    UserMention(EntityRef),
    RoleMention(EntityRef),
    CustomEmoji {
        reference: EntityRef,
        name: String,
        animated: bool,
    },
    Link,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Span {
    pub kind: SpanKind,
    pub text: String,
}

#[must_use]
pub fn parse(input: &str) -> Vec<Span> {
    let mut spans = Vec::new();
    let mut cursor = 0;
    while cursor < input.len() {
        let rest = &input[cursor..];
        let candidate = [
            rest.find("```"),
            rest.find("||"),
            rest.find("**"),
            rest.find("~~"),
            rest.find('`'),
            rest.find("<@"),
            rest.find("<:"),
            rest.find("<a:"),
            rest.find("https://"),
            rest.find("http://"),
        ]
        .into_iter()
        .flatten()
        .min();
        let Some(offset) = candidate else {
            push_text(&mut spans, rest);
            break;
        };
        if offset > 0 {
            push_text(&mut spans, &rest[..offset]);
            cursor += offset;
            continue;
        }
        if let Some((span, consumed)) = parse_token(rest) {
            spans.push(span);
            cursor += consumed;
        } else {
            let Some(character) = rest.chars().next() else {
                break;
            };
            push_text(&mut spans, &character.to_string());
            cursor += character.len_utf8();
        }
    }
    spans
}

fn parse_token(input: &str) -> Option<(Span, usize)> {
    for (delimiter, kind) in [
        ("```", SpanKind::CodeBlock),
        ("||", SpanKind::Spoiler),
        ("**", SpanKind::Bold),
        ("~~", SpanKind::Strike),
        ("`", SpanKind::InlineCode),
    ] {
        if let Some(rest) = input.strip_prefix(delimiter) {
            let end = rest.find(delimiter)?;
            let text = rest[..end].to_owned();
            return Some((Span { kind, text }, delimiter.len() * 2 + end));
        }
    }
    if input.starts_with("<@") {
        let end = input.find('>')?;
        let token = &input[..=end];
        let (kind, reference) = if let Some(value) = token.strip_prefix("<@&") {
            (true, value.strip_suffix('>')?)
        } else {
            (false, token.strip_prefix("<@")?.strip_suffix('>')?)
        };
        let reference = reference.parse().ok()?;
        return Some((
            Span {
                kind: if kind {
                    SpanKind::RoleMention(reference)
                } else {
                    SpanKind::UserMention(reference)
                },
                text: token.to_owned(),
            },
            token.len(),
        ));
    }
    if input.starts_with("<:") || input.starts_with("<a:") {
        let end = input.find('>')?;
        let token = &input[..=end];
        let animated = token.starts_with("<a:");
        let body = token
            .trim_start_matches("<a:")
            .trim_start_matches("<:")
            .strip_suffix('>')?;
        let (name, reference) = body.split_once(':')?;
        if name.len() < 2
            || name.len() > 32
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        {
            return None;
        }
        let reference = reference.parse().ok()?;
        return Some((
            Span {
                kind: SpanKind::CustomEmoji {
                    reference,
                    name: name.to_owned(),
                    animated,
                },
                text: token.to_owned(),
            },
            token.len(),
        ));
    }
    if input.starts_with("https://") || input.starts_with("http://") {
        let end = input.find(char::is_whitespace).unwrap_or(input.len());
        let text = input[..end]
            .trim_end_matches(['.', ',', ')', ']', '}'])
            .to_owned();
        return Some((
            Span {
                kind: SpanKind::Link,
                text: text.clone(),
            },
            text.len(),
        ));
    }
    None
}

fn push_text(spans: &mut Vec<Span>, value: &str) {
    if value.is_empty() {
        return;
    }
    if let Some(Span {
        kind: SpanKind::Text,
        text,
    }) = spans.last_mut()
    {
        text.push_str(value);
    } else {
        spans.push(Span {
            kind: SpanKind::Text,
            text: value.to_owned(),
        });
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionMarker {
    User,
    Channel,
    Emoji,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompletionQuery {
    pub marker: CompletionMarker,
    pub query: String,
    pub start: usize,
    pub end: usize,
}

#[must_use]
pub fn completion_at(value: &str, cursor: usize) -> Option<CompletionQuery> {
    if cursor > value.len() || !value.is_char_boundary(cursor) {
        return None;
    }
    let prefix = &value[..cursor];
    let start = prefix
        .rfind(|character: char| character.is_whitespace())
        .map_or(0, |offset| offset + 1);
    let word = &prefix[start..];
    let (marker, query) = match word.as_bytes().first().copied()? {
        b'@' => (CompletionMarker::User, &word[1..]),
        b'#' => (CompletionMarker::Channel, &word[1..]),
        b':' => (
            CompletionMarker::Emoji,
            word[1..].strip_suffix(':').unwrap_or(&word[1..]),
        ),
        _ => return None,
    };
    if !query
        .chars()
        .all(|character| character.is_alphanumeric() || "_.+-".contains(character))
    {
        return None;
    }
    Some(CompletionQuery {
        marker,
        query: query.to_owned(),
        start,
        end: cursor,
    })
}

#[must_use]
pub fn replace_completion(value: &str, completion: &CompletionQuery, replacement: &str) -> String {
    format!(
        "{}{} {}",
        &value[..completion.start],
        replacement,
        &value[completion.end..]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_mentions_and_custom_emoji_are_typed() {
        let spans = parse("hello <@42@chat.example> <:wave:99@chat.example> ||secret||");
        assert!(matches!(spans[1].kind, SpanKind::UserMention(_)));
        assert!(matches!(spans[3].kind, SpanKind::CustomEmoji { .. }));
        assert_eq!(spans[5].kind, SpanKind::Spoiler);
    }

    #[test]
    fn malformed_protocol_tokens_remain_plain_text() {
        assert_eq!(
            parse("<@oops>"),
            vec![Span {
                kind: SpanKind::Text,
                text: "<@oops>".to_owned()
            }]
        );
    }

    #[test]
    fn completion_is_cursor_scoped_and_preserves_suffix() {
        let Some(query) = completion_at("hello :hea later", 10) else {
            panic!("completion expected")
        };
        assert_eq!(query.marker, CompletionMarker::Emoji);
        assert_eq!(
            replace_completion("hello :hea later", &query, "❤️"),
            "hello ❤️  later"
        );
    }
}
