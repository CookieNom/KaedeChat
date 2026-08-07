export interface EmojiOption {
  value: string;
  name: string;
  keywords: string[];
  category: 'people' | 'nature' | 'food' | 'activity' | 'travel' | 'objects' | 'symbols';
}

export interface CustomEmojiOption {
  id: string;
  origin_domain: string;
  name: string;
  url: string;
  /** Stable wire-format token to insert into the message composer. */
  value: string;
  animated?: boolean;
}

const people = [
  ['😀', 'grinning face', 'happy smile'],
  ['😃', 'smiling face', 'happy'],
  ['😄', 'smile', 'happy laugh'],
  ['😁', 'beaming face', 'grin'],
  ['😂', 'tears of joy', 'laugh'],
  ['🤣', 'rolling laughing', 'lol'],
  ['😊', 'warm smile', 'blush'],
  ['🙂', 'slight smile', 'okay'],
  ['🙃', 'upside down face', 'silly'],
  ['😉', 'wink', 'playful'],
  ['😍', 'heart eyes', 'love'],
  ['🥰', 'smiling hearts', 'love'],
  ['😘', 'kiss', 'love'],
  ['😎', 'sunglasses', 'cool'],
  ['🤔', 'thinking', 'hmm'],
  ['🤨', 'raised eyebrow', 'skeptical'],
  ['😐', 'neutral face', 'blank'],
  ['😴', 'sleeping', 'tired'],
  ['😭', 'crying', 'sad'],
  ['😡', 'angry', 'mad'],
  ['🥳', 'party face', 'celebrate'],
  ['🤯', 'mind blown', 'shocked'],
  ['🫡', 'salute', 'respect'],
  ['👍', 'thumbs up', 'yes like'],
  ['👎', 'thumbs down', 'no dislike'],
  ['👏', 'clap', 'applause'],
  ['🙌', 'raised hands', 'celebrate'],
  ['🙏', 'folded hands', 'please thanks'],
  ['💪', 'flex', 'strong'],
  ['👀', 'eyes', 'look'],
  ['❤️', 'red heart', 'love'],
  ['💔', 'broken heart', 'sad']
] as const;
const nature = [
  ['🐶', 'dog', 'pet'],
  ['🐱', 'cat', 'pet'],
  ['🐭', 'mouse', 'animal'],
  ['🐹', 'hamster', 'pet'],
  ['🐰', 'rabbit', 'bunny'],
  ['🦊', 'fox', 'animal'],
  ['🐻', 'bear', 'animal'],
  ['🐼', 'panda', 'animal'],
  ['🐸', 'frog', 'animal'],
  ['🐢', 'turtle', 'animal'],
  ['🌸', 'cherry blossom', 'flower'],
  ['🌹', 'rose', 'flower'],
  ['🌻', 'sunflower', 'flower'],
  ['🌈', 'rainbow', 'weather'],
  ['☀️', 'sun', 'weather'],
  ['🌙', 'moon', 'night']
] as const;
const food = [
  ['🍎', 'apple', 'fruit'],
  ['🍓', 'strawberry', 'fruit'],
  ['🍉', 'watermelon', 'fruit'],
  ['🍕', 'pizza', 'food'],
  ['🍔', 'hamburger', 'food'],
  ['🍟', 'fries', 'food'],
  ['🌮', 'taco', 'food'],
  ['🍣', 'sushi', 'food'],
  ['🍪', 'cookie', 'dessert'],
  ['🍰', 'cake', 'dessert'],
  ['☕', 'coffee', 'drink'],
  ['🍺', 'beer', 'drink']
] as const;
const activity = [
  ['⚽', 'soccer ball', 'sport'],
  ['🏀', 'basketball', 'sport'],
  ['🏈', 'football', 'sport'],
  ['🎮', 'game controller', 'gaming'],
  ['🎲', 'game die', 'game'],
  ['🎨', 'artist palette', 'art'],
  ['🎵', 'music note', 'music'],
  ['🎉', 'party popper', 'celebrate'],
  ['🏆', 'trophy', 'winner']
] as const;
const travel = [
  ['🚗', 'car', 'travel'],
  ['✈️', 'airplane', 'travel'],
  ['🚀', 'rocket', 'space'],
  ['🏠', 'house', 'home'],
  ['🌍', 'globe', 'world'],
  ['⛰️', 'mountain', 'travel']
] as const;
const objects = [
  ['💡', 'light bulb', 'idea'],
  ['📱', 'phone', 'mobile'],
  ['💻', 'laptop', 'computer'],
  ['📷', 'camera', 'photo'],
  ['🔔', 'bell', 'notification'],
  ['🔒', 'lock', 'secure'],
  ['🔑', 'key', 'unlock'],
  ['🎁', 'gift', 'present'],
  ['🔥', 'fire', 'hot']
] as const;
const symbols = [
  ['✅', 'check mark', 'yes done'],
  ['❌', 'cross mark', 'no'],
  ['⚠️', 'warning', 'alert'],
  ['❓', 'question mark', 'question'],
  ['❗', 'exclamation mark', 'important'],
  ['💯', 'hundred points', 'perfect'],
  ['✨', 'sparkles', 'shiny'],
  ['⭐', 'star', 'favorite']
] as const;

function options(
  category: EmojiOption['category'],
  rows: readonly (readonly [string, string, string])[]
): EmojiOption[] {
  return rows.map(([value, name, keywords]) => ({
    value,
    name,
    keywords: keywords.split(' '),
    category
  }));
}

export const unicodeEmojis: EmojiOption[] = [
  ...options('people', people),
  ...options('nature', nature),
  ...options('food', food),
  ...options('activity', activity),
  ...options('travel', travel),
  ...options('objects', objects),
  ...options('symbols', symbols)
];

export const emojiCategories: Array<{ id: EmojiOption['category']; label: string; icon: string }> =
  [
    { id: 'people', label: 'People', icon: '😀' },
    { id: 'nature', label: 'Nature', icon: '🐻' },
    { id: 'food', label: 'Food', icon: '🍕' },
    { id: 'activity', label: 'Activities', icon: '🎮' },
    { id: 'travel', label: 'Travel', icon: '🚀' },
    { id: 'objects', label: 'Objects', icon: '💡' },
    { id: 'symbols', label: 'Symbols', icon: '✨' }
  ];
