// Idempotency keys and new collection IDs.
export function randomHex(bytes) {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (item) =>
    item.toString(16).padStart(2, '0'),
  ).join('');
}
