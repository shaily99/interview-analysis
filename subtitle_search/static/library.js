/* The library: every recording in the study, and one search across all of them. */

import { $, api, debounce, escapeHtml, formatTime } from "./util.js";
import { applyStoredTheme, bindThemeToggle, notify } from "./chrome.js";
import { currentMode, ensureCoder, mountCoderControls } from "./coder.js";

const el = {
  meta: $("meta"),
  notices: $("notices"),
  recordings: $("recordings"),
  recordingCount: $("recording-count"),
  tagsSection: $("tags-section"),
  tagRail: $("tag-rail"),
  tagCount: $("tag-count"),
  videoSection: $("video-section"),
  videoRail: $("video-rail"),
  videoCount: $("video-count"),
  searchInput: $("search-input"),
  searchResults: $("search-results"),
  searchCount: $("search-count"),
  resultsStrip: $("results-strip"),
};

function readerLink(recordingId, extra = "") {
  return `/reader?recording=${encodeURIComponent(recordingId)}${extra}`;
}

function recordingCard(recording, tagsByRecording, quoteCount, videoByRecording) {
  const parts = recording.part_count > 1
    ? `<span class="card__fact">${recording.part_count} recordings joined</span>`
    : "";
  const speakers = (recording.speakers || []).length
    ? `<span class="card__fact">${recording.speakers.length} speakers</span>`
    : `<span class="card__fact card__fact--warn">no speakers detected</span>`;
  const media = recording.media_file
    ? `<span class="card__fact">${recording.media_kind}</span>`
    : `<span class="card__fact card__fact--warn">no media</span>`;

  const tags = (tagsByRecording.get(recording.id) || [])
    .slice(0, 6)
    .map(([tag, count]) => `<span class="tag">${escapeHtml(tag)}<span class="tag__count">${count}</span></span>`)
    .join("");
  const video = (videoByRecording.get(recording.id) || [])
    .slice(0, 6)
    .map(([entry, count]) => `<span class="tag"><i class="strip__swatch vc--${escapeHtml(entry.color)}"></i> ${escapeHtml(entry.name)}<span class="tag__count">${count}</span></span>`)
    .join("");

  return `
    <a class="card" href="${readerLink(recording.id)}">
      <h3 class="card__title">${escapeHtml(recording.title)}</h3>
      <p class="card__facts">
        <span class="card__fact card__fact--time">${formatTime(recording.duration)}</span>
        ${speakers}${parts}${media}
      </p>
      <p class="card__quotes">
        ${quoteCount
          ? `<strong>${quoteCount}</strong> quote${quoteCount === 1 ? "" : "s"}`
          : "<span class='card__fact'>no quotes yet</span>"}
      </p>
      <div class="tags">${tags}</div>
      ${video ? `<div class="tags">${video}</div>` : ""}
    </a>`;
}

function render(data) {
  el.meta.textContent = [
    data.root || "",
    `${data.recordings.length} recordings`,
    `${data.quote_count} quotes`,
    `${data.tags.length} text codes`,
    `${(data.video || []).length} video codes`,
  ].filter(Boolean).join("  ·  ");

  // Which text codes each recording carries, for the card chips.
  const byRecording = new Map();
  for (const entry of data.tags) {
    for (const [recordingId, count] of Object.entries(entry.recordings)) {
      if (!byRecording.has(recordingId)) byRecording.set(recordingId, []);
      byRecording.get(recordingId).push([entry.tag, count]);
    }
  }
  for (const list of byRecording.values()) list.sort((a, b) => b[1] - a[1]);
  const videoByRecording = new Map();
  for (const entry of data.video || []) {
    for (const [recordingId, count] of Object.entries(entry.recordings)) {
      if (!videoByRecording.has(recordingId)) videoByRecording.set(recordingId, []);
      videoByRecording.get(recordingId).push([entry, count]);
    }
  }
  for (const list of videoByRecording.values()) list.sort((a, b) => b[1] - a[1]);

  el.recordingCount.textContent = `${data.recordings.length}`;
  el.recordings.innerHTML = data.recordings
    .map((recording) => recordingCard(recording, byRecording, data.quote_counts?.[recording.id] ?? recording.quote_count, videoByRecording))
    .join("");

  el.tagsSection.hidden = !data.tags.length;
  if (data.tags.length) {
    el.tagCount.textContent = `${data.tags.length}`;
    el.tagRail.innerHTML = data.tags
      .map(
        (entry) => `
        <a class="tag-chip" href="/themes?tag=${encodeURIComponent(entry.tag)}">
          <span class="tag-chip__name">${escapeHtml(entry.tag)}</span>
          <span class="tag-chip__counts">${entry.quote_count} in ${entry.recording_count}</span>
        </a>`
      )
      .join("");
  }

  const video = data.video || [];
  el.videoSection.hidden = !video.length;
  el.videoCount.textContent = `${video.length}`;
  el.videoRail.innerHTML = video
    .map(
      (entry) => `
        <span class="tag-chip">
          <span class="tag-chip__name"><i class="strip__swatch vc--${escapeHtml(entry.color)}"></i> ${escapeHtml(entry.name)}</span>
          <span class="tag-chip__counts">${entry.span_count} in ${entry.recording_count}</span>
        </span>`
    )
    .join("");

  for (const bad of data.unreadable || []) {
    notify(el.notices, `${bad.folder} could not be read: ${bad.reason}`, { kind: "warn" });
  }
  if (!data.recordings.length) {
    el.recordings.innerHTML = '<p class="empty">No recordings found under this folder.</p>';
  }
}

function bindSearch() {
  const run = debounce(async () => {
    const query = el.searchInput.value.trim();
    if (query.length < 2) {
      el.resultsStrip.hidden = true;
      return;
    }
    try {
      const { results } = await api(`/api/library/search?q=${encodeURIComponent(query)}`);
      el.resultsStrip.hidden = false;
      el.searchCount.textContent = `${results.length}`;
      el.searchResults.innerHTML = results.length
        ? results
            .map((hit) => {
              const before = escapeHtml(hit.snippet.slice(0, hit.match_start));
              const match = escapeHtml(hit.snippet.slice(hit.match_start, hit.match_end));
              const after = escapeHtml(hit.snippet.slice(hit.match_end));
              return `
                <a class="result" href="${readerLink(hit.recording_id, `&t=${hit.start_time}`)}">
                  <span class="result__head">
                    <span class="result__speaker">${escapeHtml(hit.recording_title)}</span>
                    <span class="result__kind">${escapeHtml(hit.speaker || "—")}</span>
                    <span class="result__time">${formatTime(hit.start_time)}</span>
                  </span>
                  <p class="result__text">${before}<mark>${match}</mark>${after}</p>
                </a>`;
            })
            .join("")
        : '<p class="empty">Nothing found in any transcript.</p>';
    } catch (error) {
      notify(el.notices, `Search failed: ${error.message}`, { kind: "warn" });
    }
  }, 180);

  el.searchInput.addEventListener("input", run);
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement !== el.searchInput) {
      event.preventDefault();
      el.searchInput.focus();
    }
  });
}

applyStoredTheme();
bindThemeToggle($("theme-toggle"));
bindSearch();
const loadLibrary = () =>
  api(`/api/library?mode=${currentMode()}`)
    .then(render)
    .catch((error) => notify(el.notices, `Could not load the library: ${error.message}`, { kind: "warn" }));
// The library is the home page, so this is where a coder first says who they are.
ensureCoder()
  .then(() => {
    mountCoderControls($("theme-toggle").parentElement, { onRefresh: loadLibrary });
    window.addEventListener("modechange", loadLibrary);
    window.addEventListener("coderchange", loadLibrary);
    loadLibrary();
  })
  .catch((error) => notify(el.notices, `Could not load the coders: ${error.message}`, { kind: "warn" }));
