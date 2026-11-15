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

// ---- design-copy workstream ----

const WINDOWS = {
  'last-day': 'Last day',
  'last-week': 'Last week',
  'last-30-days': 'Last 30 days',
  'last-year': 'Last year',
  'all-time': 'All time',
};
// "Last 30 days"; a window this page does not know is still readable.
export function windowName(value) {
  const words = String(value ?? '').replaceAll('-', ' ').trim();
  return WINDOWS[value] || (words ? words[0].toUpperCase() + words.slice(1) : '');
}

const EVENTS = {
  discovery: 'New birds',
  generation_approved: 'New plates',
  terminal_error: 'A plate needs a hand',
  degraded: 'Something stopped working',
  recovered: 'It works again',
  publication_error: 'Plates could not be published',
  publication_recovered: 'Publishing works again',
  display_stale: 'The wall frame stopped updating',
  display_recovered: 'The wall frame updates again',
};
export function eventName(value) {
  const words = String(value ?? '').replaceAll('_', ' ').trim();
  return EVENTS[value] || (words ? words[0].toUpperCase() + words.slice(1) : '');
}

// A moment in the owner's terms: "today at 14:05", "yesterday at 09:10",
// "on Tuesday", "12 Mar 2019". An unreadable value gives an empty string.
export function when(value, today = new Date()) {
  const moment = new Date(value);
  if (value === null || value === undefined || value === '' || Number.isNaN(moment.getTime()))
    return '';
  const pad = (number) => String(number).padStart(2, '0');
  const local =
    moment.getFullYear() + '-' + pad(moment.getMonth() + 1) + '-' + pad(moment.getDate());
  const day = recentDate(local, today);
  return ['today', 'yesterday'].includes(day)
    ? day + ' at ' + pad(moment.getHours()) + ':' + pad(moment.getMinutes())
    : day;
}
