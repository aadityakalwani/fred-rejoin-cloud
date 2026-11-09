# fred-rejoin-cloud

Cloud backup watcher that keeps the Fireflies bot (Fred) in "Tutoring *" lessons.

- Runs on GitHub Actions every 5 minutes (`.github/workflows/fred-rejoin.yml`), plus manual `workflow_dispatch`.
- Reads "Tutoring *" events from Google Calendar with a service account.
- Asks Fireflies `active_meetings` for the live rooms. If Fred is absent from a lesson that is in progress, it calls `addToLiveMeeting`.
- Fireflies' own calendar auto-join is the primary path. This is the backup.

## Cloud mode (sweep)
GitHub cron can run 5 to 15 minutes late. So cloud mode checks EVERY lesson in progress (start+1 min until end), not narrow minute slots. A late run still rescues the lesson.
It keeps no state file. Fireflies is the source of truth:
- Fred already in the room: nothing to do.
- A recording of the room began under 4 minutes ago: Fred is joining, wait.
- Our add ended as a silent meeting: do not re-add.
- Same attendees live with Fred in another Tutoring room (back-to-back overrun): do not re-add.
- Cap of 3 adds per run.

## Secrets (GitHub Actions)
- `FRED_SA_JSON`: full service-account JSON text.
- `FIREFLIES_API_KEY`: Fireflies API key.
Without env vars the script falls back to the Mac files (`service-account.json`, `~/.config/fred-rejoin/fireflies.key`).

## Mac LaunchAgent
`com.aadi.fred-rejoin` (minute-slot mode) stays on. Both run safely together: each checks active meetings before adding.

## Test
`python fred_rejoin.py --selftest`
`gh workflow run fred-rejoin`
