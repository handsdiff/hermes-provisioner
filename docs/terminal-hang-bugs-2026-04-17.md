---
type: incident-findings
tags:
  - hermes
  - terminal-tool
  - subprocess
  - fleet
date: 2026-04-17
status: bug-1 fixed + fleet cleaned (2026-04-18); bugs 2, 3, 4 open
---

## 2026-04-18 update

**Bug 1 fixed and deployed.** Root cause was `A && B &` in bash parsing
as `(A && B) &`, forking a subshell that waits for infinite B.
Fork-main commit `e9bcacc5` rewrites the tail to `A && { B & }` using a
shell-aware walker in `tools/terminal_tool.py`; called once from
`BaseEnvironment.execute()` so it runs on every backend. Upstream PR:
[#12207](https://github.com/NousResearch/hermes-agent/pull/12207). 34
new unit tests + end-to-end validation on `wait4test` VM.

**Fleet deployed 2026-04-18.** All 5 production VMs + wait4test updated
to fork main `9aae960f`. All leaked processes cleaned up — most via
`systemctl restart hermes`, but four cgroup-escaped orphans on slate-sal
had migrated to `/init.scope` and survived the restart; killed manually.

**Bugs 2, 3, 4 still open.** The 180s `terminal.timeout` not firing, the
`/stop` not reaching worker threads, and the slate-sal 15,113s API-call
idle remain unaddressed by this fix. Upstream `20f2258f` (#11907) pulled
into fork main as part of the rebuild should cover `/stop` propagation
to worker threads; validation pending next occurrence.

---


# Hermes terminal-hang bugs — evidence and fleet exposure (2026-04-17)

Triggering incident: Jakub reported vela unresponsive for ~30 min in a Telegram DM. Investigation found three distinct bugs + one open anomaly, plus six leaked subprocesses across the fleet. This doc records the evidence; fixes are deferred.

## Summary

| # | Bug | Layer | Evidence | Fix state |
|---|---|---|---|---|
| 1 | Foreground `terminal` call that launches a server with `&` leaves an orphaned `bash` parent stuck in `wait4()`, which prevents `proc.poll()` from ever returning | Agent behavior + bash/subprocess interaction | `/proc/*/stack`, live process tree on 2 VMs | None |
| 2 | `terminal.timeout` (default 180s) never fires when bug #1 triggers — no 10s activity heartbeats for 1800s+ | Hermes gateway / `_wait_for_process` | Timeout-state log strings, absence of `terminal command running (Xs elapsed)` descs | None |
| 3 | `/stop` releases the gateway-side session lock but does not kill the orphaned subprocess | Hermes gateway `_handle_stop_command` | Source comment + live-process evidence hours after `/stop` | Acknowledged in source, not resolved |
| 4 (anomaly) | slate-sal had a 15,113s (4.2h) idle-timeout with `tool=none, last_activity=API call #8 completed` | Hermes gateway | `errors.log` 2026-04-15 20:29:25 | Not investigated |

## Bug 1 — orphaned bash waits on backgrounded child

### Pattern

Agent issues a **foreground** terminal tool call whose command body backgrounds a long-running server. Example from the Jakub incident:

```bash
cd /home/exedev && python3 -m http.server 8000 &>/dev/null &
sleep 1
curl -s -o /dev/null -w "%{http_code}" https://slate-vela.exe.xyz/hub-artifacts/02-vision.md
```

The tool is `terminal`, NOT the `process` / `background=true` path. Hermes commit `933fbd8f` (fix: prevent agent hang when backgrounding processes via terminal tool) added `set +m;` prefix to the `bash -lic` spawn inside `tools/process_registry.py`, but that fix only covers explicit `background=true`. Foreground `bash -c` where the agent manually appends `&` is uncovered.

### Evidence (vela, PID 23363, stuck since 15:26 UTC)

```
$ cat /proc/23363/cmdline   # bash wrapper, 1h30m after incident
/usr/bin/bash -c source /tmp/hermes-snap-*.sh 2>/dev/null || true
cd /home/exedev/.hermes/hermes-agent || exit 126
eval 'cd /home/exedev && python3 -m http.server 8000 &>/dev/null &
sleep 1
curl -s -o /dev/null -w "%{http_code}" https://slate-vela.exe.xyz/hub-artifacts/02-vision.md'
__hermes_ec=$?
export -p > /tmp/hermes-snap-*.sh 2>/dev/null || true
pwd -P > /tmp/hermes-cwd-*.txt 2>/dev/null || true
printf '\n__HERMES_CWD_*__%s__HERMES_CWD_*__\n' "$(pwd -P)"
exit $__hermes_ec

$ sudo cat /proc/23363/stack
[<0>] do_wait+0x61/0xe0
[<0>] kernel_wait4+0xae/0x150
[<0>] __do_sys_wait4+0x9e/0xb0

$ ls -la /proc/23363/fd/
0 -> /dev/null
1 -> pipe:[136556]
2 -> pipe:[136556]

$ ps -o pid,ppid,pgid,sid,stat,cmd -p 23363 23365
  PID    PPID    PGID     SID STAT CMD
23363       1   23360   23360 S    /usr/bin/bash -c ...
23365   23363   23360   23360 S    python3 -m http.server 8000
```

Key observations:
- `bash -c` is non-interactive, non-login — job control is off by default. Yet bash is in `wait4()`.
- Bash still holds FD 1 and 2 open to the pipe the gateway's drain thread reads from. `proc.stdout.readline()` never sees EOF.
- PPid=1 — the gateway-executor's subprocess wrapper is gone; bash has been reparented to init.
- Python HTTP server is in the same pgid/sid as bash — it was not detached into its own session.

Identical stack on slate-sal PID 1563 (leaked since 2026-04-14).

### Why bash doesn't exit (hypothesis)

Non-interactive bash normally exits without waiting for background children. But in this spawn configuration — `subprocess.Popen([bash, "-c", cmd], stdout=PIPE, stderr=STDOUT, preexec_fn=os.setsid)` — the child `python3 -m http.server` inherits bash's stdout pipe briefly before `&>/dev/null` remaps its own FDs. If the inheritance window matters for bash's internal job tracking, or if bash's implicit end-of-script wait is triggered by the pipe on FD 1, bash stays in `wait4`. Full confirmation needs `strace` or `bash -x` repro (blocked on this VM by YAMA ptrace restrictions; reproduce off-VM).

`set +m` inside the eval (matching the existing fix on the background path) is likely sufficient but not verified. The general-purpose fix is to force the backgrounded process into its own session (`nohup` + `setsid` + `&` + redirect all FDs).

## Bug 2 — `terminal.timeout=180` never kills in this failure mode

### Expected

`tools/environments/base.py:_wait_for_process`:

```python
while proc.poll() is None:
    if is_interrupted(): ...
    if time.monotonic() > deadline:
        self._kill_process(proc)  # killpg(pgid, SIGTERM → SIGKILL)
        return {"output": ..., "returncode": 124}
    touch_activity_if_due(_activity_state, "terminal command running")
    time.sleep(0.2)
```

Two guarantees this path should provide:
1. At 180s, `killpg(pgid, SIGTERM)` → wait 1s → `SIGKILL`. Bash should be dead.
2. Every 10s, `touch_activity_if_due` calls the thread-local callback set at `run_agent.py:7868`, which updates `self._last_activity_ts`. Gateway's 1800s idle timeout should never fire while this loop is active.

### Observed

Neither guarantee holds in these incidents.

- Bash is alive **hours** after spawn — the 180s kill never fired. killpg either wasn't called or silently failed (signal masks? process-group mismatch? PGID/SID on the live bash is `23360` while PID is `23363` — the setsid'd process was `23360`, not bash itself; signals to `pgid=23360` may not reach bash if it was moved out of that group).
- The gateway-timeout ERROR log shows `last_activity=executing tool: terminal | tool=terminal`. That is the desc set by `_touch_activity(f"executing tool: {function_name}")` at `run_agent.py:7860` — fired once at tool start. If `touch_activity_if_due` inside the poll loop had fired even once, desc would read `terminal command running (10s elapsed)` or similar. It never did. So either (a) `_wait_for_process` was never reached, (b) the poll loop didn't iterate, or (c) `_get_activity_callback()` returned None on that thread.

### Why this matters

Without (1), a single bad command hangs the session for the full gateway inactivity window (30 min). Without (2), the stuck state is indistinguishable to the gateway from a truly dead agent, and the fallback response sent to the user is a generic "⏱️ Agent inactive for 30 min" with no command-level detail.

## Bug 3 — `/stop` doesn't kill subprocess children

`gateway/run.py:3050-3053` comment:

```
# /stop must hard-kill the session when an agent is running.
# A soft interrupt (agent.interrupt()) doesn't help when the agent
# is truly hung — the executor thread is blocked and never checks
# _interrupt_requested.
```

The code force-cleans `_running_agents` (releasing the session lock so new messages route) but never sends a signal to the subprocess. Evidence: vela's `/stop` fired at 15:36:02, bash (PID 23363) is still alive 1h30m later.

Every incident in the table below leaked processes unless a gateway restart with `systemctl restart hermes` happened to take them out via control-group kill.

## Fleet scan

Config uniform across all 5 VMs: `terminal.timeout: 180`, `persistent_shell: true`, `gateway_timeout: 1800`.

Incident counts from each VM's `~/.hermes/logs/errors.log` (same-minute duplicates collapsed):

| VM | Stuck-terminal events | Currently leaked processes | Notes |
|---|---|---|---|
| slate-vela | 3 (04-16 16:43; 04-17 09:54; 04-17 15:56) | 1 bash + 1 python http.server (PID 23363/23365, port 8000) | Jakub's incident |
| slate-sal | 2 (04-16 00:41 cluster; plus 04-15 20:29 non-terminal idle) | 2 bash + 4 python http.server (ports 8000, 9000, 9996, 9998), leaked since 2026-04-14 | Worst offender |
| combiagent | 2 (04-16 12:34 cluster; 04-16 17:56) | None visible — likely cleared by gateway restart | |
| trapezius | 1 (04-17 09:55) | None visible | |
| slate-tars | 0 | None | |

Totals: **8 unique stuck-terminal events in ~3 days across 4 of 5 VMs. 7 leaked subprocesses currently running.**

## Noteworthy side-observations

- **Session-skipping for failed requests.** After timeout the gateway logs `Skipping transcript persistence for failed request in session X to prevent session growth loop.` The stuck turn is not written to `state.db`. That's why `session_20260417_152350_9a0b6d3f.json` stops at iteration 8 even though the gateway's activity tracker was at iteration 10. Hides forensic evidence about what the agent actually ran.
- **Cross-session log-tagging.** Log lines at 15:25:29 tagged `[20260417_092012_6c98452a]` (the morning hub session) were emitted while handling a telegram message for session `20260417_152350_9a0b6d3f`. Contextvar bleed from a previous session — observability bug, not a root cause here but worth fixing so timeouts can be attributed correctly.
- **Self-diagnosis confabulation.** After the timeout, vela told Jakub "the timeout message suggests something got delayed on your end" and attributed it to a nonexistent 60s Telegram inactivity timer. The agent has no introspection surface for its own gateway state; it invents causes. Same shape as trapezius's confident-assertion habit from yesterday's sessions. Not part of the subprocess bug, but the reason Jakub spent 30 min uncertain instead of just reading `errors.log`.

## Open questions for follow-up

1. Does `set +m` inside the foreground eval body actually resolve bug 1? Needs off-VM repro.
2. Why does `_kill_process` fail to take out bash? PID/PGID split suggests the signal target group is wrong. Verify `os.getpgid(proc.pid)` returns a group containing bash, not the dead intermediate.
3. Why do no activity heartbeats fire? Rule out: thread-local not propagating, `_wait_for_process` never entered, poll-loop short-circuiting on exception.
4. slate-sal's 15,113s idle with `tool=none, last_activity=API call #8 completed` — separate bug class. Likely LLM stream that silently stalled without the stream-delta heartbeat; needs its own investigation.
5. Fleet cleanup: 7 leaked processes currently holding ports and FDs. Decision pending on restart vs. surgical kill.

## Upstream-PR targets

Reasonable PRs to NousResearch/hermes-agent based on the above (not filed yet):

- **Wrap agent-issued foreground commands containing `&` with `set +m; <cmd>`** in `_wrap_command` (or equivalently, detect trailing `&` and push the spawn into the background-registry path). Mirrors `933fbd8f` but for the path it missed.
- **Make `_handle_stop_command` send `SIGTERM` to the running tool's subprocess group**, not just flip in-memory state. Paired with a timeout-based `SIGKILL` escalator.
- **Always persist the stuck-turn transcript on inactivity timeout** (partial, with an `incomplete: true` marker). Losing the tool call args on timeout makes post-mortem harder than it needs to be.
