/* Video codes: named spans of the recording's time.
 *
 * Kept apart from quotes and their tags throughout. Quotes mark words; video
 * codes mark what happened on screen, so they never paint the text. They show in
 * three places instead: a lane over the scrub bar (the whole session at a glance),
 * bands down the time spine (next to what was being said), and their own sidebar
 * tab (the list, and the codebook they come from).
 *
 * Marking is two keys, `i` for in and `o` for out, at the playhead. The codebook
 * is shared by the library; a code can carry its own key, which closes an open
 * span and codes it in one press.
 */

import { $, api, escapeHtml, formatTime } from "./util.js";
import { seek } from "./player.js";

const esc = escapeHtml;

export function initVideoCodes(ctx) {
  ctx.vc = {
    codes: [],
    colors: [],
    reserved: [],
    spans: ctx.data.video_codes || [],
    pending: null,     // session time of an `i` not yet closed by `o`
    filter: null,      // code id the list is narrowed to
    managing: false,   // codebook editor open
    active: new Set(), // span ids under the playhead
  };
  Object.assign(ctx.el, {
    tabVideoCodes: $("tab-video-codes"),
    panelVideoCodes: $("panel-video-codes"),
    videoCodeCount: $("video-code-count"),
    vcFilters: $("vc-filters"),
    vcList: $("vc-list"),
    vcManage: $("vc-manage"),
    vcLane: $("vc-lane"),
    vcPicker: $("vc-picker"),
  });

  ctx.el.tabVideoCodes.addEventListener("click", () => ctx.showTab("video-codes"));
  ctx.el.panelVideoCodes.addEventListener("click", (event) => onPanelClick(ctx, event));
  ctx.el.panelVideoCodes.addEventListener("change", (event) => onPanelChange(ctx, event));
  ctx.el.vcLane.addEventListener("pointerdown", (event) => onLanePointer(ctx, event));

  ctx.onGeometry = () => renderBands(ctx);
  ctx.onVideoCodeTime = (seconds) => markActive(ctx, seconds);

  refreshCodebook(ctx).catch((error) =>
    ctx.notify(`Could not load the video codebook: ${error.message}`, { kind: "warn" })
  );
}

async function refreshCodebook(ctx, state) {
  const book = state || (await api("/api/library/video-codebook"));
  ctx.vc.codes = book.codes;
  ctx.vc.colors = book.colors;
  ctx.vc.reserved = book.reserved_keys;
  renderAll(ctx);
}

const codeOf = (ctx, id) => ctx.vc.codes.find((c) => c.id === id);

function renderAll(ctx) {
  renderLane(ctx);
  renderBands(ctx);
  renderPanel(ctx);
}

/* ------------------------------------------------------------ keyboard -- */

/** Returns true when the key was a video-code key and has been handled. */
export function videoCodeKey(ctx, event) {
  if (!ctx.vc) return false;
  const { key } = event;

  if (ctx.vc.pending != null) {
    const code = ctx.vc.codes.find((c) => c.key && c.key === key);
    if (code) {
      event.preventDefault();
      closeSpan(ctx, code);
      return true;
    }
  }

  if (key === "i") {
    event.preventDefault();
    ctx.vc.pending = ctx.currentTime;
    renderLane(ctx);
    ctx.notify(`Video code starts at ${formatTime(ctx.vc.pending)} — press o to end it`);
    return true;
  }
  if (key === "o") {
    event.preventDefault();
    if (ctx.vc.pending == null) {
      ctx.notify("Press i first to mark where the video code starts.");
      return true;
    }
    openPicker(ctx);
    return true;
  }
  if (key === "x") {
    event.preventDefault();
    const under = spansAt(ctx, ctx.currentTime);
    if (!under.length) {
      ctx.notify("No video code under the playhead.");
      return true;
    }
    // The innermost: the one that started last is the one you are looking at.
    removeSpan(ctx, under[under.length - 1]);
    return true;
  }
  if (key === "Escape" && ctx.vc.pending != null) {
    ctx.vc.pending = null;
    renderLane(ctx);
    return false; // let the reader clear its selection too
  }
  return false;
}

function spansAt(ctx, t) {
  return ctx.vc.spans.filter((s) => s.start <= t && t < s.end);
}

/** `o` was pressed: the span is [pending, now], in whichever order they came. */
function bounds(ctx) {
  const a = ctx.vc.pending;
  const b = ctx.currentTime;
  return a <= b ? [a, b] : [b, a];
}

async function closeSpan(ctx, code) {
  const [start, end] = bounds(ctx);
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
    const matches = ctx.vc.codes.filter((c) => c.name.toLowerCase().includes(q));
    const exact = ctx.vc.codes.some((c) => c.name.toLowerCase() === q);
    return q && !exact ? [...matches, { create: input.value.trim() }] : matches;
  };
  const draw = () => {
    const items = options();
    highlighted = Math.min(highlighted, Math.max(0, items.length - 1));
    list.innerHTML = items.length
      ? items
          .map((item, i) =>
            item.create
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
    picker.hidden = true;
    picker.innerHTML = "";
  };
  const choose = async (item) => {
    close();
    let code = item;
    if (item.create) {
      try {
        const result = await api("/api/library/video-codebook", {
          method: "POST",
          body: { name: item.create },
        });
        await refreshCodebook(ctx, result);
        code = codeOf(ctx, result.code.id);
      } catch (error) {
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

function renderLane(ctx) {
  const lane = ctx.el.vcLane;
  const duration = ctx.data.duration || 1;
  const { rows, placed } = stack(ctx.vc.spans);
  lane.style.setProperty("--vc-rows", String(Math.max(1, rows)));
  const pct = (t) => `${(Math.max(0, Math.min(duration, t)) / duration) * 100}%`;

  lane.innerHTML =
    ctx.vc.spans
      .map((span) => {
        const code = codeOf(ctx, span.code_id);
        const name = code ? code.name : "unknown code";
        return (
          `<div class="vc-bar vc--${code ? code.color : "slate"}${ctx.vc.active.has(span.id) ? " vc-bar--active" : ""}" data-span="${span.id}"` +
          ` style="left:${pct(span.start)};width:calc(${pct(span.end)} - ${pct(span.start)});--vc-row:${placed.get(span.id)}"` +
          ` title="▶ ${esc(name)} · ${formatTime(span.start)}–${formatTime(span.end)}">` +
          `<span class="vc-bar__edge" data-edge="start"></span><span class="vc-bar__edge" data-edge="end"></span></div>`
        );
      })
      .join("") +
    (ctx.vc.pending != null ? `<div class="vc-pending" style="left:${pct(ctx.vc.pending)}"></div>` : "");
}

/* Click a bar to go there; drag either end to move that end. */
function onLanePointer(ctx, event) {
  const bar = event.target.closest(".vc-bar");
  if (!bar) return;
  const span = ctx.vc.spans.find((s) => s.id === bar.dataset.span);
  if (!span) return;
  const edge = event.target.closest(".vc-bar__edge")?.dataset.edge;
  if (!edge) {
    seek(ctx, span.start);
    return;
  }

  event.preventDefault();
  const lane = ctx.el.vcLane;
  const rect = lane.getBoundingClientRect();
  const duration = ctx.data.duration || 1;
  const original = { start: span.start, end: span.end };
  const at = (x) => Math.max(0, Math.min(duration, ((x - rect.left) / rect.width) * duration));
  bar.setPointerCapture(event.pointerId);

  const move = (e) => {
    const t = at(e.clientX);
    if (edge === "start") span.start = Math.min(t, span.end - 0.1);
    else span.end = Math.max(t, span.start + 0.1);
    renderLane(ctx);
  };
  const up = async () => {
    bar.removeEventListener("pointermove", move);
    bar.removeEventListener("pointerup", up);
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
}

/* --------------------------------------------------------------- spine -- */

/* Bands just left of the time spine, from the block where a span starts to the
 * block where it ends. Blocks are the only time-to-height mapping the page has,
 * so a band's ends are placed proportionally inside those blocks. */
function yAt(ctx, t) {
  const chunks = ctx.chunks;
  const els = ctx.chunkEls || [];
  if (!chunks.length || !els.length) return 0;
  let i = chunks.findIndex((c) => c.end > t);
  if (i < 0) i = chunks.length - 1;
  const chunk = chunks[i];
  const el = els[i];
  if (!el) return 0;
  if (t <= chunk.start) {
    // In the silence before this block: pin to its top.
    return el.offsetTop;
  }
  const f = Math.min(1, (t - chunk.start) / Math.max(0.001, chunk.end - chunk.start));
  return el.offsetTop + f * el.offsetHeight;
}

function renderBands(ctx) {
  const host = ctx.el.transcript;
  host.querySelectorAll(".vc-band").forEach((el) => el.remove());
  if (!ctx.vc || !ctx.chunkEls?.length) return;
  const { placed } = stack(ctx.vc.spans);
  const fragment = document.createDocumentFragment();
  for (const span of ctx.vc.spans) {
    const code = codeOf(ctx, span.code_id);
    const top = yAt(ctx, span.start);
    const bottom = Math.max(top + 6, yAt(ctx, span.end));
    const band = document.createElement("button");
    band.type = "button";
    band.className = `vc-band vc--${code ? code.color : "slate"}${ctx.vc.active.has(span.id) ? " vc-band--active" : ""}`;
    band.dataset.span = span.id;
    band.style.top = `${top}px`;
    band.style.height = `${bottom - top}px`;
    band.style.setProperty("--vc-row", String(placed.get(span.id) || 0));
    band.title = `▶ ${code ? code.name : "unknown code"} · ${formatTime(span.start)}–${formatTime(span.end)}`;
    band.setAttribute("aria-label", band.title);
    band.addEventListener("click", (event) => {
      event.stopPropagation();
      seek(ctx, span.start);
      ctx.showTab("video-codes");
      revealSpan(ctx, span.id);
    });
    fragment.appendChild(band);
  }
  host.appendChild(fragment);
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
    el.classList.toggle("vc-band--active", on && el.classList.contains("vc-band"));
    el.classList.toggle("vc-item--active", on && el.classList.contains("vc-item"));
  }
}

/* --------------------------------------------------------------- panel -- */

function renderPanel(ctx) {
  const { spans, codes } = ctx.vc;
  ctx.el.videoCodeCount.textContent = String(spans.length);

  const here = new Map();
  for (const s of spans) here.set(s.code_id, (here.get(s.code_id) || 0) + 1);
  if (ctx.vc.filter && !here.has(ctx.vc.filter)) ctx.vc.filter = null;

  ctx.el.vcFilters.innerHTML = codes
    .filter((c) => here.has(c.id))
    .map(
      (c) =>
        `<button class="tag vc-chip" type="button" data-filter="${c.id}" aria-pressed="${ctx.vc.filter === c.id}">` +
        `<span class="vc-swatch vc--${c.color}"></span>${esc(c.name)}<span class="tag__count">${here.get(c.id)}</span></button>`
    )
    .join("");

  renderManage(ctx);

  const visible = ctx.vc.filter ? spans.filter((s) => s.code_id === ctx.vc.filter) : spans;
  if (!visible.length) {
    ctx.el.vcList.innerHTML = spans.length
      ? '<p class="empty">No spans with that video code.</p>'
      : '<p class="empty">Press <kbd>i</kbd> where something starts in the video and <kbd>o</kbd> where it ends, then pick a video code. Video codes are separate from quote tags.</p>';
    return;
  }
  ctx.el.vcList.innerHTML = [...visible]
    .sort((a, b) => a.start - b.start)
    .map((span) => {
      const code = codeOf(ctx, span.code_id);
      return `
      <article class="vc-item vc-item--${code ? code.color : "slate"}${ctx.vc.active.has(span.id) ? " vc-item--active" : ""}" data-span="${span.id}">
        <div class="vc-item__head">
          <button class="vc-item__jump" type="button" data-jump="${span.id}">
            <span class="vc-swatch vc--${code ? code.color : "slate"}"></span>▶ ${esc(code ? code.name : "unknown code")}
          </button>
          <time>${formatTime(span.start)}–${formatTime(span.end)}</time>
          <span class="vc-item__len">${formatDuration(span.end - span.start)}</span>
          <button class="icon-btn" type="button" data-remove="${span.id}" aria-label="Delete">✕</button>
        </div>
        <select class="vc-item__code" data-recode="${span.id}" aria-label="Video code">
          ${codes.map((c) => `<option value="${c.id}"${c.id === span.code_id ? " selected" : ""}>${esc(c.name)}</option>`).join("")}
        </select>
        <textarea class="quote__note" rows="1" data-note="${span.id}" placeholder="Note">${esc(span.note || "")}</textarea>
      </article>`;
    })
    .join("");
}

function formatDuration(seconds) {
  return seconds < 60 ? `${Math.round(seconds)}s` : formatTime(seconds);
}

function renderManage(ctx) {
  const host = ctx.el.vcManage;
  const { codes, colors, managing } = ctx.vc;
  if (!managing) {
    host.innerHTML = `<button class="btn vc-manage__open" type="button" data-manage="open">Manage video codes (${codes.length})</button>`;
    return;
  }
  host.innerHTML =
    `<div class="vc-manage__head"><span>Video codebook · shared by the library</span>` +
    `<button class="icon-btn" type="button" data-manage="close" aria-label="Close">✕</button></div>` +
    codes
      .map(
        (c) => `
      <div class="vc-code" data-code="${c.id}">
        <select class="vc-code__color vc--${c.color}" data-field="color" aria-label="Color">
          ${colors.map((k) => `<option value="${k}"${k === c.color ? " selected" : ""}>${k}</option>`).join("")}
        </select>
        <input class="vc-code__name" data-field="name" value="${esc(c.name)}" aria-label="Name">
        <input class="vc-code__key" data-field="key" value="${esc(c.key || "")}" maxlength="1" placeholder="key" aria-label="Key">
        <span class="vc-code__count" title="Spans across the library">${c.span_count || 0}</span>
        ${
          c.span_count
            ? `<select class="vc-code__merge" data-merge="${c.id}" aria-label="Merge into">
                 <option value="">merge into…</option>
                 ${codes.filter((o) => o.id !== c.id).map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}
               </select>`
            : `<button class="icon-btn" type="button" data-delete-code="${c.id}" aria-label="Delete ${esc(c.name)}">✕</button>`
        }
        <input class="vc-code__desc" data-field="description" value="${esc(c.description || "")}" placeholder="What counts as this code" aria-label="Description">
      </div>`
      )
      .join("") +
    `<form class="vc-code vc-code--new" data-new="1">
       <input class="vc-code__name" name="name" placeholder="New video code" aria-label="New video code name">
       <button class="btn" type="submit">Add</button>
     </form>`;
  const form = host.querySelector("[data-new]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = form.elements.name.value.trim();
    if (!name) return;
    await codebookCall(ctx, "/api/library/video-codebook", "POST", { name });
  });
  // Keys typed into these fields are text, not reader commands.
  host.addEventListener("keydown", (event) => event.stopPropagation());
}

async function codebookCall(ctx, path, method, body) {
  try {
    const result = await api(path, { method, body });
    await refreshCodebook(ctx, result);
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
  const codeRow = target.closest("[data-code]");
  if (codeRow && target.dataset.field) {
    const field = target.dataset.field;
    const value = target.value.trim();
    if (field === "key" && value && rosterKeys(ctx).includes(value)) {
      ctx.notify(`'${value}' is already a speaker key.`, { kind: "warn" });
      renderPanel(ctx);
      return;
    }
    await codebookCall(ctx, `/api/library/video-codebook/${codeRow.dataset.code}`, "PATCH", {
      [field]: value,
    });
    return;
  }
  if (target.dataset.merge && target.value) {
    const from = codeOf(ctx, target.dataset.merge);
    const into = codeOf(ctx, target.value);
    if (!confirm(`Merge “${from.name}” into “${into.name}”? Every ${from.name} span in the library becomes ${into.name}.`)) {
      renderPanel(ctx);
      return;
    }
    const result = await codebookCall(ctx, `/api/library/video-codebook/${from.id}/merge`, "POST", {
      into: into.id,
    });
    if (result) {
      for (const span of ctx.vc.spans) if (span.code_id === from.id) span.code_id = into.id;
      renderAll(ctx);
      ctx.notify(`Merged ${from.name} into ${into.name} (${result.moved} spans).`);
    }
    return;
  }
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
  const filter = t.closest("[data-filter]");
  if (filter) {
    ctx.vc.filter = ctx.vc.filter === filter.dataset.filter ? null : filter.dataset.filter;
    renderPanel(ctx);
    return;
  }
  const manage = t.closest("[data-manage]");
  if (manage) {
    ctx.vc.managing = manage.dataset.manage === "open";
    renderPanel(ctx);
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
  const del = t.closest("[data-delete-code]");
  if (del) codebookCall(ctx, `/api/library/video-codebook/${del.dataset.deleteCode}`, "DELETE");
}

function revealSpan(ctx, id) {
  const item = ctx.el.vcList.querySelector(`.vc-item[data-span="${id}"]`);
  if (!item) return;
  item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  item.classList.add("vc-item--new");
  setTimeout(() => item.classList.remove("vc-item--new"), 1600);
}
