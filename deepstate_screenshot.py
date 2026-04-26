#!/usr/bin/env python3
"""Take screenshots of DeepState Map (deepstatemap.live) for specific Ukrainian locations."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from geopy.geocoders import Nominatim
from playwright.sync_api import sync_playwright

NAVIGATION_TIMEOUT = 60000  # 60s — the site can be slow

# JavaScript snippet injected before page load to capture the Leaflet map instance.
# Leaflet does not store the map on the DOM element, so we hook L.Map.prototype.initialize.
LEAFLET_CAPTURE_SCRIPT = """
    window.__capturedMaps = [];
    Object.defineProperty(window, 'L', {
        configurable: true,
        set(val) {
            Object.defineProperty(window, 'L', {value: val, writable: true, configurable: true});
            if (val && val.Map && val.Map.prototype) {
                const origInit = val.Map.prototype.initialize;
                val.Map.prototype.initialize = function() {
                    window.__capturedMaps.push(this);
                    return origInit.apply(this, arguments);
                };
            }
        },
        get() { return undefined; }
    });
"""

# JS expression to retrieve the captured Leaflet map instance.
_GET_MAP_JS = "window.__capturedMaps[0]"


def geocode_location(name: str) -> tuple[float, float, str]:
    """Geocode a location name to coordinates, restricted to Ukraine.

    Returns (lat, lng, display_name) or raises ValueError.
    """
    geolocator = Nominatim(user_agent="deepstate-screenshot-tool/1.0")
    location = geolocator.geocode(name, country_codes="ua")
    if location is None:
        raise ValueError(f"Could not find location: {name!r} in Ukraine")
    return location.latitude, location.longitude, location.address


def build_url(lat: float, lng: float, zoom: int = 13) -> str:
    """Build a DeepState Map URL with the given coordinates and zoom level."""
    return f"https://deepstatemap.live/en#{zoom}/{lat:.6f}/{lng:.6f}"


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use in a filename."""
    return re.sub(r"[^\w\-.]", "_", name).strip("_")


def _dismiss_overlays(page) -> None:
    """Remove popups, modals, and overlays that obscure the map."""
    page.evaluate("""() => {
        // Click any "agree" / "accept" buttons for license agreements
        document.querySelectorAll('button, a').forEach(el => {
            const text = (el.textContent || '').toLowerCase();
            if (text.includes('agree') || text.includes('accept') || text.includes('погоджу')) {
                el.click();
            }
        });

        // Remove all known overlay elements
        document.querySelectorAll(
            '.cl-dialog, [class*="cl-dialog"], [class*="cl-widget"],'
            + '.cl-content-locker, .overlay-backdrop, .drawer-backdrop,'
            + '.onboarding-overlay, .dialog-overlay,'
            + '[class*="license"], [class*="agreement"], [class*="modal"],'
            + '[class*="popup"], [class*="cookie"]'
        ).forEach(el => el.remove());

        // Remove any full-screen overlays by z-index
        document.querySelectorAll('*').forEach(el => {
            const style = window.getComputedStyle(el);
            const z = parseInt(style.zIndex) || 0;
            const pos = style.position;
            if (z > 1000 && (pos === 'fixed' || pos === 'absolute')
                && el.offsetWidth > 500 && el.offsetHeight > 500
                && !el.classList.contains('leaflet-container')
                && el.id !== 'map'
                && el.id !== 'overview-label-overlay') {
                el.remove();
            }
        });
    }""")


def _js_click(page, selector: str) -> None:
    """Click an element via JavaScript to bypass overlay interception."""
    _dismiss_overlays(page)
    page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) el.click();
    }}""")


def _open_settings_panel(page) -> None:
    """Open the map settings panel by clicking the 'tune' nav button."""
    _js_click(page, 'button[data-site-nav="settings"]')
    time.sleep(1)


def _close_settings_panel(page) -> None:
    """Close the settings panel by clicking the 'tune' nav button again (toggle)."""
    _js_click(page, 'button[data-site-nav="settings"]')
    time.sleep(0.5)


def _enable_satellite(page) -> None:
    """Switch the basemap to satellite view."""
    _dismiss_overlays(page)
    result = page.evaluate("""() => {
        const items = document.querySelectorAll('.basemap-item');
        for (const item of items) {
            const label = item.querySelector('.basemap-item-label');
            if (label && label.textContent.includes('Супутник')) {
                // Use dispatchEvent for proper Vue.js reactivity
                item.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                return {clicked: true, label: label.textContent.trim(),
                        isActive: item.classList.contains('active')};
            }
        }
        const labels = [...items].map(i =>
            i.querySelector('.basemap-item-label')?.textContent?.trim());
        return {clicked: false, available: labels};
    }""")
    print(f"    Satellite switch: {result}")
    # Wait for satellite tile network requests to complete
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(3)
    # Verify the switch took effect
    active = page.evaluate("""() => {
        const active = document.querySelector('.basemap-item.active .basemap-item-label');
        return active ? active.textContent.trim() : 'none';
    }""")
    print(f"    Active basemap: {active}")


def _enable_show_ifs(page) -> None:
    """Toggle the 'Show IFS' (defense structures) checkbox on."""
    _dismiss_overlays(page)
    # Scroll the IFS section into view first
    page.evaluate("""() => {
        const rows = document.querySelectorAll('.icon-toggle-row');
        for (const row of rows) {
            const label = row.querySelector('.icon-toggle-label');
            if (label && label.textContent.includes('ІФС')) {
                row.scrollIntoView({behavior: 'instant', block: 'center'});
                break;
            }
        }
    }""")
    time.sleep(0.5)
    result = page.evaluate("""() => {
        const rows = document.querySelectorAll('.icon-toggle-row');
        for (const row of rows) {
            const label = row.querySelector('.icon-toggle-label');
            if (label && label.textContent.includes('ІФС')) {
                const checkbox = row.querySelector('input.switch__input');
                if (checkbox && !checkbox.checked) {
                    checkbox.click();
                    return {toggled: true, checked: checkbox.checked};
                }
                return {toggled: false, alreadyOn: true, checked: checkbox.checked};
            }
        }
        return {toggled: false, found: false};
    }""")
    print(f"    IFS toggle: {result}")
    time.sleep(1)


# Toggle label mapping (English site labels)
TOGGLE_LABELS = {
    "nato_standards": "NATO standards",
    "units": "Units",
    "headquarters": "Headquarters",
    "airfields": "Airfields",
    "direction_of_attack": "Direction of attack",
    "railways": "Railways",
    "defence_lines": "Show defence lines",
    "simplified_view": "Simplified view",
    "dark_theme": "Dark theme",
    "map_center": "Map center",
    "imperial_scale": "Imperial scale",
    "landfill_boundaries": "Landfill boundaries",
    "animations": "Animations",
}


def _set_toggle(page, label: str, desired: bool) -> None:
    """Set a settings toggle to the desired state (on/off) by its label text."""
    _dismiss_overlays(page)
    escaped = label.replace("'", "\\'")
    # Scroll into view
    page.evaluate(f"""() => {{
        const rows = document.querySelectorAll('.icon-toggle-row');
        for (const row of rows) {{
            const lbl = row.querySelector('.icon-toggle-label');
            if (lbl && lbl.textContent.includes('{escaped}')) {{
                row.scrollIntoView({{behavior: 'instant', block: 'center'}});
                break;
            }}
        }}
    }}""")
    time.sleep(0.3)
    result = page.evaluate(f"""() => {{
        const rows = document.querySelectorAll('.icon-toggle-row');
        for (const row of rows) {{
            const lbl = row.querySelector('.icon-toggle-label');
            if (lbl && lbl.textContent.includes('{escaped}')) {{
                const checkbox = row.querySelector('input.switch__input');
                if (!checkbox) return {{found: true, hasCheckbox: false}};
                const want = {'true' if desired else 'false'};
                if (checkbox.checked !== want) {{
                    checkbox.click();
                    return {{toggled: true, now: checkbox.checked}};
                }}
                return {{toggled: false, alreadyCorrect: true, checked: checkbox.checked}};
            }}
        }}
        return {{found: false}};
    }}""")
    print(f"    Toggle '{label}' -> {desired}: {result}")
    time.sleep(0.5)


def _fit_bounds(page, locations: list[tuple[str, float, float]], padding: int = 40) -> None:
    """Fit the map view to encompass all locations as tightly as possible.

    Uses fractional zoom (zoomSnap: 0) so Leaflet picks the exact zoom that
    matches the bounding box, rather than rounding down to an integer level.
    """
    bounds = [[lat, lng] for _, lat, lng in locations]
    page.evaluate(f"""() => {{
        const leafletMap = {_GET_MAP_JS};
        if (leafletMap) {{
            leafletMap.options.zoomSnap = 0;
            leafletMap.options.zoomDelta = 0.25;
            leafletMap.fitBounds({bounds}, {{padding: [{padding}, {padding}], animate: false}});
        }}
    }}""")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(3)


def _add_overview_markers(page, locations: list[tuple[str, float, float]]) -> None:
    """Add markers with greedy 8-slot label placement and leader lines.

    Each label is placed at the best of 8 candidate directions (N, NE, E, ...)
    around its marker, scored against already-placed labels, other markers,
    and viewport edges. If the best slot is far from the marker, a thin
    leader line is drawn back to the anchor.
    """
    # Inject CSS
    page.evaluate("""() => {
        if (document.getElementById('overview-style')) return;
        const style = document.createElement('style');
        style.id = 'overview-style';
        style.textContent = `
            .overview-marker {
                width: 21px;
                height: 21px;
                background: red;
                border: 2px solid #8b0000;
                border-radius: 50%;
                box-shadow: 0 0 8px rgba(0,0,0,0.6), 0 0 0 3px rgba(255,0,0,0.3);
            }
            .overview-label {
                position: absolute;
                background: rgba(0, 0, 0, 0.85);
                color: white;
                border: none;
                font-weight: bold;
                font-size: 14px;
                padding: 4px 10px;
                border-radius: 4px;
                white-space: nowrap;
                box-shadow: 0 2px 6px rgba(0,0,0,0.4);
                letter-spacing: 0.5px;
                pointer-events: none;
            }
        `;
        document.head.appendChild(style);
    }""")

    locations_json = json.dumps([{"name": n, "lat": lat, "lng": lng} for n, lat, lng in locations])

    page.evaluate(f"""() => {{
        const leafletMap = {_GET_MAP_JS};
        if (!leafletMap) return;
        const locations = {locations_json};

        // Clean up any previous overlay
        const prev = document.getElementById('overview-label-overlay');
        if (prev) prev.remove();

        const mapContainer = leafletMap.getContainer();
        const overlay = document.createElement('div');
        overlay.id = 'overview-label-overlay';
        overlay.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:10000;';
        mapContainer.appendChild(overlay);

        // SVG layer for leader lines (drawn first so labels sit above)
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;';
        overlay.appendChild(svg);

        // Add markers and record their pixel positions in the map container
        const markers = locations.map(loc => {{
            const icon = L.divIcon({{
                className: '',
                html: '<div class="overview-marker"></div>',
                iconSize: [21, 21],
                iconAnchor: [10, 10],
            }});
            L.marker([loc.lat, loc.lng], {{icon: icon, zIndexOffset: 10000}}).addTo(leafletMap);
            const pt = leafletMap.latLngToContainerPoint([loc.lat, loc.lng]);
            return {{name: loc.name, x: pt.x, y: pt.y}};
        }});

        // Create label nodes (hidden) to measure their box sizes
        const labels = markers.map(m => {{
            const el = document.createElement('div');
            el.className = 'overview-label';
            el.textContent = m.name;
            el.style.visibility = 'hidden';
            el.style.left = '0px';
            el.style.top = '0px';
            overlay.appendChild(el);
            const r = el.getBoundingClientRect();
            return {{el: el, w: r.width, h: r.height}};
        }});

        const DIRS = [
            {{name:'N',  dx: 0, dy:-1}},
            {{name:'NE', dx: 1, dy:-1}},
            {{name:'E',  dx: 1, dy: 0}},
            {{name:'SE', dx: 1, dy: 1}},
            {{name:'S',  dx: 0, dy: 1}},
            {{name:'SW', dx:-1, dy: 1}},
            {{name:'W',  dx:-1, dy: 0}},
            {{name:'NW', dx:-1, dy:-1}},
        ];
        const MARKER_R = 14;
        const RADII = [18, 36, 60, 92];
        const vpRect = mapContainer.getBoundingClientRect();
        const VW = vpRect.width, VH = vpRect.height;

        function candidateRect(marker, label, dir, radius) {{
            const len = Math.hypot(dir.dx, dir.dy) || 1;
            const ux = dir.dx / len, uy = dir.dy / len;
            const nearOff = Math.abs(ux) * label.w / 2 + Math.abs(uy) * label.h / 2;
            const cx = marker.x + ux * (radius + nearOff);
            const cy = marker.y + uy * (radius + nearOff);
            return {{
                left: cx - label.w / 2,
                top: cy - label.h / 2,
                right: cx + label.w / 2,
                bottom: cy + label.h / 2,
                cx: cx, cy: cy,
            }};
        }}

        function rectOverlap(a, b) {{
            const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            return (ox > 0 && oy > 0) ? ox * oy : 0;
        }}

        function markerBox(m) {{
            return {{left: m.x - MARKER_R, right: m.x + MARKER_R, top: m.y - MARKER_R, bottom: m.y + MARKER_R}};
        }}

        function scoreCandidate(rect, placedRects, dirIdx, radiusIdx) {{
            let score = 0;
            for (const p of placedRects) score += rectOverlap(rect, p) * 100;
            for (const m of markers) score += rectOverlap(rect, markerBox(m)) * 50;
            if (rect.left   < 4)       score += (4 - rect.left) * 500;
            if (rect.top    < 4)       score += (4 - rect.top) * 500;
            if (rect.right  > VW - 4)  score += (rect.right - (VW - 4)) * 500;
            if (rect.bottom > VH - 4)  score += (rect.bottom - (VH - 4)) * 500;
            score += radiusIdx * 40;     // prefer closer
            score += dirIdx * 0.5;       // mild N-first tiebreak
            return score;
        }}

        // Placement order: north-to-south so upper labels get priority on N slots
        const order = markers.map((_, i) => i).sort((a, b) => markers[a].y - markers[b].y || markers[a].x - markers[b].x);

        const placements = new Array(markers.length);
        const placedRects = [];
        for (const i of order) {{
            let best = null;
            for (let ri = 0; ri < RADII.length; ri++) {{
                for (let di = 0; di < DIRS.length; di++) {{
                    const rect = candidateRect(markers[i], labels[i], DIRS[di], RADII[ri]);
                    const s = scoreCandidate(rect, placedRects, di, ri);
                    if (best === null || s < best.score) {{
                        best = {{score: s, rect: rect, radius: RADII[ri], dirIdx: di}};
                    }}
                }}
                if (best && best.score < 1) break;
            }}
            placements[i] = best;
            placedRects.push(best.rect);
        }}

        // Render labels + leader lines
        for (let i = 0; i < markers.length; i++) {{
            const p = placements[i];
            const node = labels[i];
            node.el.style.visibility = 'visible';
            node.el.style.left = p.rect.left + 'px';
            node.el.style.top = p.rect.top + 'px';

            const dx = p.rect.cx - markers[i].x;
            const dy = p.rect.cy - markers[i].y;
            const dist = Math.hypot(dx, dy);
            if (dist > 32) {{
                // Anchor leader at closest point on label rect to marker
                const cx = Math.max(p.rect.left, Math.min(markers[i].x, p.rect.right));
                const cy = Math.max(p.rect.top,  Math.min(markers[i].y, p.rect.bottom));
                const line = document.createElementNS(svgNS, 'line');
                line.setAttribute('x1', markers[i].x);
                line.setAttribute('y1', markers[i].y);
                line.setAttribute('x2', cx);
                line.setAttribute('y2', cy);
                line.setAttribute('stroke', 'rgba(0,0,0,0.75)');
                line.setAttribute('stroke-width', '1.75');
                line.setAttribute('stroke-linecap', 'round');
                svg.appendChild(line);
            }}
        }}
    }}""")


def _detect_label_overlaps(page) -> list[dict]:
    """Return a list of overlapping label pairs from the current page.

    Each entry: {'a': name_a, 'b': name_b, 'overlap_px': area}. Uses the
    live DOM bounding rects of `.overview-label` elements.
    """
    return page.evaluate("""() => {
        const labels = [...document.querySelectorAll('.overview-label')];
        const items = labels.map(el => ({
            name: (el.textContent || '').trim(),
            r: el.getBoundingClientRect(),
        }));
        const overlaps = [];
        for (let i = 0; i < items.length; i++) {
            for (let j = i + 1; j < items.length; j++) {
                const a = items[i].r, b = items[j].r;
                const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                if (ox > 0 && oy > 0) {
                    overlaps.push({
                        a: items[i].name,
                        b: items[j].name,
                        overlap_px: Math.round(ox * oy),
                    });
                }
            }
        }
        return overlaps;
    }""")


def process_overview(
    page,
    location_names: list[str],
    output_dir: str,
    delay: int = 3,
    satellite: bool = False,
    show_ifs: bool = False,
    map_only: bool = False,
) -> str:
    """Produce a single overview screenshot with all locations marked."""
    # 1. Geocode all locations
    locations: list[tuple[str, float, float]] = []
    for i, name in enumerate(location_names):
        print(f"Geocoding {name!r}...")
        lat, lng, display = geocode_location(name)
        print(f"  Found: {display} ({lat:.6f}, {lng:.6f})")
        locations.append((name, lat, lng))
        # Respect Nominatim rate limit between calls
        if i < len(location_names) - 1:
            time.sleep(1)

    # 2. Compute centroid, navigate at zoom=8
    avg_lat = sum(lat for _, lat, _ in locations) / len(locations)
    avg_lng = sum(lng for _, _, lng in locations) / len(locations)
    url = build_url(avg_lat, avg_lng, zoom=8)
    print(f"  Overview URL: {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT)
    time.sleep(2)

    # 3. Dismiss overlays
    _dismiss_overlays(page)

    # 4. Open settings → configure toggles for a clean overview → close
    _open_settings_panel(page)
    # Disable clutter layers (NATO markers, units, HQs, airfields, railways)
    for key in ("nato_standards", "units", "headquarters", "airfields", "railways"):
        _set_toggle(page, TOGGLE_LABELS[key], False)
    # Keep direction of attack enabled
    _set_toggle(page, TOGGLE_LABELS["direction_of_attack"], True)
    if satellite:
        _enable_satellite(page)
    if show_ifs:
        _enable_show_ifs(page)
    _close_settings_panel(page)

    # 5. Always hide UI chrome for overview (sidebar eats viewport and skews fit)
    _hide_ui_for_map_only(page)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # 6. Fit bounds to all locations (tight padding for an aggressive crop)
    _fit_bounds(page, locations, padding=80)
    time.sleep(1)  # let Leaflet settle so latLngToContainerPoint is stable

    # 7. Add markers (after fitBounds so viewport is correct)
    _add_overview_markers(page, locations)

    # 8. Wait and final cleanup
    time.sleep(delay)
    _dismiss_overlays(page)
    time.sleep(1)

    # 8b. Detect label overlaps (for now just log them)
    label_count = page.evaluate("() => document.querySelectorAll('.overview-label').length")
    print(f"  Labels in DOM: {label_count}")
    overlaps = _detect_label_overlaps(page)
    if overlaps:
        print(f"  ⚠ {len(overlaps)} label overlap(s) detected:")
        for o in overlaps:
            print(f"    - {o['a']!r} <-> {o['b']!r}  ({o['overlap_px']} px²)")
    else:
        print("  ✓ No label overlaps detected")

    # 9. Screenshot
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{'&'.join(sanitize_filename(n) for n in location_names[:3])}_{timestamp}.png"
    output_path = os.path.join(output_dir, filename)
    page.screenshot(path=output_path, full_page=False)
    print(f"  Saved: {output_path}")
    return output_path


def _hide_ui_for_map_only(page) -> None:
    """Hide all UI chrome so only the map is visible, then resize the map."""
    page.evaluate("""() => {
        // Remove blocking overlays
        document.querySelectorAll(
            '.cl-content-locker, .overlay-backdrop, .drawer-backdrop'
        ).forEach(el => el.remove());

        // Hide all UI chrome
        [
            '.site-nav',
            'section.sidebar',
            '.mobile-toolbar',
            '.map-load-error',
            '.offline-warn',
            '.orientation-lock',
            '.leaflet-control-container',
        ].forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                el.style.display = 'none';
            });
        });

        // Make map fill entire viewport
        const map = document.getElementById('map');
        if (map) {
            map.style.left = '0';
            map.style.width = '100vw';
        }
    }""")
    # Trigger Leaflet to recalculate its size for the expanded map
    page.evaluate(f"""() => {{
        const leafletMap = {_GET_MAP_JS};
        if (leafletMap) {{
            leafletMap.invalidateSize();
        }}
        // Fallback: dispatch a resize event so Leaflet picks it up
        window.dispatchEvent(new Event('resize'));
    }}""")
    time.sleep(2)


def take_screenshot(
    page,
    url: str,
    output_path: str,
    delay: int = 3,
    satellite: bool = False,
    show_ifs: bool = False,
    map_only: bool = False,
) -> None:
    """Navigate the page to the URL and save a screenshot."""
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT)
    time.sleep(2)

    _dismiss_overlays(page)

    # Configure map settings if requested
    if satellite or show_ifs:
        _open_settings_panel(page)
        if satellite:
            _enable_satellite(page)
        if show_ifs:
            _enable_show_ifs(page)
        _close_settings_panel(page)

    # Hide UI elements for a clean map-only screenshot
    if map_only:
        _hide_ui_for_map_only(page)
        # Wait for tiles to reload after map resize
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    # Wait for map tiles to finish rendering
    time.sleep(delay)

    # Final sweep to remove any popups that appeared during the wait
    _dismiss_overlays(page)
    time.sleep(1)

    page.screenshot(path=output_path, full_page=False)


def process_location(
    page,
    name: str | None,
    lat: float | None,
    lng: float | None,
    zoom: int,
    output_dir: str,
    delay: int,
    satellite: bool = False,
    show_ifs: bool = False,
    map_only: bool = False,
) -> str:
    """Geocode (if needed), build URL, take screenshot. Returns the output path."""
    if lat is not None and lng is not None:
        label = f"{lat}_{lng}"
    elif name is not None:
        print(f"Geocoding {name!r}...")
        lat, lng, display = geocode_location(name)
        print(f"  Found: {display} ({lat:.6f}, {lng:.6f})")
        label = sanitize_filename(name)
    else:
        raise ValueError("Provide either a location name or --lat/--lng coordinates")

    url = build_url(lat, lng, zoom)
    print(f"  URL: {url}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{label}_{zoom}_{timestamp}.png"
    output_path = os.path.join(output_dir, filename)

    print("  Taking screenshot...")
    take_screenshot(
        page,
        url,
        output_path,
        delay=delay,
        satellite=satellite,
        show_ifs=show_ifs,
        map_only=map_only,
    )
    print(f"  Saved: {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Take screenshots of DeepState Map for Ukrainian locations."
    )
    parser.add_argument(
        "locations",
        nargs="*",
        help="One or more location names to screenshot (e.g. 'Vovchansk' 'Bakhmut')",
    )
    parser.add_argument(
        "-z", "--zoom", type=int, default=13, help="Map zoom level (default: 13)"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="screenshots",
        help="Output directory (default: screenshots)",
    )
    parser.add_argument("--lat", type=float, help="Latitude (skip geocoding)")
    parser.add_argument("--lng", type=float, help="Longitude (skip geocoding)")
    parser.add_argument("--width", type=int, default=1920, help="Viewport width (default: 1920)")
    parser.add_argument("--height", type=int, default=1080, help="Viewport height (default: 1080)")
    parser.add_argument(
        "--delay",
        type=int,
        default=3,
        help="Extra seconds to wait after networkidle (default: 3)",
    )
    parser.add_argument(
        "--satellite",
        action="store_true",
        help="Use satellite basemap instead of standard",
    )
    parser.add_argument(
        "--show-ifs",
        action="store_true",
        help="Enable defense structures (IFS) overlay",
    )
    parser.add_argument(
        "--map-only",
        action="store_true",
        help="Hide all UI chrome for a clean map-only screenshot",
    )
    parser.add_argument(
        "--overview",
        action="store_true",
        help="Overview mode: show all locations as markers on a single map",
    )

    args = parser.parse_args()

    if args.overview:
        if len(args.locations) < 2:
            parser.error("--overview requires at least 2 location names")
        if args.lat is not None or args.lng is not None:
            parser.error("--overview is incompatible with --lat/--lng")
    elif not args.locations and args.lat is None:
        parser.error("Provide at least one location name or --lat/--lng coordinates")

    if (args.lat is None) != (args.lng is None):
        parser.error("--lat and --lng must be used together")

    os.makedirs(args.output, exist_ok=True)

    # Overview mode: single screenshot with all locations as markers
    if args.overview:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": args.width, "height": args.height}
            )
            page = context.new_page()
            page.add_init_script(LEAFLET_CAPTURE_SCRIPT)
            try:
                process_overview(
                    page,
                    location_names=args.locations,
                    output_dir=args.output,
                    delay=args.delay,
                    satellite=args.satellite,
                    show_ifs=args.show_ifs,
                    map_only=args.map_only,
                )
            finally:
                browser.close()
        print("Done.")
        return

    # Build the list of jobs: (name_or_None, lat_or_None, lng_or_None)
    jobs: list[tuple[str | None, float | None, float | None]] = []
    if args.lat is not None:
        jobs.append((None, args.lat, args.lng))
    for loc in args.locations:
        jobs.append((loc, None, None))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height}
        )
        page = context.new_page()
        page.add_init_script(LEAFLET_CAPTURE_SCRIPT)

        for i, (name, lat, lng) in enumerate(jobs):
            try:
                process_location(
                    page,
                    name=name,
                    lat=lat,
                    lng=lng,
                    zoom=args.zoom,
                    output_dir=args.output,
                    delay=args.delay,
                    satellite=args.satellite,
                    show_ifs=args.show_ifs,
                    map_only=args.map_only,
                )
            except ValueError as e:
                print(f"  Error: {e}", file=sys.stderr)
            # Respect Nominatim rate limit (1 req/sec) between geocode calls
            if name is not None and i < len(jobs) - 1:
                time.sleep(1)

        browser.close()

    print("Done.")


if __name__ == "__main__":
    main()
