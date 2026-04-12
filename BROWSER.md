# Browser Tools on exe.dev: Hermes vs Shelley

## The situation

exe.dev VMs ship with a headless Chromium binary at `/headless-shell/headless-shell`
(Chrome 147, added to PATH via the image). **Shelley** (exe.dev's built-in coding agent)
uses this directly via Chrome DevTools Protocol (CDP) — it's a Go binary with CDP
support compiled in. Zero setup, works out of the box.

**Hermes** takes a different path: its browser tools shell out to `agent-browser`, an
npm CLI that wraps Playwright. Playwright in turn expects its own managed Chromium
install at `~/.cache/ms-playwright/chromium_headless_shell-{version}/`. If that path
doesn't exist, browser tools fail with "Executable doesn't exist."

## What we do in provisioning

1. Install Node 20+ (exeuntu ships without Node)
2. `npm install` in the hermes-agent directory (installs agent-browser + Playwright)
3. Symlink the pre-installed Chromium to Playwright's expected path:
   ```
   ln -sf /headless-shell/headless-shell \
       ~/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell
   ```

This avoids a ~150MB Chromium download. The version string (`1217`) is tied to the
Playwright version bundled with agent-browser and will break if agent-browser updates
its Playwright dependency.

## The fragility

The symlink path `chromium_headless_shell-1217` is hardcoded to the current
agent-browser/Playwright version. When Playwright updates, the expected path changes
and the symlink stops working. This will manifest as browser tools silently failing
with "Executable doesn't exist."

## Alternatives considered

- **CDP mode** (`BROWSER_CDP_URL`): Launch headless-shell as a CDP server and point
  Hermes at it. Eliminates the Playwright path issue but still requires agent-browser
  CLI. Tested but agent-browser's CDP mode had issues ("No page found").

- **`npx playwright install`**: Downloads Playwright's own Chromium (~150MB). Reliable
  but wasteful since an identical binary already exists on the VM.

- **Hermes fork change**: Modify Hermes to use CDP directly like Shelley does, removing
  the agent-browser/Playwright dependency entirely. This is the right long-term fix but
  is a non-trivial change to the browser tool implementation.
