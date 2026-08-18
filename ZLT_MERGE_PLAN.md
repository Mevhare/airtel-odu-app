# Merging PR #1 (ZLT/Tozed support): what's blocking a direct merge, and the plan

PR #1 adds support for ZLT/Tozed hardware (e.g. the X17U) alongside the existing
ZTE pair, with auto-detection so the app picks the right client without being
told. The architecture is sound and does deliver that goal — but an extensive
review (manual read of the full diff + 8 parallel automated review passes)
turned up real bugs, some of which break the PR's *own* advertised setup path.
This app is also a live production instance for the ZTE hardware, so "mostly
works" isn't good enough for a direct merge.

This document is the record of what was found and the plan for fixing it. Work
happens on the `zlt-merge` branch, in an isolated worktree
(`wifiapp-zlt-merge/`) separate from the live checkout, so the running
dashboard is never touched mid-edit.

## Why not merge PR #1 as-is

### Bugs that would ship

1. **Crashes on the PR's own documented minimal ZLT config.** The README and
   `hardware.py`'s docstring both say a single-device ZLT unit doesn't need a
   separate `"odu"`/`"router"` config block. Two places disagree in code:
   - `hardware.py`'s `_candidates()` builds a tuple literal that unconditionally
     reads `config["router"]["host"]` and `config["odu"]["host"]` — no
     short-circuit, so it throws `KeyError` before ever probing for ZLT.
   - `app.py`'s `_handle_login()` unconditionally does
     `config["router"]["password"] = router_password` after a successful
     single-device login, same `KeyError`.
   - A new ZLT user following the PR's own README gets a crash, either at
     startup or immediately after logging in.

2. **Login race can invalidate the session.** `ZltSession` has a lock whose
   whole purpose (per its own comment) is to stop two logins racing — the
   firmware invalidates one session with the other. `ZltOdu.login()` /
   `ZltRouter.login()`, called from the HTTP `/api/login` handler, call
   `ZltSession.login()` directly and **unlocked**. The collector's background
   poll thread takes the lock before re-logging in internally on session
   expiry. A user logging in at the same moment the poll loop silently
   re-authenticates can race the firmware into invalidating the session the
   app thinks it holds.

3. **APN edit can create a duplicate-named profile.** ZLT's `set_apn()`
   matches profiles by `apnName`, but the `profile_id` the collector passes in
   is actually the *previous* profile's name, not a stable id (unlike the ZTE
   client, which matches by `profileId`). Editing an existing manual APN to a
   new string that matches no existing `apnName` appends a **second** profile
   with the same name instead of updating the first — the 4-slot capacity
   check also undercounts because it runs before the append.

4. **SMS ordering breaks past 2-digit message counts on ZLT.** Message `id` is
   stored as an un-cast string. The collector's generic "is this message new"
   check does a numeric comparison, but gets string comparison instead
   (`"10" > "9"` is `False` lexicographically) — new-message detection can
   silently misfire once a mailbox crosses that boundary.

5. **Narrow-bitmask edge case.** A ZLT unit advertising only legacy 2G/3G modes
   leaves `mode_goals` missing keys entirely; clicking Optimise-for on such a
   unit likely throws a JS error or posts an invalid mode instead of the
   button being hidden. Low likelihood, real gap.

### Structural concern (not a bug, flagged for awareness)

The clean "hardware asks itself what it supports" pattern (`capabilities`,
`net_modes`, `mode_goals`, `.reboot()`, `.carriers()`) is what makes this
maintainable — but `single_device` and "what optimise-goal is currently
active" don't fully go through it. They show up as hardcoded
`if single_device: ... else: ...` branches in `app.py` (reboot fan-out, login
shape) and independently, six separate times, in `app.js`. Works fine for
exactly two families; a third family wouldn't "just work" through
`hardware.py` the way the rest of the pattern implies. Not fixed in this pass
(no third family exists yet to design against) — noted so it doesn't get
forgotten if one shows up.

### Missing feature (the user's own request, folded in here)

There is currently no path for "this isn't ZTE or ZLT hardware." `"auto"`
detection only *tests for* ZLT; if that fails, it silently **assumes** ZTE
(kept for backward compatibility with configs written before this PR existed)
rather than confirming it. A genuinely unsupported third device gets treated
as ZTE, then every API call fails against it, surfacing as a generic 502
"the hardware refused or is not answering" — which reads like a connectivity
problem, not "we haven't built for this."

## Work plan

- [x] Merge PR #1 into an isolated worktree/branch (`zlt-merge`), not the live
      checkout.
- [x] Fix #1: make `_candidates()` and `_handle_login()` tolerate a config with
      no `"router"`/`"odu"` block, matching what the docs already promise.
      (Bonus: `_diagnostics()` had the same root cause, pre-existing, not part
      of the PR's diff — fixed alongside it.)
- [x] Fix #2: move the login handshake under `ZltSession`'s existing lock so
      the HTTP-triggered login and the poll thread's internal re-login can't
      race.
- [x] Fix #3: match ZLT APN profiles by a stable id the way the ZTE client
      does, instead of by name, so edits update in place instead of
      duplicating.
- [x] Fix #4: cast ZLT SMS `id` to int at parse time so ordering comparisons
      are numeric everywhere, not just in the one place that was already fixed.
- [x] Fix #5: `goals_for()` now falls back to whatever mode it can, for every
      goal, whenever a narrow bitmask leaves some or all of them unmatched --
      the dashboard's `mode_goals` map is always complete, so the frontend
      never sees an unmapped goal in the first place.
- [x] Add a real "unsupported hardware" outcome. `odu.probe()` now mirrors
      `zlt.probe()` (a cheap, unauthenticated login-salt call). `auto`
      detection tries ZLT, then confirms ZTE the same way; only when a host
      positively answers HTTP but as neither protocol does `hardware.build()`
      return `UNSUPPORTED` (odu/router both `None`) -- a host that simply
      doesn't answer at all (still booting, offline) still falls back to
      assuming ZTE, exactly as before this existed, so a real ZTE unit on a
      slow network never gets misclassified. `Collector` skips starting its
      poll threads for `UNSUPPORTED`; `/api/session` and `/api/login` report
      and refuse it cleanly; the frontend shows a dedicated screen instead of
      the login form.
- [x] Verify the ZTE path is unaffected against the real hardware (read-only
      checks — login, overview, no writes) from the worktree before anything
      touches the live checkout. Confirmed: `hardware.build()` still resolves
      to `zte`, ODU and router both login and read cleanly (netinfo, session
      usage, device list) — one login attempt per device, nothing written.
- [ ] Leave the live `master` checkout and its running Task Scheduler process
      untouched until this is reviewed and you decide to deploy it.

Structural cleanup (capability-branch consolidation) is noted but out of scope
for this pass — it doesn't cause incorrect behavior on either hardware family
today. The startup-probe latency is now up to ~12s in the worst case (nothing
answers at all: two ZLT probes plus a ZTE probe plus a reachability check,
each up to 3s) — one-time per process start, and only the full worst case
when every configured address is unreachable.
