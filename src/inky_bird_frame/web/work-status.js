// Owner-only presentation of recorded activity. Never infer a live connection
// from a successful saved snapshot, or a promised start time from eligibility.
export function sourceState(source, complete) {
  if (!source.enabled) return 'Off';
  const state = source.activity?.status;
  if (state === 'running') return 'Checking now';
  if (state === 'unknown') return 'Not heard from lately';
  if (state === 'failed') return 'Last check failed';
  if (state === 'succeeded') return complete ? 'Checked' : 'Checked · may be out of date';
  if (source.status === 'error') return 'Last check failed';
  if (source.status === 'degraded') return 'Last check was incomplete';
  if (source.status === 'ok') return complete ? 'Checked' : 'May be out of date';
  return 'Not checked yet';
}

export function nextCheck(job, enabled = true, now = Date.now()) {
  if (!enabled) return 'Plate making is off.';
  if (job?.status === 'running') return 'Working now. New requests wait their turn.';
  const date = Date.parse(job?.next_run_at);
  if (!Number.isFinite(date)) return 'Next check: not known yet.';
  if (date < now)
    return 'The next check is overdue. Inky may be busy, or its scheduler may be stopped.';
  return 'Next check: about ' + new Date(date).toLocaleString() + '.';
}

export function requestState(request, work) {
  if (request.status === 'approved') return 'Already has a plate';
  if (request.status === 'queued') return 'Waiting to be tried again';
  if (request.status === 'preparing') return 'Getting ready to try again';
  if (request.status === 'blocked' && request.outcome === 'pending_review')
    return 'A new plate is waiting for you. Keep it or send it back first.';
  if (request.status === 'blocked' && request.outcome === 'retained_state')
    return 'Needs a hand: its saved work needs a look from whoever runs Inky. Nothing was thrown away.';
  if (request.status === 'blocked' && request.outcome === 'not_retryable')
    return 'There is nothing to try again for this bird right now.';
  if (request.status === 'blocked')
    return request.outcome === 'interrupted'
      ? 'Getting it ready was interrupted, and it was not started again. Check this page before asking once more.'
      : 'Needs a hand before it can be tried again.';
  if (request.status !== 'eligible') return 'Inky could not read this request. Reload to update it.';
  if (work.needs_attention?.includes(request.taxon_id))
    return 'Ready, but waiting for you to ask again. See why above.';
  if (work.waiting?.includes(request.taxon_id))
    return 'Ready. Inky is waiting a while before it tries again.';
  return 'Ready to be tried again';
}
