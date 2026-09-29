/* The coding view: the video, its video codes, and nothing else.
 *
 * The reader is built around the text, with the recording as a reference. Coding
 * behaviour is the other way round -- you watch, and the transcript is a
 * distraction -- so this page drops it. The video fills the space, sound can be
 * muted for coding what is on screen rather than what is said, and the same
 * `i` / `o` / `x` keys and the same files are used as in the reader, so spans
 * made here are there when you go back to the transcript.
 */

import { $, api, formatTime, recall, remember } from "./util.js";
import { initPlayer, nudge, seek, stepRate, togglePlay } from "./player.js";
import { initVideoCodes, reloadVideoCodes, videoCodeKey } from "./video_codes.js";
import { notify } from "./chrome.js";
import { ensureCoder, mountCoderControls } from "./coder.js";

const MUTE_KEY = "subtitle-search:codingMuted";

const ctx = {
  el: {
    title: $("title"),
    meta: $("meta"),
    notices: $("notices"),
    dock: $("dock"),
    dockToggle: $("dock-toggle"),
    dockGrip: null,
    dockStage: $("dock-stage"),
    media: $("media"),
    play: $("play"),
    scrub: $("scrub"),
    clock: $("clock"),
    duration: $("duration"),
    rate: $("rate"),
    mute: $("mute"),
    readerLink: $("reader-link"),
    themeToggle: $("theme-toggle"),
  },
  currentTime: 0,
  chunks: [],
  scrubbing: false,
  // The player reports mode changes for the reader's benefit; nothing follows here.
  setMode: () => {},
};

ctx.onTimeUpdate = (seconds) => ctx.onVideoCodeTime?.(seconds);

ctx.notify = (message, options) => notify(ctx.el.notices, message, options);

function syncMute() {
  const muted = ctx.el.media.muted;
  ctx.el.mute.setAttribute("aria-pressed", String(muted));
  ctx.el.mute.textContent = muted ? "🔇 Muted" : "🔊 Sound on";
}

function setMuted(muted) {
  ctx.el.media.muted = muted;
  remember(MUTE_KEY, muted ? "1" : "0");
  syncMute();
}

async function load() {
  // Every span is someone's, so nothing loads until we know who is coding.
  await ensureCoder();
  const config = await api("/api/config");
  const asked = new URLSearchParams(location.search).get("recording");
  ctx.recordingId =
    (asked && config.recordings.some((r) => r.id === asked) && asked) ||
    config.default_recording_id;
  if (!ctx.recordingId) {
    ctx.notify("No recording loaded.", { kind: "warn" });
    return;
  }

  ctx.data = await api(`/api/recordings/${ctx.recordingId}`);
  document.title = `Code the video · ${ctx.data.title}`;
  ctx.el.title.textContent = ctx.data.title;
  ctx.el.meta.textContent = [formatTime(ctx.data.duration), ctx.data.media_file || "no media"].join("  ·  ");
  ctx.el.readerLink.href = `/reader?recording=${encodeURIComponent(ctx.recordingId)}`;

  initPlayer(ctx);
  // Always the full picture here: the reader's minimized dock is its own choice.
  ctx.el.dock.dataset.state = "expanded";
  ctx.el.dockToggle.hidden = true;
  if (ctx.data.media_kind !== "video") {
    ctx.notify("This recording has no video, only audio. You can still code it by ear.");
  }

  ctx.el.media.muted = recall(MUTE_KEY, "0") === "1";
  syncMute();
  ctx.el.mute.addEventListener("click", () => setMuted(!ctx.el.media.muted));

  initVideoCodes(ctx);
  window.addEventListener("focus", () => reloadVideoCodes(ctx).catch(() => {}));
  const redraw = () => ctx.onVideoCodesModeChange?.();
  window.addEventListener("modechange", redraw);
  window.addEventListener("coderchange", () => reloadVideoCodes(ctx).then(redraw).catch(() => {}));
  // A link from the Codebook page carries the moment of the span it points at.
  const at = Number(new URLSearchParams(location.search).get("t"));
  if (Number.isFinite(at) && at > 0) seek(ctx, at);
  mountCoderControls(ctx.el.themeToggle.parentElement, {
    onRefresh: async () => {
      await reloadVideoCodes(ctx);
      ctx.notify("Refreshed from the folder.");
    },
  });
}

const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);

document.addEventListener("keydown", (event) => {
  if (!ctx.data) return;
  if (TYPING.has(event.target.tagName) || event.target.isContentEditable) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (videoCodeKey(ctx, event)) return;

  switch (event.key) {
    case " ":
      event.preventDefault();
      togglePlay(ctx);
      break;
    case "ArrowLeft":
      event.preventDefault();
      nudge(ctx, -5);
      break;
    case "ArrowRight":
      event.preventDefault();
      nudge(ctx, 5);
      break;
    case ",":
      event.preventDefault();
      nudge(ctx, -1);
      break;
    case ".":
      event.preventDefault();
      nudge(ctx, 1);
      break;
    case "[":
    case "]": {
      event.preventDefault();
      const rate = stepRate(ctx, event.key === "]" ? 1 : -1);
      if (rate) ctx.notify(`Playing at ${rate}×`);
      break;
    }
    case "m":
      event.preventDefault();
      setMuted(!ctx.el.media.muted);
      break;
    default:
      break;
  }
});

const THEME_KEY = "subtitle-search:theme";
const applyTheme = (theme) => {
  document.documentElement.dataset.theme = theme;
  remember(THEME_KEY, theme);
};
applyTheme(recall(THEME_KEY, "auto"));
ctx.el.themeToggle.addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const current = document.documentElement.dataset.theme || "auto";
  applyTheme(order[(order.indexOf(current) + 1) % order.length]);
});

load().catch((error) => {
  ctx.notify(`Could not load the recording: ${error.message}`, { kind: "warn", key: null });
});
