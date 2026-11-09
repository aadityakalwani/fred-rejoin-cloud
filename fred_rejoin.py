#!/usr/bin/env python3
"""Keep the Fireflies bot (Fred) in tutoring lessons that start late or drop.

Runs every 60 s with --live (LaunchAgent com.aadi.fred-rejoin). Per "Tutoring *"
lesson with a Meet link, checks at start +1,+3,...,+23 (every 2 min), then +30,+40,+50
(while before end), then end-5 and end-1; +/-30 s slot tolerance; no early stop.
One shared active_meetings query per tick; if the Meet code is absent, addToLiveMeeting.
Zero API calls outside slots or with no key. Cap 3 adds per rolling 20 min; a lesson is
not re-added within 4 min. No re-add when our last add ended as a Fireflies silent meeting,
or when the same attendees are live with Fred in another Tutoring room (back-to-back overrun).
Flags: --live (else DRY-RUN), --now ISO (test), --probe, --selftest.
Cloud mode (env GITHUB_ACTIONS or flag --sweep): stateless. Every run checks EVERY lesson in
progress (start+7 min to end), so a delayed cron still rescues it. Fireflies (active_meetings and
today's transcripts) is the source of truth: no local state file. Secrets from env
FRED_SA_JSON (service-account JSON text) and FIREFLIES_API_KEY.
Key: env FIREFLIES_API_KEY or ~/.config/fred-rejoin/fireflies.key. No key = log + exit 0.
"""
import datetime, json, os, pathlib, sys, urllib.request, zoneinfo

TZ = zoneinfo.ZoneInfo("Europe/London")
REM = pathlib.Path.home() / "Library/Application Support/tutoring-reminders"
SA = REM / "service-account.json"
CAL = os.environ.get("FRED_CAL", "primary")
CFG = pathlib.Path.home() / ".config/fred-rejoin"
KEYFILE = CFG / "fireflies.key"
STATE = CFG / "state.json"
CACHE = pathlib.Path("/tmp/fred-rejoin-events.json")
LOG = pathlib.Path("/tmp/fred-rejoin.log")
API = "https://api.fireflies.ai/graphql"
SLOTS = list(range(7, 24, 2)) + [30, 40, 50]   # min after start (from +7: Fireflies auto-join can take ~4 min; earlier adds caused duplicate Freds 7 Nov); plus end-5 and end-1
READD_GAP = 4                                      # minutes
ADD_CAP, ADD_WINDOW = 3, 20                   # Fireflies: 3 adds per 20 min


def log(m):
    line = f"[{datetime.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {m}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def key():
    return (os.environ.get("FIREFLIES_API_KEY") or "").strip() or (KEYFILE.read_text().strip() if KEYFILE.exists() else "")


def gql(query, variables=None):
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key()}",
                                  "User-Agent": "fred-rejoin/2"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def sweep_due(age, dur):
    """Cloud sweep: any lesson in progress, from 7 min after start until it ends (Fireflies auto-join needs ~4 min)."""
    return 7 <= age < dur


def recent_join(link, trs, now):
    """A recording of this room began in the last READD_GAP minutes: Fred is still joining."""
    return any(meet_id(t.get("meeting_link")) == meet_id(link)
               and 0 <= now.timestamp() - t["date"] / 1000 < READD_GAP * 60 for t in trs)


def fetch_events(d0):
    if CACHE.exists():  # 10 min cache so a 60 s tick does not hit Google every time
        c = json.loads(CACHE.read_text())
        if c.get("v") == 2 and c.get("day") == d0.date().isoformat() and datetime.datetime.now().timestamp() - c["at"] < 600:
            return c["events"]
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    if os.environ.get("FRED_SA_JSON"):
        cr = service_account.Credentials.from_service_account_info(json.loads(os.environ["FRED_SA_JSON"]), scopes=scopes)
    else:
        cr = service_account.Credentials.from_service_account_file(str(SA), scopes=scopes)
    svc = build("calendar", "v3", credentials=cr, cache_discovery=False)
    items = svc.events().list(calendarId=CAL, timeMin=d0.isoformat(),
                              timeMax=(d0 + datetime.timedelta(days=1)).isoformat(),
                              singleEvents=True, orderBy="startTime").execute().get("items", [])
    evs = [{"summary": e.get("summary", ""), "start": e.get("start", {}).get("dateTime"),
            "end": e.get("end", {}).get("dateTime"), "link": e.get("hangoutLink"),
            "att": sorted(a["email"].lower() for a in e.get("attendees", []) if a.get("email"))} for e in items]
    CACHE.write_text(json.dumps({"v": 2, "day": d0.date().isoformat(), "at": datetime.datetime.now().timestamp(), "events": evs}))
    return evs


def transcripts(d0, now):
    """Today's Fireflies recordings, [] on error. A recording spanning `now` (only possible
    in a --now replay) also counts as live, so replays see what active_meetings saw then."""
    try:
        r = gql("query($f:DateTime){transcripts(fromDate:$f,limit:50){date duration meeting_link "
                "organizer_email participants meeting_info{silent_meeting}}}", {"f": d0.isoformat()})
        return (r.get("data") or {}).get("transcripts") or []
    except Exception as e:
        log(f"transcripts failed: {e!r}"); return []


def meet_id(link):
    return (link or "").rstrip("/").split("/")[-1].split("?")[0].lower()


def skip_reason(link, att, added, evs, active, trs, now):
    """Why not to re-add Fred to this lesson, or None. trs = today's Fireflies transcripts."""
    for t in trs:  # 1. our last add produced a recording that ended silent: the room is empty
        t0 = t["date"] / 1000
        if meet_id(t.get("meeting_link")) == meet_id(link) and added and t0 >= added - 60 \
                and t0 + (t.get("duration") or 0) * 60 <= now.timestamp() \
                and ((t.get("meeting_info") or {}).get("silent_meeting")
                     or not set(t.get("participants") or []) - {t.get("organizer_email"), "fred@fireflies.ai"}):
            return "last add ended as a silent meeting, not re-adding"
    active = active | {meet_id(t.get("meeting_link")) for t in trs   # recordings spanning now (--now replays)
                       if t["date"] / 1000 <= now.timestamp() < t["date"] / 1000 + (t.get("duration") or 0) * 60}
    for e in evs:  # 2. same people still live with Fred in another Tutoring room
        if att and e["summary"].lower().startswith("tutoring") and e.get("link") \
                and meet_id(e["link"]) != meet_id(link) and meet_id(e["link"]) in active and e.get("att") == att:
            return f"same attendees live in {e['summary']} ({meet_id(e['link'])}), not re-adding"
    return None


def selftest():
    now = datetime.datetime(2026, 10, 20, 6, 59, tzinfo=TZ)
    ts = lambda h, m: datetime.datetime(2026, 10, 20, h, m, tzinfo=TZ).timestamp()
    att = ["owner@example.com", "tutor@example.com", "fred@fireflies.ai"]
    s1 = {"summary": "Tutoring Yunal Science 1", "link": "https://meet.google.com/aaa-bbbb-ccc", "att": att}
    s2 = {"summary": "Tutoring Yunal Science 2", "link": "https://meet.google.com/ddd-eeee-fff", "att": att}
    silent = [{"date": ts(6, 45) * 1000, "duration": 12.07, "meeting_link": s2["link"],
               "organizer_email": "owner@example.com", "participants": ["owner@example.com"],
               "meeting_info": {"silent_meeting": True}}]
    a = ts(6, 42)
    assert "silent" in skip_reason(s2["link"], att, a, [s1, s2], set(), silent, now)
    assert skip_reason(s2["link"], att, 0, [s1, s2], set(), silent, now) is None            # not our add: rescue
    assert skip_reason(s2["link"], att, ts(6, 50), [s1, s2], set(), silent, now) is None    # recording predates add
    live = [dict(silent[0], meeting_info={"silent_meeting": False}, participants=att)]
    assert skip_reason(s2["link"], att, a, [s1, s2], set(), live, now) is None              # real lesson: rescue
    assert skip_reason(s2["link"], att, a, [s1, s2], set(), silent, now.replace(minute=50)) is None  # not ended yet
    assert "same attendees" in skip_reason(s2["link"], att, 0, [s1, s2], {"aaa-bbbb-ccc"}, [], now)
    other = dict(s1, att=att[:1] + ["x@y.com"])
    assert skip_reason(s2["link"], att, 0, [other, s2], {"aaa-bbbb-ccc"}, [], now) is None  # different student
    assert skip_reason(s2["link"], att, 0, [s1, s2], {"ddd-eeee-fff"}, [], now) is None
    replay = [dict(live[0], date=ts(5, 30) * 1000, duration=93.3, meeting_link=s1["link"])]  # S1 overran to 07:03
    assert "same attendees" in skip_reason(s2["link"], att, 0, [s1, s2], set(), replay, now)
    assert not sweep_due(0.5, 60) and not sweep_due(5, 60) and sweep_due(7, 60) and sweep_due(17, 60) and sweep_due(59.9, 60) and not sweep_due(60, 60)
    assert sweep_due(14, 60)                                   # a 14 min late cron still rescues the lesson
    rj = [{"date": ts(6, 57) * 1000, "meeting_link": s2["link"]}]
    assert recent_join(s2["link"], rj, now) and not recent_join(s1["link"], rj, now)
    assert not recent_join(s2["link"], rj, now.replace(minute=2, hour=7))  # joined >4 min ago: may re-add
    print("selftest ok")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    live = "--live" in sys.argv
    if "--probe" in sys.argv:
        r = gql("query{active_meetings{id title meeting_link state}}")
        log(f"probe: errors={'errors' in r} active={len((r.get('data') or {}).get('active_meetings') or [])}")
        return 0
    now = datetime.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).astimezone(TZ) \
        if "--now" in sys.argv else datetime.datetime.now(TZ)
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        evs = fetch_events(d0)
    except Exception as e:
        log(f"calendar read failed: {e!r}"); return 0
    sweep = "--sweep" in sys.argv or bool(os.environ.get("GITHUB_ACTIONS"))
    state = json.loads(STATE.read_text()) if STATE.exists() and not sweep else {}
    state = {k: v for k, v in state.items() if k == "adds" or k[:10] >= d0.date().isoformat()}
    adds = [t for t in state.get("adds", []) if now.timestamp() - t < ADD_WINDOW * 60]
    due, seen = [], set()
    for e in evs:
        s, st, en, link = e["summary"], e["start"], e["end"], e["link"]
        if not s.lower().startswith("tutoring") or not st or not link or (s, st) in seen:
            continue
        seen.add((s, st))
        st, en = datetime.datetime.fromisoformat(st).astimezone(TZ), datetime.datetime.fromisoformat(en).astimezone(TZ)
        if now >= en:
            continue
        age = (now - st).total_seconds() / 60
        dur = (en - st).total_seconds() / 60
        k = st.isoformat() + "|" + s
        ls = state.get(k, {})
        slots = [x for x in SLOTS if x < dur - 5] + [dur - 5, dur - 1]
        if sweep:
            if not sweep_due(age, dur):
                continue
            ls["added"] = 0
        else:
            hit = [x for x in slots if abs(age - x) <= 0.5 and str(round(x, 1)) not in ls.get("slots", [])]
            if not hit:
                continue
            ls["slots"] = ls.get("slots", []) + [str(round(hit[0], 1))]
        due.append((k, s, st, en, link, ls, e.get("att", [])))
    if not due:
        if sweep:
            log(f"{now:%a %d %b %H:%M}: calendar read ok, {len(evs)} event(s) today, 0 lesson(s) in progress")
        return 0                                  # idle: zero Fireflies calls
    log(f"{'LIVE' if live else 'DRY-RUN'} {now:%a %d %b %H:%M}: {len(due)} lesson(s) {'in progress' if sweep else 'in a check slot'}")
    if not key():
        log("no key (put Fireflies API key in ~/.config/fred-rejoin/fireflies.key), skipping"); return 0
    try:
        r = gql("query{active_meetings{id title meeting_link state}}")
    except Exception as e:
        log(f"active_meetings failed: {e!r}"); return 0
    if "errors" in r:
        log(f"active_meetings error: {json.dumps(r['errors'])[:300]}"); return 0
    active = {meet_id(a.get("meeting_link")) for a in (r["data"]["active_meetings"] or [])}
    trs = None                                    # today's transcripts, fetched only if Fred is missing somewhere
    for k, s, st, en, link, ls, att in sorted(due, key=lambda d: d[2]):
        tag = f"{s} {st:%H:%M} (+{int((now - st).total_seconds() // 60)}m)"
        if meet_id(link) in active:
            log(f"{tag}: Fred is in the room")
        elif sweep and recent_join(link, trs := trs if trs is not None else transcripts(d0, now), now):
            log(f"{tag}: Fred missing, a recording of this room began under {READD_GAP} min ago, waiting")
        elif now.timestamp() - ls.get("added", 0) < READD_GAP * 60:
            log(f"{tag}: Fred missing, added moments ago, waiting")
        elif (why := skip_reason(link, att, ls.get("added", 0) or (st.timestamp() if sweep else 0), evs, active,
                                 trs := trs if trs is not None else transcripts(d0, now), now)):
            log(f"{tag}: Fred missing, {why}")
        elif len(adds) >= ADD_CAP:
            log(f"{tag}: Fred missing but add cap ({ADD_CAP}/{ADD_WINDOW}min) reached, next slot retries")
        else:
            mins = max(15, min(120, int((en - now).total_seconds() // 60) + 30))  # time left + 30 min overrun buffer
            if live:
                res = gql("mutation($l:String!,$t:String,$d:Int){addToLiveMeeting(meeting_link:$l,title:$t,duration:$d){success}}",
                          {"l": link, "t": s, "d": mins})
                log(f"{tag}: Fred missing, addToLiveMeeting -> {json.dumps(res)[:300]}")
                adds.append(now.timestamp()); ls["added"] = now.timestamp()
            else:
                log(f"{tag}: Fred missing, WOULD addToLiveMeeting(link={link}, duration={mins})")
        state[k] = ls
    state["adds"] = adds
    if live and not sweep:
        CFG.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
