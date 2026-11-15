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

// --- design-heart: journal lines, the reason a bird is shown, new plates ---

// Where a record came from, in the owner's terms. Never a place.
const NOTED_AT = Object.freeze({
  birdbuddy: 'at your feeder',
  'birdnet-analyzer': 'on your microphone',
  birdweather: 'on your microphone',
  'birdnet-go': 'on your microphone',
  'ebird-archive': 'in your checklists',
  inaturalist: 'nearby',
  ebird: 'nearby',
});
const RECORDS_FROM = Object.freeze({
  birdbuddy: 'at your feeder',
  'birdnet-analyzer': 'on your microphone',
  'ebird-archive': 'from your checklists',
});
const RETAINED_ORDER = ['birdbuddy', 'birdnet-analyzer', 'ebird-archive'];
const LIVE_ORDER = ['birdbuddy', 'birdweather', 'birdnet-go', 'inaturalist', 'ebird'];
// How far back the live sources look, as the owner would say it.
const WINDOW_PERIOD = Object.freeze({
  'last-day': 'this week',
  'last-week': 'this week',
  'last-30-days': 'this month',
  'last-year': 'this year',
});

function records(evidence) {
  return (Array.isArray(evidence) ? evidence : []).filter(
    (row) => row && Number.isInteger(row.count) && row.count > 0,
  );
}
function earliest(values) {
  return values.filter((value) => plainDate(value)).sort()[0] || '';
}
function latest(values) {
  return values.filter((value) => plainDate(value)).sort().at(-1) || '';
}

// "In your journal: first noted 12 Mar 2019 · last noted 2 Nov 2026 · 14 records",
// or an empty string when nothing is recorded.
export function journalLine(evidence) {
  const rows = records(evidence);
  if (!rows.length) return '';
  const first = earliest(rows.map((row) => row.first_noted));
  const last = latest(rows.map((row) => row.last_noted));
  const parts = [];
  if (first && last && plainDate(first) === plainDate(last)) parts.push('noted ' + plainDate(last));
  else {
    if (first) parts.push('first noted ' + plainDate(first));
    if (last) parts.push('last noted ' + plainDate(last));
  }
  const sources = [...new Set(rows.map((row) => row.source))];
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  const from = sources.length === 1 ? RECORDS_FROM[sources[0]] : '';
  parts.push(plural(total, 'record', 'records') + (from ? ' ' + from : ''));
  return 'In your journal: ' + parts.join(' · ');
}

function daysAgo(value, today) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ''));
  if (!match || !plainDate(value)) return null;
  const day = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Math.round((Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()) - day) / 86400000);
}

// When and where the bird was most recently noted, from the owner's own
// records: "noted at your feeder on Tuesday". Empty when no dated record exists.
export function notedClause(evidence, today = new Date()) {
  let best = null;
  for (const row of records(evidence))
    if (NOTED_AT[row.source] && plainDate(row.last_noted) && (!best || row.last_noted > best.last_noted))
      best = row;
  if (!best) return '';
  const when = recentDate(best.last_noted, today);
  return 'noted ' + NOTED_AT[best.source] + ' ' + (/^\d/.test(when) ? 'on ' + when : when);
}

// One line saying why this bird is shown today, only from data that supports
// it: a record from this week, a live source's current window, or the year it
// was first noted. Empty otherwise. `window` is the live sources' look-back;
// without a known period the line names none.
export function whyThisBird(bird, { window = null, today = new Date() } = {}) {
  if (!bird) return '';
  const rows = records(bird.retained_evidence);
  for (const source of RETAINED_ORDER) {
    const recent = rows.some((row) => {
      const age = row.source === source ? daysAgo(row.last_noted, today) : null;
      return age !== null && age >= 0 && age < 7;
    });
    if (recent) return 'Noted ' + NOTED_AT[source] + ' this week';
  }
  const live = LIVE_ORDER.find((source) => (bird.sources || []).includes(source));
  if (live) {
    const period = WINDOW_PERIOD[window];
    const place = NOTED_AT[live];
    const lead = place === 'nearby' ? 'Seen nearby' : 'Noted ' + place;
    return period ? lead + ' ' + period : lead;
  }
  const first = earliest(rows.map((row) => row.first_noted));
  return first ? 'From your journal, ' + first.slice(0, 4) : '';
}

// "Today", "Yesterday", "Tuesday" or "12 Mar 2019", for a short list.
export function shortDate(value, today = new Date()) {
  const when = recentDate(value, today).replace(/^on /, '');
  return when ? when[0].toUpperCase() + when.slice(1) : '';
}

// New plates waiting for a look, and plates that need a hand, for Home and the
// Manage tab. Both are zero when plate work cannot be read.
export function plateCounts(management) {
  if (!management?.available) return { newPlates: 0, needsAHand: 0 };
  const candidates = management.work?.in_review?.candidates || [];
  const ready = candidates.filter((candidate) => candidate.inspect_available).length;
  return {
    newPlates: ready,
    needsAHand: (management.work?.needs_attention || []).length + candidates.length - ready,
  };
}
