---
name: deepstate
description: Take a screenshot of the DeepState Map (deepstatemap.live) for a Ukrainian location. Use when asked to screenshot, capture, or show the front lines at a location.
argument-hint: "<location> [--zoom N]"
disable-model-invocation: false
allowed-tools: mcp__chrome-devtools__navigate_page, mcp__chrome-devtools__take_screenshot, mcp__chrome-devtools__evaluate_script, mcp__chrome-devtools__take_snapshot, mcp__chrome-devtools__click, mcp__chrome-devtools__wait_for, mcp__chrome-devtools__new_page, mcp__chrome-devtools__resize_page, Bash(python *deepstate_screenshot.py*), Read, Write
---

# DeepState Map Screenshot

Take a screenshot of the DeepState Map for the given location(s).

## Arguments

`$ARGUMENTS` — one or more Ukrainian location names (e.g. "Vovchansk"), optionally followed by `--zoom N` (default: 13).

Examples:
- `/deepstate Vovchansk`
- `/deepstate Bakhmut --zoom 11`
- `/deepstate 50.3969 36.8784 --zoom 11` (lat lng directly)

## Steps

### 1. Parse arguments

Extract location name(s) and optional `--zoom` value from `$ARGUMENTS`. Default zoom is 13.

### 2. Geocode the location

If a place name is given (not coordinates), geocode it to lat/lng. Use the Python script's geocoder:

```bash
python deepstate_screenshot.py "<location>" --zoom <zoom> 2>&1 | head -5
```

Or look up coordinates via web search if the script is unavailable. The location must be in Ukraine.

### 3. Build the URL

```
https://deepstatemap.live/en#<zoom>/<lat>/<lng>
```

### 4. Take the screenshot with Chrome DevTools MCP

1. **Navigate**: Use `navigate_page` to go to the constructed URL
2. **Wait**: Allow 3-5 seconds for map tiles to load
3. **Remove overlays**: Use `evaluate_script` to remove popups:
   ```javascript
   document.querySelectorAll('.cl-dialog, [class*="cl-dialog"], [class*="cl-widget"], .onboarding-overlay, .dialog-overlay').forEach(el => el.remove());
   ```
4. **Wait again**: Allow 2-3 more seconds for the map to render cleanly
5. **Screenshot**: Use `take_screenshot` and save to `screenshots/<location>_<zoom>_<timestamp>.png`

### 5. Show the result

Display the saved screenshot path and confirm the location/coordinates.

## Fallback

If Chrome DevTools MCP tools are not available, fall back to the Python script:

Run from the project root (the directory containing `deepstate_screenshot.py`):

```bash
source .venv/bin/activate && python deepstate_screenshot.py "<location>" --zoom <zoom>
```

For a multi-location overview with greedy label placement:

```bash
source .venv/bin/activate && python deepstate_screenshot.py --overview "Sumy" "Vovchansk" "Kupiansk"
```
