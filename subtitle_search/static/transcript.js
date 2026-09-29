/* Rendering the transcript, resolving selections, and driving the reading cursor.
 *
 * Every cue gets its own <span>. That is what makes an arbitrary text selection
 * resolvable: the selection's endpoints land inside cue spans, and the offset
 * within a span maps back to a point in time.
 */

import { visibleNow } from "./coder.js";
import { $, escapeHtml, formatTime, lastAtOrBefore } from "./util.js";

const READING_LINE = 0.32; // Where down the viewport "the line you are reading" sits.

/** "7 minutes", "1 hr 5 min", "45 seconds" -- how long the recording was down. */
function formatGap(seconds) {
  const total = Math.round(seconds);
  if (total < 90) return `${total} seconds`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} minutes`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} hr ${rest} min` : `${hours} hr`;
}

function clockTime(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  return when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/**
 * The marker shown where one recording ended and the next began.
 *
 * The timeline itself is continuous -- every session timestamp has audio behind
 * it -- so this is the only place the real interruption is visible. It reports
 * wall-clock time where the filenames carry it, and stays honest about not
 * knowing where they do not.
 */
function partBreak(ctx, part) {
  const row = document.createElement("div");
  row.className = "part-break";
  row.dataset.partIndex = String(part.index);

  const label = document.createElement("span");
  label.className = "part-break__label";

  const started = clockTime(part.started_at);
  if (part.gap_before != null) {
    label.textContent = `Recording resumed after ${formatGap(part.gap_before)}`;
  } else {
    label.textContent = `New recording — part ${part.index + 1} of ${ctx.parts.length}`;
  }
  if (started) {
    const stamp = document.createElement("span");
    stamp.className = "part-break__clock";
    stamp.textContent = started;
    label.appendChild(stamp);
  }

  row.appendChild(label);
  return row;
}

/**
 * Break open any block the reader has asked to see line by line.
 *
 * Splitting is a view, not an edit: nothing is written, because there is nothing
 * to record yet. A block that merged two people is still one run of one label
 * until somebody says otherwise -- this only exposes the seams so each line can
 * be assigned, and the assignment is what persists. Cue ids are stable across a
 * rebuild, so the split survives one.
 */
export function expandChunks(chunks, cueById, split) {
  if (!split.size) return chunks;
  const out = [];
  for (const chunk of chunks) {
    if (!chunk.cue_ids.some((id) => split.has(id))) {
      out.push(chunk);
      continue;
    }
    chunk.cue_ids.forEach((id, position) => {
      const cue = cueById.get(id);
      out.push({
        ...chunk,
        id: `${chunk.id}:${id}`,
        cue_ids: [id],
        paragraphs: [[id]],
        start: cue ? cue.start : chunk.start,
        end: cue ? cue.end : chunk.end,
        split: true,
        starts_part: chunk.starts_part && position === 0,
      });
    });
  }
  return out.map((chunk, index) => ({ ...chunk, index }));
}

/** Build a chunk's reading view: speaker label, then prose split by pause. */
function fillBody(ctx, chunk, body) {
  const parts = [];

  if (chunk.speaker) {
    const speaker = document.createElement("p");
    speaker.className = "chunk__speaker";
    speaker.textContent = chunk.speaker;
    parts.push(speaker);
  }

  for (const paragraph of chunk.paragraphs) {
    const para = document.createElement("p");
    para.className = "chunk__text";
    paragraph.forEach((cueId, position) => {
      const cue = ctx.cueById.get(cueId);
      if (!cue) return;
      if (position > 0) para.appendChild(document.createTextNode(" "));
      const span = document.createElement("span");
      span.className = "cue";
      span.dataset.cueId = cueId;
      span.textContent = cue.text;
      para.appendChild(span);
    });
    parts.push(para);
  }

  body.replaceChildren(...parts);
}

/** Restore one chunk's reading view, after edit mode replaced it with lines. */
export function refreshChunk(ctx, chunkIndex) {
  const chunk = ctx.chunks[chunkIndex];
  const article = ctx.chunkEls[chunkIndex];
  if (!chunk || !article) return;
  const body = article.querySelector(".chunk__body");
  if (body) fillBody(ctx, chunk, body);
}

export function renderTranscript(ctx) {
  const fragment = document.createDocumentFragment();

  for (const chunk of ctx.chunks) {
    if (chunk.starts_part && chunk.part_index > 0) {
      const part = ctx.parts[chunk.part_index];
      if (part) fragment.appendChild(partBreak(ctx, part));
    }

    const article = document.createElement("article");
    article.className = chunk.split ? "chunk chunk--split" : "chunk";
    article.dataset.chunkId = chunk.id;

    const time = document.createElement("button");
    time.type = "button";
    // A measured block is marked, because the difference between a measured and
    // an interpolated timestamp is the difference between knowing and guessing.
    // A caption with no words cannot be measured, so it must not be what stops a
    // block from reading as measured.
    const measured = chunk.cue_ids.every((id) => {
      const item = ctx.cueById.get(id);
      return !item || !item.text.trim() || item.timed;
    });
    time.className = measured ? "chunk__time chunk__time--measured" : "chunk__time";
    time.textContent = formatTime(chunk.start);
    time.title = measured
      ? "Play from here — this block's words are aligned to the audio"
      : "Play from here — times inside this block are estimated";
    article.appendChild(time);

    const body = document.createElement("div");
    body.className = "chunk__body";
    fillBody(ctx, chunk, body);

    article.appendChild(body);
    fragment.appendChild(article);
  }

  ctx.el.chunks.replaceChildren(fragment);
  cacheGeometry(ctx);
}

/** Chunk offsets are read on every scroll, so measure once instead of per frame. */
export function cacheGeometry(ctx) {
  // Only .chunk elements -- part breaks are siblings, and counting them would
  // shift every cursor index out of step with ctx.chunks.
  ctx.chunkEls = Array.from(ctx.el.chunks.querySelectorAll(".chunk"));
  ctx.chunkTops = ctx.chunkEls.map((el) => el.offsetTop);
  ctx.chunkStarts = ctx.chunks.map((chunk) => chunk.start);
  ctx.onGeometry?.();
}

/* ----------------------------------------------------------- highlights -- */

/** Which highlights touch a given cue, as offsets within that cue's text. */
function highlightSlices(ctx) {
  const slices = new Map();
  for (const highlight of ctx.highlights) {
    // Only the quotes the current mode shows are painted into the text.
    if (!visibleNow(highlight)) continue;
    const first = ctx.cueById.get(highlight.start_cue_id);
    const last = ctx.cueById.get(highlight.end_cue_id);
    if (!first || !last) continue;

    for (let index = first.index; index <= last.index; index += 1) {
      const cue = ctx.cueByIndex.get(index);
      if (!cue) continue;
      const from = index === first.index ? highlight.start_char_offset ?? 0 : 0;
      const to = index === last.index ? highlight.end_char_offset ?? cue.text.length : cue.text.length;
      if (to <= from) continue;
      if (!slices.has(cue.id)) slices.set(cue.id, []);
      slices.get(cue.id).push({ from, to, highlight });
    }
  }
  return slices;
}

/** Rebuild one cue's markup, splitting it at highlight boundaries. */
function cueHtml(cue, slices, activeId) {
  const bounds = new Set([0, cue.text.length]);
  for (const slice of slices) {
    bounds.add(Math.max(0, Math.min(slice.from, cue.text.length)));
    bounds.add(Math.max(0, Math.min(slice.to, cue.text.length)));
  }
  const points = Array.from(bounds).sort((a, b) => a - b);

  let html = "";
  for (let i = 0; i < points.length - 1; i += 1) {
    const from = points[i];
    const to = points[i + 1];
    if (to <= from) continue;
    const segment = escapeHtml(cue.text.slice(from, to));
    // Overlapping quotes are flattened rather than nested: the most recent one
    // wins the segment, so colors never stack into an unreadable muddle.
    const covering = slices.filter((slice) => slice.from <= from && slice.to >= to);
    if (!covering.length) {
      html += segment;
      continue;
    }
    const top = covering[covering.length - 1].highlight;
    const classes = ["hl", `hl--${top.color}`];
    if (top.note) classes.push("hl--noted");
    if (top.id === activeId) classes.push("hl--active");
    html += `<mark class="${classes.join(" ")}" data-highlight-id="${top.id}">${segment}</mark>`;
  }
  return html;
}

export function applyHighlights(ctx) {
  const slices = highlightSlices(ctx);
  const touched = new Set(slices.keys());

  // Reset cues that lost their highlight, so deleting a quote actually clears it.
  for (const cueId of ctx.paintedCues) {
    if (touched.has(cueId)) continue;
    const el = ctx.el.chunks.querySelector(`[data-cue-id="${cueId}"]`);
    const cue = ctx.cueById.get(cueId);
    if (el && cue) el.textContent = cue.text;
  }

  for (const [cueId, cueSlices] of slices) {
    const el = ctx.el.chunks.querySelector(`[data-cue-id="${cueId}"]`);
    const cue = ctx.cueById.get(cueId);
    if (!el || !cue) continue;
    el.innerHTML = cueHtml(cue, cueSlices, ctx.activeHighlightId);
  }

  ctx.paintedCues = touched;
  cacheGeometry(ctx);
  updateSpine(ctx);
}

/* ------------------------------------------------------------ selection -- */

export function offsetWithin(cueEl, node, offset) {
  if (node === cueEl) {
    let total = 0;
    for (let i = 0; i < offset && i < cueEl.childNodes.length; i += 1) {
      total += cueEl.childNodes[i].textContent.length;
    }
    return total;
  }
  const walker = document.createTreeWalker(cueEl, NodeFilter.SHOW_TEXT);
  let total = 0;
  let current = walker.nextNode();
  while (current) {
    if (current === node) return total + offset;
    total += current.nodeValue.length;
    current = walker.nextNode();
  }
  return total;
}

/**
 * Resolve the current selection to cue anchors.
 *
 * Uses the cue spans the range actually intersects rather than its endpoint
 * containers, so a selection that starts in whitespace or drags past the end of
 * a paragraph still anchors to real cues.
 */
export function selectionAnchors(ctx) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;

  const text = selection.toString().trim();
  if (!text) return null;

  const range = selection.getRangeAt(0);
  if (!ctx.el.chunks.contains(range.commonAncestorContainer)) return null;

  const cueEls = Array.from(ctx.el.chunks.querySelectorAll(".cue")).filter((el) =>
    range.intersectsNode(el)
  );
  if (!cueEls.length) return null;

  const startEl = cueEls[0];
  const endEl = cueEls[cueEls.length - 1];
  const startCue = ctx.cueById.get(startEl.dataset.cueId);
  const endCue = ctx.cueById.get(endEl.dataset.cueId);
  if (!startCue || !endCue) return null;

  const startOffset = startEl.contains(range.startContainer) || startEl === range.startContainer
    ? offsetWithin(startEl, range.startContainer, range.startOffset)
    : 0;
  const endOffset = endEl.contains(range.endContainer) || endEl === range.endContainer
    ? offsetWithin(endEl, range.endContainer, range.endOffset)
    : endCue.text.length;

  return {
    text,
    start_cue_id: startCue.id,
    start_char_offset: startOffset,
    end_cue_id: endCue.id,
    end_char_offset: endOffset,
    speaker: startCue.speaker,
    // Mirrors the server's estimator so the quote bar can show the time live.
    estimated_start: startCue.start + (startOffset / Math.max(1, startCue.text.length)) * (startCue.end - startCue.start),
    rect: range.getBoundingClientRect(),
  };
}

/* --------------------------------------------------------------- cursor -- */

export function chunkIndexAtScroll(ctx) {
  const reader = ctx.el.reader;
  const line = reader.scrollTop + reader.clientHeight * READING_LINE;
  return lastAtOrBefore(ctx.chunkTops, line);
}

export function chunkIndexAtTime(ctx, seconds) {
  return lastAtOrBefore(ctx.chunkStarts, seconds);
}

export function setCursor(ctx, index, { scroll = false } = {}) {
  // The transcript is moving itself, so it leads any pane scrolled with it.
  if (scroll) ctx.leadScroll?.("reader");
  const bounded = Math.max(0, Math.min(index, ctx.chunks.length - 1));
  if (bounded === ctx.cursorIndex && !scroll) return;

  const previous = ctx.chunkEls[ctx.cursorIndex];
  if (previous) previous.classList.remove("chunk--cursor");

  ctx.cursorIndex = bounded;
  const current = ctx.chunkEls[bounded];
  if (current) {
    current.classList.add("chunk--cursor");
    if (scroll) current.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  updateSpine(ctx);
  ctx.onCursorMoved?.(ctx.chunks[bounded]);
}

/** Position the spine marker over the current block. */
export function updateSpine(ctx) {
  const el = ctx.chunkEls[ctx.cursorIndex];
  const marker = ctx.el.spineMarker;
  if (!el || !marker) return;

  marker.hidden = false;
  marker.style.top = `${el.offsetTop + 8}px`;
  marker.style.height = `${Math.max(12, el.offsetHeight - 16)}px`;

  const dot = ctx.el.spineDot;
  if (!dot) return;
  if (ctx.mode !== "following") {
    dot.hidden = true;
    return;
  }
  const chunk = ctx.chunks[ctx.cursorIndex];
  const span = Math.max(0.001, chunk.end - chunk.start);
  const progress = Math.max(0, Math.min(1, (ctx.currentTime - chunk.start) / span));
  dot.hidden = false;
  dot.style.top = `${progress * 100}%`;
}

export function flashCue(ctx, cueId) {
  const el = ctx.el.chunks.querySelector(`[data-cue-id="${cueId}"]`);
  if (!el) return;
  el.classList.remove("cue--flash");
  void el.offsetWidth; // restart the animation
  el.classList.add("cue--flash");
  setTimeout(() => el.classList.remove("cue--flash"), 1600);
}

export function scrollToChunk(ctx, chunkId) {
  const index = ctx.chunks.findIndex((chunk) => chunk.id === chunkId);
  if (index < 0) return;
  setCursor(ctx, index, { scroll: true });
}
