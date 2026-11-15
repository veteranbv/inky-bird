// Shared words for the owner pages: one name per thing, and plain dates and counts.

export const WORDS = Object.freeze({
  journal: 'Your journal',
  plate: 'Plate',
  newPlate: 'New plate',
  onMyFrame: 'On my frame',
  favorite: 'Favorite',
  favorites: 'Favorites',
  collections: 'Collections',
  morePlates: 'More plates',
  play: 'Play',
  wallFrame: 'Wall frame',
  sources: 'Sources',
  inTheWorks: 'In the works',
  needsAHand: 'Needs a hand',
});

export function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function calendarDay(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ''));
  if (!match) return null;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return Number.isNaN(day.getTime()) || day.getUTCDate() !== Number(match[3]) ? null : day;
}

// "12 Mar 2019"; an unreadable value gives an empty string so callers can omit the part.
export function plainDate(value) {
  const day = calendarDay(value);
  return day ? `${day.getUTCDate()} ${MONTHS[day.getUTCMonth()]} ${day.getUTCFullYear()}` : '';
}

// "today", "yesterday", a weekday within the last week, otherwise the plain date.
export function recentDate(value, today = new Date()) {
  const day = calendarDay(value);
  if (!day) return '';
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  const age = Math.round((now - day.getTime()) / 86400000);
  if (age === 0) return 'today';
  if (age === 1) return 'yesterday';
  if (age > 1 && age < 7) return `on ${DAYS[day.getUTCDay()]}`;
  return plainDate(value);
}

// Workstream additions go below, each in its own marked section, appended at the end.
