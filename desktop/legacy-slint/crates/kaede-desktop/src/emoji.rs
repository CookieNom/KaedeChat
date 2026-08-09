//! Embedded Unicode emoji catalog mirroring the web client's
//! emojibase-derived dataset (same categories, shortcodes, and search
//! scoring), so the picker and `:` completions match the web experience.

use std::sync::OnceLock;

use serde::Deserialize;

#[derive(Deserialize)]
pub struct EmojiDef {
    /// The emoji glyph itself.
    pub e: String,
    /// Shortcode without the surrounding colons.
    pub s: String,
    /// Category id: people, nature, food, activity, travel, objects,
    /// symbols, flags.
    pub g: String,
    /// Space-separated search keywords.
    pub t: String,
}

pub const CATEGORIES: [(&str, &str, &str); 8] = [
    ("people", "Smileys & people", "😀"),
    ("nature", "Animals & nature", "🐻"),
    ("food", "Food & drink", "🍕"),
    ("activity", "Activities", "🎮"),
    ("travel", "Travel & places", "🚀"),
    ("objects", "Objects", "💡"),
    ("symbols", "Symbols", "✨"),
    ("flags", "Flags", "🏳️"),
];

pub fn catalog() -> &'static [EmojiDef] {
    static CATALOG: OnceLock<Vec<EmojiDef>> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(include_str!("emoji_catalog.json")).unwrap_or_default()
    })
}

/// Search scoring identical to the web client: exact shortcode, shortcode
/// prefix, keyword prefix, then substring anywhere.
fn score(emoji: &EmojiDef, needle: &str) -> Option<u8> {
    if needle.is_empty() {
        return Some(3);
    }
    if emoji.s == needle {
        return Some(0);
    }
    if emoji.s.starts_with(needle) {
        return Some(1);
    }
    if emoji
        .t
        .split_whitespace()
        .any(|keyword| keyword == needle || keyword.starts_with(needle))
    {
        return Some(2);
    }
    if emoji.s.contains(needle) || emoji.t.contains(needle) {
        return Some(3);
    }
    None
}

pub fn search(query: &str, limit: usize) -> Vec<&'static EmojiDef> {
    let needle = query.trim().to_lowercase();
    let mut scored = catalog()
        .iter()
        .enumerate()
        .filter_map(|(index, emoji)| score(emoji, &needle).map(|score| (score, index, emoji)))
        .collect::<Vec<_>>();
    scored.sort_by_key(|(score, index, _)| (*score, *index));
    scored
        .into_iter()
        .take(limit)
        .map(|(_, _, emoji)| emoji)
        .collect()
}
