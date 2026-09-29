/* Video codes: named spans of the recording's time.
 *
 * Kept apart from text codes throughout; spans never paint the text. They show
 * as code rows under the video, one row per coder (common, you, then others in
 * collaborative mode), over a zoomable window that follows the playhead, and in
 * the List tab.
 *
 * Marking is `i` for in and `o` for out, at the playhead. Each coder has their
 * own video codebook; a code can carry its own key, which closes an open span
 * and codes it in one press.
 */

import { $, api, escapeHtml, formatTime, recall, remember } from "./util.js";
import { seek } from "./player.js";
import { coderTag, currentCoder, initialsOf, isCommon, isMine, nameOf, visibleNow } from "./coder.js";
import { openMoveDialog, returnToCoders } from "./commondialog.js";
import { renderStrip } from "./codestrip.js";

const esc = escapeHtml;

//: How much of the recording the code rows show at once, in seconds.
const WINDOW_KEY = "subtitle-search:tierWindow";
const WINDOWS = [10, 20, 30, 60, 120, 300, 600, 1800];
const ROW_H = 22;

export function initVideoCodes(ctx) {
  ctx.vc = {
    codes: [],
    colors: [],
    reserved: [],
    spans: ctx.data.video_codes || [],
    pending: null,     // session time of an `i` not yet closed by `o`
    out: null,         // session time of that `o`, while its code is being chosen
    filter: null,      // code id the list is narrowed to
    active: new Set(), // span ids under the playhead
    window: WINDOWS.includes(Number(recall(WINDOW_KEY))) ? Number(recall(WINDOW_KEY)) : 60,
    from: 0, // session time at the left edge of the code rows
  };
  Object.assign(ctx.el, {
    panelVideoCodes: $("panel-video-codes"),
    videoCodeCount: $("video-code-count"),
    vcFilters: $("vc-filters"),
    vcList: $("vc-list"),
    vcManage: $("vc-manage"),
    vcTiers: $("vc-tiers"),
    vcZoom: $("vc-zoom"),
    vcPicker: $("vc-picker"),
  });

  ctx.el.panelVideoCodes.addEventListener("click", (event) => onPanelClick(ctx, event));
  ctx.el.panelVideoCodes.addEventListener("change", (event) => onPanelChange(ctx, event));
  ctx.el.vcTiers?.addEventListener("pointerdown", (event) => onTierPointer(ctx, event));
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("#vc-pop, .vc-bar")) closeSpanPopup();
  });
  // Esc closes the pop-up before anything else it would mean.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.getElementById("vc-pop")) {
      event.stopPropagation();
      closeSpanPopup();
    }
  }, true);
  ctx.el.vcZoom?.addEventListener("click", (event) => {
    const step = event.target.closest("[data-zoom]")?.dataset.zoom;
    if (!step) return;
    const i = WINDOWS.indexOf(ctx.vc.window) + (step === "in" ? -1 : 1);
    ctx.vc.window = WINDOWS[Math.max(0, Math.min(WINDOWS.length - 1, i))];
    remember(WINDOW_KEY, ctx.vc.window);
    follow(ctx, ctx.currentTime, { force: true });
    renderTiers(ctx);
  });
  // The reader's lower-left pane switches between the code rows and the list.
  ctx.showVideoCodesTab = (which) => {
    ctx.el.panelVideoCodes.querySelectorAll("[data-vc-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.vcTab === which)));
    ctx.el.panelVideoCodes.querySelectorAll("[data-vc-view]").forEach((v) => (v.hidden = v.dataset.vcView !== which));
    if (which === "rows") renderTiers(ctx);
  };

  ctx.onVideoCodesModeChange = () => {
    ctx.vc.active = new Set();
    renderAll(ctx);
  };
  ctx.onVideoCodeTime = (seconds) => {
    markActive(ctx, seconds);
    // Not while an edge is being dragged: redrawing would drop the drag.
    if (!ctx.vc.dragging && follow(ctx, seconds)) renderTiers(ctx);
    else placePlayhead(ctx, seconds);
  };

  refreshCodebook(ctx).catch((error) =>
    ctx.notify(`Could not load the video codebook: ${error.message}`, { kind: "warn" })
  );
}

/**
 * Re-read spans and codebook from disk.
 *
 * Done when the reader comes back into focus, so edits made in another tab
 * (the Codebook page, another recording) show up.
 */
export async function reloadVideoCodes(ctx) {
  if (!ctx.vc || ctx.vc.pending != null || !ctx.el.vcPicker.hidden) return;
  const { spans } = await api(`/api/recordings/${ctx.recordingId}/video-codes`);
  ctx.vc.spans = spans;
  await refreshCodebook(ctx);
}

async function refreshCodebook(ctx, state) {
  const book = state || (await api("/api/library/video-codebook"));
  ctx.vc.codes = book.codes;
  ctx.vc.colors = book.colors;
  ctx.vc.reserved = book.reserved_keys;
  renderAll(ctx);
}

const codeOf = (ctx, id) => ctx.vc.codes.find((c) => c.id === id);
//: The spans the current mode shows: yours, or everyone's in collaborative mode.
const shownSpans = (ctx) => ctx.vc.spans.filter(visibleNow);
//: You code with your own video codes in every mode.
const myCodes = (ctx) => ctx.vc.codes.filter(isMine);
const whoLabel = (span) => (isMine(span) ? "" : ` · ${nameOf(span.coder)}`);

function renderAll(ctx) {
  renderTiers(ctx);
  renderPanel(ctx);
}

/* ------------------------------------------------------------ keyboard -- */

/** Returns true when the key was a video-code key and has been handled. */
export function videoCodeKey(ctx, event) {
  if (!ctx.vc) return false;
  const { key } = event;

  if (ctx.vc.pending != null) {
    const code = myCodes(ctx).find((c) => c.key && c.key === key);
    if (code) {
      event.preventDefault();
      closeSpan(ctx, code);
      return true;
    }
  }

  if (key === "i") {
    event.preventDefault();
    ctx.vc.pending = ctx.currentTime;
    renderTiers(ctx);
    ctx.notify(`Video code starts at ${formatTime(ctx.vc.pending)} — press o to end it`);
    return true;
  }
  if (key === "o") {
    event.preventDefault();
    if (ctx.vc.pending == null) {
      ctx.notify("Press i first to mark where the video code starts.");
      return true;
    }
    // Pause while choosing, so the moment being coded is still on screen and
    // nothing plays past unwatched. The end is fixed here, at the keypress, not
    // whenever the choice is made.
    ctx.vc.out = ctx.currentTime;
    ctx.el.media.pause();
    openPicker(ctx);
    return true;
  }
  if (key === "x") {
    event.preventDefault();
    // Only your own spans can be deleted; other coders' are read-only.
    const under = spansAt(ctx, ctx.currentTime).filter(isMine);
    if (!under.length) {
      ctx.notify("None of your video codes is under the playhead.");
      return true;
    }
    // The innermost: the one that started last is the one you are looking at.
    removeSpan(ctx, under[under.length - 1]);
    return true;
  }
  if (key === "Escape" && ctx.vc.pending != null) {
    ctx.vc.pending = null;
    renderTiers(ctx);
    return false; // let the reader clear its selection too
  }
  return false;
}

function spansAt(ctx, t) {
  return shownSpans(ctx).filter((s) => s.start <= t && t < s.end);
}

/** The span is [start, end], in whichever order `i` and `o` came. */
function bounds(ctx) {
  const a = ctx.vc.pending;
  const b = ctx.vc.out ?? ctx.currentTime;
  return a <= b ? [a, b] : [b, a];
}

async function closeSpan(ctx, code) {
  const [start, end] = bounds(ctx);
  ctx.vc.out = null;
  if (end - start < 0.1) {
    ctx.notify("That span is empty — play on a little before pressing o.");
    return;
  }
  try {
    const { span } = await api(`/api/recordings/${ctx.recordingId}/video-codes`, {
      method: "POST",
      body: { code_id: code.id, start, end },
    });
    ctx.vc.pending = null;
    ctx.vc.spans.push(span);
    code.span_count = (code.span_count || 0) + 1;
    renderAll(ctx);
    ctx.notify(`${code.name} · ${formatTime(start)}–${formatTime(end)}`);
  } catch (error) {
    ctx.notify(`Could not save the video code: ${error.message}`, { kind: "warn" });
  }
}

async function removeSpan(ctx, span) {
  try {
    await api(`/api/recordings/${ctx.recordingId}/video-codes/${span.id}`, { method: "DELETE" });
  } catch (error) {
    ctx.notify(`Could not delete it: ${error.message}`, { kind: "warn" });
    return;
  }
  ctx.vc.spans = ctx.vc.spans.filter((s) => s.id !== span.id);
  const code = codeOf(ctx, span.code_id);
  if (code) code.span_count = Math.max(0, (code.span_count || 1) - 1);
  renderAll(ctx);
  ctx.notify(`Deleted ${code ? code.name : "video code"} at ${formatTime(span.start)}`, {
    action: {
      label: "Undo",
      onAct: async () => {
        const { span: back } = await api(`/api/recordings/${ctx.recordingId}/video-codes`, {
          method: "POST",
          body: { code_id: span.code_id, start: span.start, end: span.end, note: span.note },
        });
        ctx.vc.spans.push(back);
        if (code) code.span_count += 1;
        renderAll(ctx);
      },
    },
  });
}

/* -------------------------------------------------------------- picker -- */

/* A single choice from the codebook, or a new code typed in. Deliberately not
 * the tag field: a span has exactly one code, and this list offers only video
 * codes, never tags. */
function openPicker(ctx) {
  const picker = ctx.el.vcPicker;
  const [start, end] = bounds(ctx);
  picker.hidden = false;
  picker.innerHTML =
    `<div class="vc-picker__head">▶ Video code · ${formatTime(start)}–${formatTime(end)}</div>` +
    `<input class="vc-picker__input" id="vc-picker-input" placeholder="Pick or type a new code" autocomplete="off" spellcheck="false">` +
    `<div class="vc-picker__list" id="vc-picker-list" role="listbox"></div>`;
  const input = $("vc-picker-input");
  const list = $("vc-picker-list");
  let highlighted = 0;

  const options = () => {
    const q = input.value.trim().toLowerCase();
    const matches = myCodes(ctx).filter((c) => c.name.toLowerCase().includes(q));
    const exact = myCodes(ctx).some((c) => c.name.toLowerCase() === q);
    // Common codes are offered by name: picking one codes with your own
    // same-named code, which is later merged into the common one.
    const mine = new Set(myCodes(ctx).map((c) => c.name.toLowerCase()));
    const common = ctx.vc.codes
      .filter((c) => isCommon(c) && !mine.has(c.name.toLowerCase()) && c.name.toLowerCase().includes(q))
      .map((c) => ({ create: c.name, common: c }));
    const typedIsCommon = common.some((c) => c.create.toLowerCase() === q);
    return [...matches, ...common, ...(q && !exact && !typedIsCommon ? [{ create: input.value.trim() }] : [])];
  };
  const draw = () => {
    const items = options();
    highlighted = Math.min(highlighted, Math.max(0, items.length - 1));
    list.innerHTML = items.length
      ? items
          .map((item, i) =>
            item.common
              ? `<button type="button" class="vc-picker__opt" data-i="${i}" aria-selected="${i === highlighted}"><span class="vc-swatch vc--${item.common.color}"></span>✓ ${esc(item.create)}<kbd>use</kbd></button>`
              : item.create
              ? `<button type="button" class="vc-picker__opt" data-i="${i}" aria-selected="${i === highlighted}">+ New code “${esc(item.create)}”</button>`
              : `<button type="button" class="vc-picker__opt" data-i="${i}" aria-selected="${i === highlighted}">` +
                `<span class="vc-swatch vc--${item.color}"></span>${esc(item.name)}` +
                (item.key ? `<kbd>${esc(item.key)}</kbd>` : "") +
                `</button>`
          )
          .join("")
      : `<p class="vc-picker__none">Type a name to create the first video code.</p>`;
  };
  const close = () => {
    ctx.vc.out = null; // a later `o` sets a new end; the start stays open
    picker.hidden = true;
    picker.innerHTML = "";
  };
  const choose = async (item) => {
    const end = ctx.vc.out;
    close();
    ctx.vc.out = end;
    let code = item;
    if (item.create) {
      try {
        const result = await api("/api/library/video-codebook", {
          method: "POST",
          // A code taken from common keeps its colour and description.
          body: { name: item.create, ...(item.common ? { color: item.common.color, description: item.common.description } : {}) },
        });
        await refreshCodebook(ctx, result);
        code = codeOf(ctx, result.code.id);
      } catch (error) {
        ctx.vc.out = null;
        ctx.notify(`Could not add that code: ${error.message}`, { kind: "warn" });
        return;
      }
    }
    closeSpan(ctx, code);
  };

  input.addEventListener("input", () => {
    highlighted = 0;
    draw();
  });
  input.addEventListener("keydown", (event) => {
    event.stopPropagation(); // typing here must not trigger reader keys
    const items = options();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlighted = Math.min(items.length - 1, highlighted + 1);
      draw();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlighted = Math.max(0, highlighted - 1);
      draw();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (items[highlighted]) choose(items[highlighted]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close(); // the start stays pending, so `o` can be pressed again
    }
  });
  input.addEventListener("blur", () => setTimeout(() => !picker.contains(document.activeElement) && close(), 150));
  list.addEventListener("mousedown", (event) => {
    const opt = event.target.closest("[data-i]");
    if (!opt) return;
    event.preventDefault();
    choose(options()[Number(opt.dataset.i)]);
  });

  draw();
  input.focus();
}

/* ---------------------------------------------------------------- lane -- */

/** Overlapping spans go on separate rows, greedily, earliest first. */
function stack(spans) {
  const rowEnds = [];
  const placed = new Map();
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    let row = rowEnds.findIndex((end) => end <= span.start);
    if (row < 0) row = rowEnds.push(0) - 1;
    rowEnds[row] = span.end;
    placed.set(span.id, row);
  }
  return { rows: rowEnds.length, placed };
}

/* ---------------------------------------------------------- code rows -- */

/* One row per coder -- common first when it has anything, then you, then other
 * coders in collaborative mode -- over a stretch of the recording that follows
 * the playhead. A span too short for its name is cut off with an ellipsis; hover
 * shows the code, the coder and the times. Click a span to go to it, or empty
 * space to go to that moment; drag the ends of your own spans to move them. */

const durationOf = (ctx) => ctx.data?.duration || 1;
const shown = (ctx) => Math.min(ctx.vc.window, durationOf(ctx));

/** Keep the playhead in view; returns whether the stretch shown moved. */
function follow(ctx, t, { force = false } = {}) {
  const win = shown(ctx);
  const from = ctx.vc.from;
  if (!force && t >= from && t <= from + win * 0.9) return false;
  const next = Math.max(0, Math.min(durationOf(ctx) - win, t - win * 0.2));
  ctx.vc.from = next;
  return next !== from || force;
}

const pctOf = (ctx, t) => ((t - ctx.vc.from) / shown(ctx)) * 100;

function placePlayhead(ctx, t) {
  const line = ctx.el.vcTiers?.querySelector(".tier-playhead");
  if (line) line.style.left = `${Math.max(0, Math.min(100, pctOf(ctx, t)))}%`;
}

function tickStep(win) {
  return [1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((s) => win / s <= 7) || 900;
}

function coderRows(ctx) {
  const spans = shownSpans(ctx);
  const rows = [];
  const common = spans.filter(isCommon);
  if (common.length) rows.push({ label: `<em class="who who--common">✓</em> Common`, spans: common });
  rows.push({ label: `<em class="who">${esc(initialsOf(currentCoder()?.id))}</em> You`, spans: spans.filter(isMine) });
  const others = [...new Set(spans.filter((s) => !isMine(s) && !isCommon(s)).map((s) => s.coder))];
  for (const coder of others.sort((a, b) => nameOf(a).localeCompare(nameOf(b)))) {
    rows.push({ label: `${coderTag(coder)} ${esc(nameOf(coder))}`, spans: spans.filter((s) => s.coder === coder) });
  }
  return rows;
}

function renderTiers(ctx) {
  const host = ctx.el.vcTiers;
  if (!host || !ctx.vc) return;
  if (ctx.el.vcZoom) {
    const win = shown(ctx);
    ctx.el.vcZoom.innerHTML =
      `<button type="button" data-zoom="in" aria-label="Show less time">−</button>` +
      `<span>${formatTime(ctx.vc.from)}–${formatTime(ctx.vc.from + win)}</span>` +
      `<button type="button" data-zoom="out" aria-label="Show more time">+</button>`;
  }
  const win = shown(ctx);
  const from = ctx.vc.from;
  const step = tickStep(win);
  const ticks = [];
  for (let t = Math.ceil(from / step) * step; t <= from + win; t += step) {
    ticks.push(`<span style="left:${pctOf(ctx, t)}%">${formatTime(t)}</span>`);
  }
  const rows = coderRows(ctx)
    .map((row) => {
      const inView = row.spans.filter((s) => s.end > from && s.start < from + win);
      const { rows: depth, placed } = stack(inView);
      const bars = inView
        .map((span) => {
          const code = codeOf(ctx, span.code_id);
          const name = code ? code.name : "unknown code";
          const left = Math.max(0, pctOf(ctx, span.start));
          const right = Math.min(100, pctOf(ctx, span.end));
          // Only a real end is draggable: a span running past the edge of the
          // stretch shown is cut off there, and that cut is not its end.
          const edges = isMine(span)
            ? (span.start >= from ? `<span class="vc-bar__edge" data-edge="start"></span>` : "") +
              (span.end <= from + win ? `<span class="vc-bar__edge" data-edge="end"></span>` : "")
            : "";
          return (
            `<div class="vc-bar tier-bar vc--${code ? code.color : "slate"}${ctx.vc.active.has(span.id) ? " vc-bar--active" : ""}${isMine(span) ? "" : " vc-bar--theirs"}" data-span="${span.id}"` +
            ` style="left:${left}%;width:${Math.max(0.4, right - left)}%;top:${placed.get(span.id) * ROW_H + 3}px"` +
            ` title="${esc(name)}${esc(whoLabel(span))} · ${formatTime(span.start)}–${formatTime(span.end)}"><span class="tier-bar__name">${esc(name)}</span>${edges}</div>`
          );
        })
        .join("");
      return `<div class="tier"><div class="tier__lab">${row.label}</div>
        <div class="tier__lane" style="height:${Math.max(1, depth) * ROW_H + 6}px">${bars}</div></div>`;
    })
    .join("");
  const pending = ctx.vc.pending != null && ctx.vc.pending >= from && ctx.vc.pending <= from + win
    ? `<div class="tier-pending" style="left:${pctOf(ctx, ctx.vc.pending)}%"></div>` : "";
  host.innerHTML =
    `<div class="tiers-grid"><div class="tier tier--ruler"><div class="tier__lab"></div><div class="tier__lane tier__ruler">${ticks.join("")}</div></div>` +
    rows +
    `<div class="tier-overlay"><div class="tier-playhead"></div>${pending}</div></div>`;
  placePlayhead(ctx, ctx.currentTime);
}

/* Click a span to go there, or an empty stretch to go to that moment; drag
 * either end of your own span to move that end. */
function onTierPointer(ctx, event) {
  const lane = event.target.closest(".tier__lane");
  if (!lane || lane.classList.contains("tier__ruler")) return;
  const rect = lane.getBoundingClientRect();
  const at = (x) => Math.max(0, Math.min(durationOf(ctx), ctx.vc.from + ((x - rect.left) / rect.width) * shown(ctx)));
  const bar = event.target.closest(".vc-bar");
  if (!bar) {
    seek(ctx, at(event.clientX));
    return;
  }
  const span = ctx.vc.spans.find((s) => s.id === bar.dataset.span);
  if (!span) return;
  const edge = isMine(span) ? event.target.closest(".vc-bar__edge")?.dataset.edge : null;
  if (!edge) {
    seek(ctx, span.start);
    openSpanPopup(ctx, span, bar);
    return;
  }

  event.preventDefault();
  const original = { start: span.start, end: span.end };
  ctx.vc.dragging = true;
  bar.setPointerCapture(event.pointerId);
  const move = (e) => {
    const t = at(e.clientX);
    if (edge === "start") span.start = Math.min(t, span.end - 0.1);
    else span.end = Math.max(t, span.start + 0.1);
    const left = Math.max(0, pctOf(ctx, span.start));
    bar.style.left = `${left}%`;
    bar.style.width = `${Math.max(0.4, Math.min(100, pctOf(ctx, span.end)) - left)}%`;
  };
  const up = async () => {
    bar.removeEventListener("pointermove", move);
    bar.removeEventListener("pointerup", up);
    bar.removeEventListener("pointercancel", up);
    bar.removeEventListener("lostpointercapture", up);
    if (!ctx.vc.dragging) return;
    ctx.vc.dragging = false;
    if (span.start === original.start && span.end === original.end) return;
    try {
      const { span: saved } = await api(`/api/recordings/${ctx.recordingId}/video-codes/${span.id}`, {
        method: "PATCH",
        body: { start: span.start, end: span.end },
      });
      Object.assign(span, saved);
    } catch (error) {
      Object.assign(span, original);
      ctx.notify(`Could not move that edge: ${error.message}`, { kind: "warn" });
    }
    renderAll(ctx);
  };
  bar.addEventListener("pointermove", move);
  bar.addEventListener("pointerup", up);
  // A drag the browser cancels still saves where it got to, so the screen and
  // the file never disagree.
  bar.addEventListener("pointercancel", up);
  bar.addEventListener("lostpointercapture", up);
}

/* -------------------------------------------------------- span pop-up -- */

// Clicking a span on the rows opens this beside it: your own span can be
// recoded, noted or deleted; anyone else's is shown read-only.
function closeSpanPopup() {
  const pop = document.getElementById("vc-pop");
  // Closing saves a note or time still being typed, so a click away never drops it.
  pop?.save?.();
  pop?.remove();
}

/** A time as the pop-up's boxes show it: m:ss.t, or h:mm:ss.t. */
function clockText(seconds) {
  const tenths = Math.round(seconds * 10);
  return `${formatTime(Math.floor(tenths / 10))}.${tenths % 10}`;
}

/** Read m:ss, h:mm:ss or plain seconds, each with optional decimals; null if it is not a time. */
function parseClock(text) {
  const parts = text.trim().split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  return parts.reduce((total, p) => total * 60 + Number(p), 0);
}

function openSpanPopup(ctx, span, bar) {
  closeSpanPopup();
  const own = isMine(span);
  const pop = document.createElement("div");
  pop.id = "vc-pop";
  pop.className = "vc-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Video code");
  // The span as last saved; a focus reload can replace the object this opened with.
  const live = () => ctx.vc.spans.find((s) => s.id === span.id) || span;
  const head = () => {
    const now = live();
    const code = codeOf(ctx, now.code_id);
    return `<span class="vc-swatch vc--${code ? code.color : "slate"}"></span>
      <b>${esc(code ? code.name : "unknown code")}</b>${own ? "" : coderTag(now.coder)}
      <time>${formatTime(now.start)}–${formatTime(now.end)}</time>
      <span class="vc-item__len">${formatDuration(now.end - now.start)}</span>`;
  };
  pop.innerHTML = `<div class="vc-pop__head">${head()}</div>` + (own
    ? `<div class="vc-pop__times">
        <label>Start <input type="text" data-time="start" value="${clockText(span.start)}" spellcheck="false"></label>
        <label>End <input type="text" data-time="end" value="${clockText(span.end)}" spellcheck="false"></label>
      </div>
      <select class="vc-item__code" aria-label="Video code">${myCodes(ctx)
        .map((c) => `<option value="${c.id}"${c.id === span.code_id ? " selected" : ""}>${esc(c.name)}</option>`)
        .join("")}</select>
      <textarea class="quote__note" rows="2" placeholder="Note">${esc(span.note || "")}</textarea>
      <button class="btn btn--danger" type="button" data-pop="delete">Delete</button>`
    : span.note ? `<p class="quote__note-text">${esc(span.note)}</p>` : "");
  document.body.appendChild(pop);

  const r = bar.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8))}px`;
  const below = r.bottom + 4;
  pop.style.top = `${below + pop.offsetHeight > innerHeight ? Math.max(8, r.top - pop.offsetHeight - 4) : below}px`;

  if (!own) return;
  pop.querySelector("select").addEventListener("change", async (event) => {
    await patchSpan(ctx, span.id, { code_id: event.target.value });
    pop.querySelector(".vc-pop__head").innerHTML = head();
  });
  const note = pop.querySelector("textarea");
  const saveNote = () => {
    if (note.value !== (live().note || "")) patchSpan(ctx, span.id, { note: note.value });
  };
  const saveTime = async (input) => {
    const which = input.dataset.time;
    const current = live();
    const value = parseClock(input.value);
    if (value === null || Math.abs(value - current[which]) < 0.05) {
      input.value = clockText(current[which]);
      return;
    }
    const start = which === "start" ? value : current.start;
    const end = which === "end" ? value : current.end;
    if (end - start < 0.1 || end > durationOf(ctx)) {
      ctx.notify("The end has to come after the start, within the recording.", { kind: "warn" });
      input.value = clockText(current[which]);
      return;
    }
    await patchSpan(ctx, span.id, { [which]: value });
    renderAll(ctx);
    input.value = clockText(live()[which]);
    pop.querySelector(".vc-pop__head").innerHTML = head();
  };
  const times = [...pop.querySelectorAll("[data-time]")];
  for (const input of times) {
    input.addEventListener("change", () => saveTime(input));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") input.blur();
    });
  }
  pop.save = () => {
    saveNote();
    times.forEach(saveTime);
  };
  note.addEventListener("change", saveNote);
  pop.querySelector('[data-pop="delete"]').addEventListener("click", () => {
    pop.save = null;
    closeSpanPopup();
    removeSpan(ctx, span);
  });
}

/* ------------------------------------------------------ active marking -- */

function markActive(ctx, seconds) {
  if (!ctx.vc) return;
  const now = new Set(spansAt(ctx, seconds).map((s) => s.id));
  const same = now.size === ctx.vc.active.size && [...now].every((id) => ctx.vc.active.has(id));
  if (same) return;
  ctx.vc.active = now;
  for (const el of document.querySelectorAll("[data-span]")) {
    const on = now.has(el.dataset.span);
    el.classList.toggle("vc-bar--active", on && el.classList.contains("vc-bar"));
    el.classList.toggle("vc-item--active", on && el.classList.contains("vc-item"));
  }
}

/* --------------------------------------------------------------- panel -- */

function renderPanel(ctx) {
  const spans = shownSpans(ctx);
  ctx.el.videoCodeCount.textContent = String(spans.length);

  const here = new Map();
  for (const s of spans) here.set(s.code_id, (here.get(s.code_id) || 0) + 1);
  // Your own codes always show, so a new one can be edited before it is used;
  // other coders' only where they appear on a span in view.
  const stripCodes = ctx.vc.codes
    .filter((c) => (isMine(c) || isCommon(c) ? true : visibleNow(c) && here.has(c.id)))
    .map((c) => ({ ...c, count: here.get(c.id) || 0, total: c.span_count }));
  if (ctx.vc.filter && !stripCodes.some((c) => c.id === ctx.vc.filter)) ctx.vc.filter = null;
  if (ctx.el.vcManage) ctx.el.vcManage.innerHTML = "";
  renderStrip(ctx.el.vcFilters, {
    codes: stripCodes,
    filter: ctx.vc.filter,
    colors: ctx.vc.colors,
    withKeys: true,
    noun: "video code",
    kind: "video",
    empty: "No video codes yet. Press i and o on the video to code your first span.",
    onFilter: (id) => {
      ctx.vc.filter = id;
      renderPanel(ctx);
    },
    onAction: (action, code, value) => codebookAction(ctx, action, code, value),
  });

  const visible = ctx.vc.filter ? spans.filter((s) => s.code_id === ctx.vc.filter) : spans;
  if (!visible.length) {
    ctx.el.vcList.innerHTML = spans.length
      ? '<p class="empty">No spans with that video code.</p>'
      : '<p class="empty">Press <kbd>i</kbd> where something starts in the video and <kbd>o</kbd> where it ends, then pick a video code. Video codes are separate from text codes.</p>';
    return;
  }
  const mine = myCodes(ctx);
  ctx.el.vcList.innerHTML = [...visible]
    .sort((a, b) => a.start - b.start)
    .map((span) => {
      const code = codeOf(ctx, span.code_id);
      const own = isMine(span);
      return `
      <article class="vc-item vc-item--${code ? code.color : "slate"}${ctx.vc.active.has(span.id) ? " vc-item--active" : ""}${own ? "" : " vc-item--theirs"}" data-span="${span.id}">
        <div class="vc-item__head">
          <button class="vc-item__jump" type="button" data-jump="${span.id}">
            <span class="vc-swatch vc--${code ? code.color : "slate"}"></span>▶ ${esc(code ? code.name : "unknown code")}
          </button>
          ${own ? "" : coderTag(span.coder)}
          <time>${formatTime(span.start)}–${formatTime(span.end)}</time>
          <span class="vc-item__len">${formatDuration(span.end - span.start)}</span>
          ${own ? `<button class="icon-btn" type="button" data-remove="${span.id}" aria-label="Delete">✕</button>` : ""}
        </div>
        ${
          own
            ? `<select class="vc-item__code" data-recode="${span.id}" aria-label="Video code">
          ${mine.map((c) => `<option value="${c.id}"${c.id === span.code_id ? " selected" : ""}>${esc(c.name)}</option>`).join("")}
        </select>
        <textarea class="quote__note" rows="1" data-note="${span.id}" placeholder="Note">${esc(span.note || "")}</textarea>`
            : span.note
              ? `<p class="quote__note-text">${esc(span.note)}</p>`
              : ""
        }
      </article>`;
    })
    .join("");
}

/** The strip's ⋯ menu, applied to your own video codebook or to a common code. */
async function codebookAction(ctx, action, code, value) {
  const base = `/api/library/video-codebook/${code.id}`;
  if (action === "move" || action === "return") {
    try {
      const done =
        action === "move"
          ? await openMoveDialog({ kind: "video", code, common: ctx.vc.codes.filter(isCommon) })
          : await returnToCoders("video", code);
      if (!done) return;
      ctx.vc.spans = (await api(`/api/recordings/${ctx.recordingId}/video-codes`)).spans;
      await refreshCodebook(ctx);
      ctx.notify(action === "move" ? `Moved “${code.name}” to common.` : `Returned ✓ ${code.name} to its coders.`);
    } catch (error) {
      ctx.notify(error.message, { kind: "warn" });
    }
    return;
  }
  if (action === "key" && value && rosterKeys(ctx).includes(value.trim())) {
    ctx.notify(`'${value.trim()}' is already a speaker key.`, { kind: "warn" });
    renderPanel(ctx);
    return;
  }
  const body = { rename: { name: value }, describe: { description: value }, color: { color: value }, key: { key: value } }[action];
  if (body) return codebookCall(ctx, base, "PATCH", body);
  if (action === "delete") return codebookCall(ctx, base, "DELETE");
  if (action === "merge") {
    const result = await codebookCall(ctx, `${base}/merge`, "POST", { into: value });
    if (result) {
      for (const span of ctx.vc.spans) if (span.code_id === code.id) span.code_id = value;
      renderAll(ctx);
      ctx.notify(`Merged “${code.name}” (${result.moved} span${result.moved === 1 ? "" : "s"}).`);
    }
  }
}

function formatDuration(seconds) {
  return seconds < 60 ? `${Math.round(seconds)}s` : formatTime(seconds);
}

async function codebookCall(ctx, path, method, body) {
  try {
    const result = await api(path, { method, body });
    await refreshCodebook(ctx, result);
    // It may have been a common code, which the header counts until pushed.
    window.dispatchEvent(new CustomEvent("commonchange"));
    return result;
  } catch (error) {
    ctx.notify(error.message, { kind: "warn" });
    renderPanel(ctx); // put back what the failed edit showed
    return null;
  }
}

function rosterKeys(ctx) {
  return (ctx.data?.transcript?.roster || []).map((entry) => entry.key).filter(Boolean);
}

async function onPanelChange(ctx, event) {
  const target = event.target;
  if (target.dataset.recode) {
    await patchSpan(ctx, target.dataset.recode, { code_id: target.value });
    return;
  }
  if (target.dataset.note) {
    await patchSpan(ctx, target.dataset.note, { note: target.value });
  }
}

async function patchSpan(ctx, id, body) {
  const span = ctx.vc.spans.find((s) => s.id === id);
  try {
    const { span: saved } = await api(`/api/recordings/${ctx.recordingId}/video-codes/${id}`, {
      method: "PATCH",
      body,
    });
    Object.assign(span, saved);
    if ("code_id" in body) await refreshCodebook(ctx);
  } catch (error) {
    ctx.notify(`Could not save that: ${error.message}`, { kind: "warn" });
    renderAll(ctx);
  }
}

function onPanelClick(ctx, event) {
  const t = event.target;
  const tab = t.closest("[data-vc-tab]");
  if (tab) {
    ctx.showVideoCodesTab(tab.dataset.vcTab);
    return;
  }
  const jump = t.closest("[data-jump]");
  if (jump) {
    const span = ctx.vc.spans.find((s) => s.id === jump.dataset.jump);
    if (span) seek(ctx, span.start);
    return;
  }
  const remove = t.closest("[data-remove]");
  if (remove) {
    const span = ctx.vc.spans.find((s) => s.id === remove.dataset.remove);
    if (span) removeSpan(ctx, span);
    return;
  }
}

