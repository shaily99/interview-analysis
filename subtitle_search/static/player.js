/* The media player in the video pane.
 *
 * An interrupted session is several files, but the reader only ever sees one
 * timeline. Everything crossing this module's boundary is in *session* time;
 * picking the right file and converting to a position inside it happens here.
 */

import { formatTime, recall, remember } from "./util.js";

const DOCK_KEY = "subtitle-search:dock";
const HEIGHT_KEY = "subtitle-search:dockHeight";
export const RATE_KEY = "subtitle-search:rate";

const MIN_STAGE = 120;
const KEY_STEP = 24;

/** Cap the video so the transcript can never be squeezed out entirely. */
function boundHeight(px) {
  return Math.max(MIN_STAGE, Math.min(px, Math.round(window.innerHeight * 0.75)));
}

function setStageHeight(px) {
  const bounded = boundHeight(px);
  document.documentElement.style.setProperty("--dock-height", `${bounded}px`);
  remember(HEIGHT_KEY, bounded);
  return bounded;
}

/** Drag the grip, or focus it and use the arrow keys. */
function initResize(ctx) {
  const { dock, dockGrip, dockStage } = ctx.el;
  if (!dockGrip) return;

  const stored = Number(recall(HEIGHT_KEY));
  setStageHeight(Number.isFinite(stored) && stored > 0 ? stored : window.innerHeight * 0.34);

  let dragging = false;
  let startY = 0;
  let startHeight = 0;

  dockGrip.addEventListener("pointerdown", (event) => {
    dragging = true;
    startY = event.clientY;
    startHeight = dockStage.offsetHeight;
    // Capture keeps the drag alive when the pointer leaves the thin grip, but
    // the drag must not depend on it -- track the state explicitly instead.
    try {
      dockGrip.setPointerCapture(event.pointerId);
    } catch (_) { /* no active pointer to capture */ }
    dock.classList.add("dock--resizing");
    event.preventDefault();
  });

  dockGrip.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    // Dragging the grip upward grows the video.
    setStageHeight(startHeight + (startY - event.clientY));
  });

  const stop = (event) => {
    dragging = false;
    try {
      dockGrip.releasePointerCapture(event.pointerId);
    } catch (_) { /* never captured */ }
    dock.classList.remove("dock--resizing");
  };
  dockGrip.addEventListener("pointerup", stop);
  dockGrip.addEventListener("pointercancel", stop);
  dockGrip.addEventListener("lostpointercapture", stop);

  dockGrip.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowUp" ? KEY_STEP : event.key === "ArrowDown" ? -KEY_STEP : 0;
    if (!step) return;
    event.preventDefault();
    setStageHeight(dockStage.offsetHeight + step);
  });

  // Keep the video inside the window when the window itself shrinks.
  window.addEventListener("resize", () => setStageHeight(dockStage.offsetHeight));
}
const SEEK_LEAD_IN = 0.75; // Land just before a quote rather than inside its first word.

/** Which part contains a session time, clamped to the ends. */
export function partAt(ctx, sessionTime) {
  const parts = ctx.parts;
  if (!parts.length) return 0;
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    if (sessionTime >= parts[i].offset) return i;
  }
  return 0;
}

export function sessionTime(ctx) {
  const part = ctx.parts[ctx.activePart];
  return (part ? part.offset : 0) + (ctx.el.media.currentTime || 0);
}

function partUrl(ctx, index) {
  return `/api/recordings/${ctx.recordingId}/parts/${index}/media`;
}

/** The speed you last chose, or normal. */
export function storedRate() {
  const value = Number(recall(RATE_KEY));
  return Number.isFinite(value) && value > 0 ? value : 1;
}

/**
 * Apply a playback speed to a media element.
 *
 * Pitch correction on, or a voice at 1.5x is a cartoon and the tone you kept the
 * recording for is gone. Safari wants the prefixed spelling.
 */
export function applyRate(media, rate) {
  media.preservesPitch = true;
  media.mozPreservesPitch = true;
  media.webkitPreservesPitch = true;
  media.playbackRate = rate;
}

/** Point the element at a part and move to a position inside it. */
async function activate(ctx, index, localTime, { play = false } = {}) {
  const part = ctx.parts[index];
  if (!part || !part.media_name) return false;

  if (ctx.activePart !== index) {
    ctx.activePart = index;
    ctx.el.media.src = partUrl(ctx, index);
    ctx.el.dock.dataset.media = part.media_kind || "audio";
    // currentTime cannot be set until the new file's metadata has loaded.
    await new Promise((resolve) => {
      ctx.el.media.addEventListener("loadedmetadata", resolve, { once: true });
      ctx.el.media.addEventListener("error", resolve, { once: true });
    });
    // A new source starts at normal speed, so the chosen rate is reapplied
    // rather than quietly resetting at every interruption in a session.
    applyRate(ctx.el.media, storedRate());
  }

  try {
    ctx.el.media.currentTime = Math.max(0, localTime);
  } catch (_) { /* metadata never arrived; leave the position alone */ }
  if (play) ctx.el.media.play().catch(() => {});
  return true;
}

export function initPlayer(ctx) {
  const { media, dock, play, scrub, clock, duration, dockToggle } = ctx.el;

  ctx.parts = ctx.data.transcript.parts || [];
  ctx.activePart = -1;

  const playable = ctx.parts.filter((part) => part.media_name);
  if (!playable.length) {
    // Say so rather than just removing the player -- an unexplained missing
    // control reads as a bug, and this is usually a folder that needs its media.
    dock.hidden = true;
    ctx.notify(
      "No media file found in this folder, so there is nothing to play. The transcript still works.",
      { kind: "warn", key: null }
    );
    return;
  }

  // The transport is how you reach the recording at all, so it is always on
  // screen. Only the video surface collapses -- and with nothing to collapse on
  // an audio-only recording, the control for it goes away rather than sitting
  // there doing nothing.
  const anyVideo = playable.some((part) => part.media_kind === "video");
  // In the reader the video is a pane of its own, shown and hidden by the pane
  // layout, so it keeps no minimized state of its own.
  const fixed = dock.dataset.fixed === "true";
  const states = anyVideo ? ["minimized", "expanded"] : ["minimized"];
  dockToggle.hidden = fixed || !anyVideo;

  // Whether you want the video showing is worth remembering between sessions.
  // A stored state from an older build may name one that no longer exists.
  const stored = recall(DOCK_KEY);
  const opening = stored === "expanded" && anyVideo ? "expanded" : "minimized";
  if (!fixed) dock.dataset.state = states.includes(opening) ? opening : "minimized";
  syncDockLabel();

  duration.textContent = formatTime(ctx.data.duration);

  const rate = ctx.el.rate;
  if (rate) {
    rate.value = String(storedRate());
    applyRate(media, storedRate());
    rate.addEventListener("change", () => {
      // Speed first, remembering it second: a storage failure must not be able to
      // swallow the change the listener exists to make.
      applyRate(media, Number(rate.value));
      remember(RATE_KEY, rate.value);
    });
  }

  initResize(ctx);
  activate(ctx, 0, 0);

  dockToggle.addEventListener("click", () => {
    const next = states[(states.indexOf(dock.dataset.state) + 1) % states.length];
    dock.dataset.state = next;
    remember(DOCK_KEY, next);
    syncDockLabel();
  });

  function syncDockLabel() {
    const expanded = dock.dataset.state === "expanded";
    const label = expanded ? "Hide video" : "Show video";
    dockToggle.title = label;
    dockToggle.setAttribute("aria-label", label);
    dockToggle.setAttribute("aria-expanded", String(expanded));
    dockToggle.textContent = expanded ? "▤" : "▣";
  }

  media.addEventListener("play", () => {
    play.textContent = "❚❚";
    play.setAttribute("aria-label", "Pause");
    // Pressing play is the signal that you want the transcript to keep up.
    ctx.setMode("following");
  });

  media.addEventListener("pause", () => {
    play.textContent = "▶";
    play.setAttribute("aria-label", "Play");
  });

  // Playing off the end of one recording continues into the next, so an
  // interrupted session plays through as if it were one.
  media.addEventListener("ended", () => {
    const next = ctx.activePart + 1;
    if (next < ctx.parts.length) activate(ctx, next, 0, { play: true });
  });

  media.addEventListener("timeupdate", () => {
    const now = sessionTime(ctx);
    ctx.currentTime = now;
    clock.textContent = formatTime(now);
    if (!ctx.scrubbing && ctx.data.duration) {
      scrub.value = String(Math.round((now / ctx.data.duration) * 1000));
    }
    ctx.onTimeUpdate?.(now);
  });

  play.addEventListener("click", () => togglePlay(ctx));

  scrub.addEventListener("input", () => {
    ctx.scrubbing = true;
    clock.textContent = formatTime((scrub.value / 1000) * ctx.data.duration);
  });

  scrub.addEventListener("change", () => {
    ctx.scrubbing = false;
    seek(ctx, (scrub.value / 1000) * ctx.data.duration);
    ctx.setMode("following");
  });
}

export function togglePlay(ctx) {
  const { media } = ctx.el;
  if (!media.src) return;
  if (media.paused) media.play().catch(() => {});
  else media.pause();
}

/** Move to a session time, switching recordings if it lands in another part. */
export function seek(ctx, target, { play = false } = {}) {
  const bounded = Math.max(0, target);
  const index = partAt(ctx, bounded);
  const part = ctx.parts[index];
  if (!part) return;
  return activate(ctx, index, bounded - part.offset, { play });
}

/**
 * Move the playhead without starting playback.
 *
 * This is what "the player is always cued to what you are reading" means: while
 * you read, the position follows silently, so play always starts where you are.
 */
export function cue(ctx, target) {
  const { media } = ctx.el;
  if (!media.src || !media.paused) return;
  if (Math.abs(sessionTime(ctx) - target) < 0.25) return;
  seek(ctx, target);
}

/** Jump to a point and play it, with a lead-in so the first word is not clipped. */
export function seekAndPlay(ctx, target, { play = false } = {}) {
  if (play) ctx.setMode("following");
  seek(ctx, Math.max(0, target - SEEK_LEAD_IN), { play });
}

/** Step to the next or previous speed the control offers. */
export function stepRate(ctx, direction) {
  const select = ctx.el.rate;
  if (!select) return null;
  const options = [...select.options].map((option) => option.value);
  const next = Math.max(0, Math.min(options.length - 1, options.indexOf(select.value) + direction));
  select.value = options[next];
  applyRate(ctx.el.media, Number(select.value));
  remember(RATE_KEY, select.value);
  return Number(select.value);
}

export function nudge(ctx, delta) {
  if (!ctx.el.media.src) return;
  seek(ctx, Math.min(ctx.data.duration, Math.max(0, sessionTime(ctx) + delta)));
}
