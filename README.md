# androauto

## Flowpilot — Android automation studio

A local-first MVP for creating projects, controlling an Android device, recording named flows, and replaying them without writing test code. The interface retains the quiet green-and-white design of the hosted prototype.

**This is real ADB integration code, tested against a simulated device adapter. It has not been verified on a physical Android device in the build environment.** Use the smoke test below on your phone before relying on it.

## Quick start on Kali Linux

Requirements: Python 3.10 or newer, Android platform-tools (`adb`), a modern desktop browser, and an Android device authorized for USB debugging. Python uses only its standard library; no pip installation or virtual environment is required.

```bash
sudo apt update
sudo apt install adb python3 git
git clone https://github.com/justinpraveen14/androauto.git
cd androauto
python3 server.py
```

Open **http://127.0.0.1:8765** in your browser. Keep the terminal running. Use Ctrl+C to stop.

On the phone, enable Developer options and USB debugging, connect USB, unlock the screen, and accept the RSA authorization prompt. Verify the connection:

```bash
adb devices -l
```

The status must be `device`, not `unauthorized` or `offline`. Root is not required. Do not run Flowpilot as root.

On Windows, install Android platform-tools, add the directory containing `adb.exe` to PATH, and start with `py -3 server.py`. On macOS, install platform-tools and start with `python3 server.py`. These platforms were not tested in this build.

## Your workflow

1. **Create project:** enter its name and installed Android package name. Find package names using `adb shell pm list packages`.
2. **Connect:** click Scan and explicitly select your device.
3. **Record:** Record launches the configured app and adds a launch step.
4. **Interact:** click the screenshot to tap, or drag to swipe. Use Back, Home, Type, Wait, and Assert as needed.
5. **Save:** choose Stop & save and enter a flow name.
6. **Replay:** return the app to the intended starting state, select the flow, and click Run. Supply text variables and opt into coordinate gestures if present.
7. **Inspect:** see per-step durations and failure details in Run history. Export JSON for inspection or version control.

**Interact through the browser preview during recording. Touches and typing performed directly on the physical phone are not captured.** Preview refreshes approximately every three seconds when idle, and after an action. This is a screenshot preview, not a low-latency video stream.

### Tap targeting and gestures

Before a tap, Flowpilot requests an Android UI hierarchy. It prefers a unique resource ID, then combinations of labels and element properties. During replay it looks up the element again and taps its current center. A missing or ambiguous selector fails the step; it does not silently choose a different element.

If no unique element is exposed, the tap uses normalized coordinates. Swipes are also coordinate-based. Such steps are visibly marked, require opt-in at replay, and refuse to run when display dimensions differ. They remain sensitive to UI changes even with the same display size.

### Text input and assertions

Tap a text field first, then use Type. Supply a variable name such as `LOGIN_EMAIL` and a value for the current recording. Only the variable name and field locator are saved; values must be supplied anew for each replay. The field must have a unique resource ID or accessibility description and be focused at replay.

Input **appends**; this MVP does not automatically clear existing values. Prepare empty fields before replay. Printable ASCII is supported except the percent (`%`) character. Unicode input needs a future IME integration.

Assert checks exact visible text against the UI tree. The text must be visible when adding the assertion. During replay it polls for the text for approximately ten seconds, plus time spent inside ADB commands. Assertions provide outcome verification; without them, a passed run only confirms that the commands completed.

### App state

Launch opens/resumes the app. It does not force-stop the app, clear app data, sign out, reset navigation, or undo earlier actions. Replays perform real actions. Use a test environment and explicitly prepare the correct starting state.

## Optional AI review — OmniRoute or compatible endpoint

AI reviews action types, selector field names, and coordinate usage. It does **not** receive screenshots, package/flow names, selector values, or entered values. It cannot modify or execute a test. This is advisory review, not self-healing or autonomous test generation.

Stop the companion. Set the actual endpoint and model configured in your router, then restart. Do not paste literal keys into source files.

```bash
export FLOWPILOT_AI_BASE_URL="http://127.0.0.1:YOUR_PORT/v1"
export FLOWPILOT_AI_MODEL="your-model-name"
export FLOWPILOT_AI_API_KEY="$(python3 -c 'import getpass; print(getpass.getpass("AI API key: "))')"
python3 server.py
```

Replace `YOUR_PORT`; the application appends `/chat/completions` to the base URL. The endpoint must support that API shape. For a local provider without authentication, omit `FLOWPILOT_AI_API_KEY`. Remote providers must use HTTPS. After restart, select a saved flow and click Review this flow. Provider compatibility is unverified until you supply your endpoint and model.

## Storage and security

- SQLite database: `~/.local/share/flowpilot/flowpilot.db` by default.
- Override: `python3 server.py --data-dir /your/private/directory --port 8766`.
- Stop the server before copying its database for backup.
- Input values are not written to flows or reports. Screenshots and hierarchy dumps are not stored by the host application; a temporary UI XML file is overwritten on the device.
- Assertions and element selectors may contain app-visible text. The database is not encrypted; keep it private.
- Companion binds only to `127.0.0.1`. A per-process token, Host/Origin checks, and same-origin UI prevent arbitrary webpages from issuing device actions. Do not expose it with ngrok or a reverse proxy.
- Only specific ADB operations are exposed. Remote shell arguments are quoted. There is no arbitrary shell command endpoint.
- Use one browser tab per companion. Recording is in memory until you save it. Closing the server loses unsaved steps; running reports are marked interrupted on the next startup.

## Supported today / remaining work

| Feature | Status |
| --- | --- |
| Project creation and named flow persistence | Implemented |
| Authorized USB ADB device selection | Implemented |
| App launch, screenshot preview, tap, swipe, navigation | Implemented |
| UI element locators and text assertions | Implemented |
| Input variables, replay cancellation, run history | Implemented |
| AI structural review through your provider | Implemented; provider setup required |
| Direct physical-phone touch capture | Not implemented |
| APK upload/install, app reset, flow import, step editor | Not implemented |
| Low-latency streaming, long press, multi-touch | Not implemented |
| Unicode text input / automatic field clearing | Not implemented |
| Visual comparison, autonomous AI, CI runner | Not implemented |

Custom canvas UIs, some WebViews, and inaccessible elements may not expose useful selectors. FLAG_SECURE screens can be black. UI hierarchy extraction can be slow or fail during continuous animation. Coordinate steps are a limited fallback, not a guarantee of robust replay.

## Verification

```bash
python3 -m unittest discover -s tests -v
```

The automated suite covers the full project → record → save → replay lifecycle with a fake device, moved element targeting, ambiguous selectors, coordinate opt-in and orientation mismatch, missing assertions, variable privacy, cancellation, persistence, AI request redaction, shell quoting, API guards, and HTTP asset delivery.

Python compilation and JavaScript syntax were checked. Browser visual/interactivity QA and physical-device testing were not available during this build.

### First physical-device smoke test

1. Create a project for an installed test app (or `com.android.settings`).
2. Connect and select the device; confirm the screenshot matches the phone.
3. Record: launch → tap a stable menu item → assert exact visible text → Back.
4. Save. Return to the same starting screen and replay.
5. Confirm each action on the physical phone and inspect the saved report.
6. Repeat with an input field and a disposable value. Confirm replay requests the variable again.

For a blank screen, test `adb exec-out screencap -p > screen.png`. For missing elements, try `adb shell uiautomator dump /data/local/tmp/flowpilot-window.xml` and inspect the command's message. Do not upload screenshots containing private app content when reporting issues.

Reference: [Android Debug Bridge documentation](https://developer.android.com/tools/adb).
