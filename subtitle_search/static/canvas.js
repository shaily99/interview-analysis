/* Themes as a plane instead of a row of columns.
 *
 * The board ran out of screen. Columns only work while they all fit, and past
 * about six themes the useful ones are off the right-hand edge -- which is the
 * point at which the interface starts deciding what you think about. A plane has
 * no such edge: it pans, it zooms, and a theme that matters can be put in the
 * middle where you keep looking.
 *
 * What the plane also gets you is the thing a column list cannot express at all.
 * Themes sit next to the themes they resemble; two areas nudged up against each
 * other is a claim you are making about them, and an outlier parked between two
 * areas is a code you have not decided about yet. None of that is a field in the
 * file. It is the arrangement, and the arrangement is the analysis.
 *
 * Three ideas hold the whole thing up:
 *
 *   A card is not a code. It is one appearance of a code, at one position, so
 *   the same code can sit inside two areas at once -- two cards, one code.
 *
 *   A card in an area is a DOM child of that area, positioned relative to it.
 *   That is what makes "themes bring their cards with them" free rather than
 *   bookkeeping: dragging an area changes two numbers and every card in it
 *   moves, and no card can be left behind by a bug in the moving code.
 *
 *   Every drag is the same drag. One pointer capture on the viewport, one ghost
 *   element following the cursor in screen space. Dragging a code out of the
 *   tray, out of one area into another, and back to the tray to put it away are
 *   the same code path, so they cannot behave differently.
 */

import { coderTag, currentMode, isCommon, isMine } from "./coder.js";
import { $, api, debounce, escapeHtml, formatTime, recall, remember } from "./util.js";

const VIEW_KEY = "subtitle-search:canvas-view";
const TRAY_KEY = "subtitle-search:canvas-tray";
const GRID_KEY = "subtitle-search:canvas-grid";

//: Pointer travel before a press becomes a drag. Without it, clicking a card's
//: play button on a trackpad regularly moves the card a pixel and saves that.
const SLOP = 4;

//: Spacing of the surface's dot grid, in canvas units.
const GRID = 24;

const ZOOM_MIN = 0.2;
const ZOOM_MAX = 2;
const ZOOM_STEP = 1.15;

//: How far a card moves under the arrow keys, and under shift for fine work.
const NUDGE = 12;
const NUDGE_FINE = 2;

//: Long enough that dragging a card around does not write the file on every
//: frame, short enough that letting go and closing the tab keeps the position.
const SAVE_AFTER = 400;

let ctx = null;
const el = {};

/** Pan and zoom. A view of the work, not the work -- so it stays in this browser. */
const view = { x: 0, y: 0, z: 1 };

/** The drag in progress, or null. One at a time, by construction. */
let drag = null;

/** Which card the keyboard is on, as ``ref`` and the area holding it. */
let focused = null;

/** Whether the view has been framed on the work at least once. */
let framed = false;

/**
 * Grid view: every area's cards drawn packed, and not one position written.
 *
 * Free placement is the point of the plane and it is also how an area ends up
 * unreadable. This is the way to read it without giving up the arrangement that
 * made it unreadable -- the stored positions are untouched, and turning it off
 * puts everything back exactly where it was.
 *
 * Packed by speaker and then by time, which is the other half of what it is
 * for: an area laid out that way is one voice at a time in the order it was
 * said, and you can see where the theme stops being one person's. It is the
 * order the server tidies into as well, so this doubles as a preview of what
 * tidying that area would commit.
 */
let gridView = false;

/* ------------------------------------------------------------- geometry -- */

/**
 * The card and area sizes the server laid the file out with.
 *
 * Not restated here with defaults. The server owns these numbers because it
 * placed the themes in a file that had no coordinates, and a second copy of
 * them in the client is exactly the bug the arrangement cannot survive -- a
 * card measured differently at the two ends drops into the wrong slot. Anything
 * that needs them runs after the first load, which is where they arrive.
 */
const metrics = () => ctx.state.metrics;

/** Where a client point lands on the plane. */
function toCanvas(clientX, clientY) {
  const rect = el.viewport.getBoundingClientRect();
  return {
    x: (clientX - rect.left - view.x) / view.z,
    y: (clientY - rect.top - view.y) / view.z,
  };
}

//: Panning fires on every frame, and storage is not free -- so where you are
//: looking is written a moment after you stop looking around.
const rememberView = debounce(() => remember(VIEW_KEY, JSON.stringify(view)), 300);

function applyView() {
  el.surface.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`;
  // Published to CSS so an area's title can undo the zoom and stay one size on
  // screen. A quote shrinking as you pull back is fine -- you are not reading it
  // from there -- but a theme's name is what you navigate by, and a plane whose
  // labels become illegible exactly when you zoom out to see all of them has
  // given up the thing it was for.
  el.surface.style.setProperty("--z", String(view.z));
  el.surface.style.setProperty("--inv-z", String(1 / view.z));
  // The dot grid is painted by the viewport rather than the surface, so it can
  // run endlessly in all four directions without a vast element to carry it.
  // Coarser when zoomed far out, where a fine grid would read as grey fog.
  const pitch = (view.z < 0.5 ? GRID * 4 : GRID) * view.z;
  el.viewport.style.backgroundSize = `${pitch}px ${pitch}px`;
  el.viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
  el.zoomLevel.textContent = `${Math.round(view.z * 100)}%`;
  fitChrome();
  rememberView();
}

//: Screen widths at which a title bar gives something up, in the order it gives
//: them. Measured rather than guessed: what fits depends on the area's width
//: *and* the zoom, and an area seen at 45% is only a couple of hundred pixels
//: across however wide it is on the plane.
const TIGHT = 340;
const CRAMPED = 250;
const BARE = 190;

/**
 * Make room for the theme's name by dropping everything less important.
 *
 * The bar does not scale, so zooming out does not shrink it -- it runs out of
 * area to sit in instead, and something has to yield. The name never does: it is
 * what you navigate by, and a row of half-truncated titles is the failure this
 * whole arrangement was meant to avoid.
 *
 * The name does not need a row to itself to be safe, though, which is what the
 * first version of this assumed. It wraps, and the area grows to hold it, so a
 * narrow bar costs it a line rather than its ending. What it needed was for the
 * *buttons* to stay put: they were on the second row, that row goes below 340px,
 * and an area seen at the zoom this page opens at is about 230px across -- so
 * tidy and delete were, in practice, never on screen at all.
 *
 * So the order of sacrifice is: the note and the recording spread, then the
 * count, and only on a bar too narrow to press anything, the buttons. Losing the
 * second row also pulls the bar back inside the room reserved for it and stops
 * it covering the top row of cards. Zooming in brings it all back.
 */
function fitChrome() {
  for (const node of el.surface.querySelectorAll(".area")) {
    const width = node.offsetWidth * view.z;
    node.classList.toggle("area--tight", width < TIGHT);
    node.classList.toggle("area--cramped", width < CRAMPED);
    node.classList.toggle("area--bare", width < BARE);
  }
  fitTitles();

  // How tall the area has to be to hold its own title bar.
  //
  // The two live in different units, and that is the whole of the arithmetic
  // here. The bar renders one layout pixel to one screen pixel, because its 1/z
  // undoes the surface's z. The area does not: its height is in canvas units and
  // renders at z of that. So a bar of H pixels needs H / z canvas units under
  // it, and using H directly -- as is tempting -- leaves the box short by
  // exactly the zoom.
  //
  // Rolled up, that *is* the height: the area is its bar and nothing else.
  // Otherwise it is a floor, reached only when zoom or a wrapped name has made
  // the bar taller than the box, and an area seen from a distance becomes a
  // labelled tile. Neither is written down; zooming in undoes both.
  for (const node of el.surface.querySelectorAll(".area")) {
    const chrome = node.querySelector(".area__chrome");
    if (!chrome) continue;
    const needed = `${chrome.offsetHeight / view.z}px`;
    const collapsed = node.classList.contains("area--collapsed");
    if (collapsed) {
      node.style.height = needed;
      node.style.minHeight = "";
    } else {
      node.style.minHeight = needed;
    }
  }
}

//: The title's size on screen, and the floor it is allowed to fall to when a
//: single long word will not fit an area however it is wrapped.
const TITLE_PX = 13;
const TITLE_MIN_PX = 9;

//: An offscreen context, for asking how wide a word is without laying anything
//: out. Measuring the real field instead would mean a write, a forced reflow and
//: a read per area on every wheel tick.
const ruler = document.createElement("canvas").getContext("2d");

function widestWord(text, font) {
  ruler.font = font;
  return text
    .split(/\s+/)
    .reduce((widest, word) => Math.max(widest, ruler.measureText(word).width), 0);
}

/**
 * Let a long name wrap, and shrink it only when even that will not do.
 *
 * Zoomed far out there is genuinely not the width for "Nobody opens the
 * spreadsheet" on one line, so the field wraps -- which is why it is a textarea
 * and not an input -- and its height is followed here rather than by
 * ``field-sizing``, which is too new to depend on.
 *
 * Wrapping runs out too. Below about a quarter zoom an area is a hundred pixels
 * across and a single word can be wider than that, and then there are only three
 * things you can do to it: cut it, break it mid-word, or make it smaller. Smaller
 * is the only one that leaves it readable, so the type gives way at exactly the
 * point it has to and not one zoom step sooner -- across the whole range anyone
 * works at, a theme's name is the same size it is in the sidebar.
 */
function fitTitles() {
  for (const field of el.surface.querySelectorAll(".area__title")) {
    field.style.fontSize = "";
    // The bar is laid out at z times the area's width and drawn back at 1/z, so
    // its layout pixels *are* screen pixels. No conversion, at any zoom.
    const room = field.clientWidth;
    const needed = widestWord(field.value, getComputedStyle(field).font);
    if (room > 0 && needed > room) {
      field.style.fontSize = `${Math.max(TITLE_MIN_PX, TITLE_PX * (room / needed))}px`;
    }

    field.style.height = "auto";
    // scrollHeight is content plus padding; the box is border-box. Without the
    // borders back the field lands two pixels short and clips its last line.
    const borders = field.offsetHeight - field.clientHeight;
    field.style.height = `${field.scrollHeight + borders}px`;
  }
}

function restoreView() {
  try {
    const saved = JSON.parse(recall(VIEW_KEY, "null"));
    if (saved && Number.isFinite(saved.z)) Object.assign(view, saved);
  } catch (_) { /* nothing remembered, or nonsense remembered */ }
  view.z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.z || 1));
}

/** Zoom about a fixed client point, so the thing under the cursor stays put. */
function zoomAt(clientX, clientY, factor) {
  const before = toCanvas(clientX, clientY);
  view.z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.z * factor));
  const after = toCanvas(clientX, clientY);
  view.x += (after.x - before.x) * view.z;
  view.y += (after.y - before.y) * view.z;
  applyView();
}

/** Frame everything that is on the canvas, or come back to the origin if nothing is. */
function fit() {
  const { card_w, card_h } = metrics();
  const boxes = [
    ...ctx.state.themes.map(areaBox).map((t) => [t.x, t.y, t.w, t.h]),
    ...ctx.state.cards
      .filter((c) => !c.theme_id)
      .map((c) => [c.x, c.y, card_w, card_h]),
  ];
  const rect = el.viewport.getBoundingClientRect();
  if (!boxes.length || !rect.width) {
    Object.assign(view, { x: 24, y: 24, z: 1 });
    applyView();
    return;
  }

  const left = Math.min(...boxes.map((b) => b[0]));
  const top = Math.min(...boxes.map((b) => b[1]));
  const right = Math.max(...boxes.map((b) => b[0] + b[2]));
  const bottom = Math.max(...boxes.map((b) => b[1] + b[3]));
  const pad = 40;
  view.z = Math.min(
    ZOOM_MAX,
    Math.max(ZOOM_MIN, Math.min(rect.width / (right - left + pad * 2), rect.height / (bottom - top + pad * 2)))
  );
  view.x = (rect.width - (right - left) * view.z) / 2 - left * view.z;
  view.y = (rect.height - (bottom - top) * view.z) / 2 - top * view.z;
  applyView();
}

/* -- packing -------------------------------------------------------------- */

/* The same packing the server performs when it tidies an area, computed from the
 * numbers the server sent. Not a second opinion about layout: one formula over
 * one set of constants, so grid view previews what tidying would write. */

function packColumns(width) {
  const { card_w, card_gap, area_pad } = metrics();
  return Math.max(1, Math.floor((width - 2 * area_pad + card_gap) / (card_w + card_gap)));
}

function packSlot(index, columns) {
  const { card_w, card_h, card_gap, area_pad, area_head } = metrics();
  return {
    x: area_pad + (index % columns) * (card_w + card_gap),
    y: area_head + Math.floor(index / columns) * (card_h + card_gap),
  };
}

function packHeight(count, width) {
  const { card_h, card_gap, area_pad, area_head } = metrics();
  const rows = Math.max(1, Math.ceil(Math.max(1, count) / packColumns(width)));
  return area_head + rows * (card_h + card_gap) - card_gap + area_pad;
}

/**
 * Where a card falls when an area is packed: by speaker, then by time.
 *
 * Who said it first, because a theme read down a column of one voice at a time
 * is a theme you can argue with -- the same person's three remarks about trust
 * sit together, and the place where somebody else takes over is visible. Then
 * time, so each voice runs in the order it was said rather than in the order it
 * happened to be dragged out.
 *
 * Quotes with nobody attributed sort last: they are the ones to fix, not the
 * ones to read first. Recording only breaks ties, so that the same cards always
 * come out in the same order.
 *
 * The server applies the same rule when it tidies an area for good. Two
 * spellings of one sentence, kept in step by ``packing_key`` in library.py --
 * which is the cheaper mistake, since the alternative is asking the server to
 * re-sort on every redraw of a view that writes nothing at all.
 */
function packingKey(card) {
  // Code cards pack alphabetically, text codes before video, as tidying does.
  const item = ctx.state.byRef.get(card.ref);
  if (!item) return [2, "", 0, ""];
  return [0, (item.name || "").toLowerCase(), item.kind === "video" ? 1 : 0, ""];
}

function packingOrder(cards) {
  return [...cards].sort((a, b) => {
    const ka = packingKey(a);
    const kb = packingKey(b);
    for (let i = 0; i < ka.length; i += 1) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
    return 0;
  });
}

/** Where an area's cards are drawn: as placed, or packed for grid view. */
function laidOut(theme, cards) {
  if (!gridView) return cards;
  const columns = packColumns(theme.w);
  return packingOrder(cards).map((card, index) => ({ ...card, ...packSlot(index, columns) }));
}

/**
 * The height to draw an area at, from state alone.
 *
 * Kept free of the DOM on purpose: this decides the inline height in the markup,
 * and a version that measured the previous render would feed its own output back
 * in and ratchet upwards. Rolled up, CSS sizes the box to its bar and nothing is
 * written here at all.
 */
function drawnHeight(theme) {
  if (theme.collapsed) return null;
  if (!gridView) return theme.h;
  const count = ctx.state.cards.filter((c) => c.theme_id === theme.id).length;
  return Math.max(theme.h, packHeight(count, theme.w));
}

/**
 * The box an area actually occupies, which is not always the one it stores.
 *
 * Rolled up it is as tall as its title bar; in grid view as tall as the packing
 * needs; and zoomed far out, as tall as a wrapped title has made that bar. All
 * three are drawing decisions rather than edits, so the stored height stays put
 * and this reports what is on screen -- which is what a drop has to be tested
 * against, and the only version of the box that is right in every case.
 */
function areaBox(theme) {
  const node = el.surface.querySelector(`.area[data-theme="${CSS.escape(theme.id)}"]`);
  // offsetWidth/Height are in canvas units: layout ignores the transform.
  if (node) return { ...theme, w: node.offsetWidth, h: node.offsetHeight };
  return { ...theme, h: drawnHeight(theme) ?? metrics().area_head };
}

/**
 * Which area a point on the plane falls in.
 *
 * Areas do not nest, so overlap is only ever clutter to be dragged apart -- but
 * while it is there, the smallest area containing the point is the one meant.
 * A big area sitting behind a small one is a backdrop, not the target.
 */
function areaAt(point) {
  return ctx.state.themes
    .map(areaBox)
    .filter(
      (t) =>
        point.x >= t.x && point.x <= t.x + t.w && point.y >= t.y && point.y <= t.y + t.h
    )
    .sort((a, b) => a.w * a.h - b.w * b.h)[0] || null;
}

/** Keep a card's own position inside the area it belongs to. */
function clampToArea(theme, x, y) {
  const { card_w, card_h, area_pad, area_head } = metrics();
  return {
    x: Math.min(Math.max(x, area_pad), Math.max(area_pad, theme.w - area_pad - card_w)),
    y: Math.min(Math.max(y, area_head), Math.max(area_head, theme.h - area_pad - card_h)),
  };
}

/* ------------------------------------------------------------ rendering -- */

/**
 * The way back to where a quote was said.
 *
 * A new tab, always. Following it in place would throw away the canvas -- the
 * pan, the zoom, what was selected, a filter halfway through being narrowed --
 * to answer a question that is usually "wait, what came before this?". The point
 * of checking the context is to come back with it, and the arrangement should
 * still be there when you do.
 */
/** A code's page, where every quote or span carrying it is listed. */
function readerLink(item) {
  return `<a class="icon-btn" data-act="open" target="_blank" rel="noopener"
             href="/codebook?kind=${item.kind}&code=${encodeURIComponent(item.id)}"
             title="Open this code's page, in a new tab">↗</a>`;
}

//: Code cards opened to show their quotes or spans, by card.
const expanded = new Set();
const cardKey = (card) => `${card.ref}|${card.theme_id || ""}`;
const noun = (item) => (item.kind === "video" ? "span" : "quote");

/** The card as it appears on the plane: a code, which opens to show its evidence. */
function cardMarkup(item, card) {
  const open = expanded.has(cardKey(card));
  const uses = `${item.count} ${noun(item)}${item.count === 1 ? "" : "s"} · ${item.recordings.length} rec`;
  const applications = open
    ? `<ol class="ccard__apps">${item.applications
        .map((a) => {
          const link = item.kind === "video"
            ? `/code?recording=${encodeURIComponent(a.recording_id)}&t=${a.start_time}`
            : `/reader?recording=${encodeURIComponent(a.recording_id)}&t=${a.start_time}`;
          return `<li><a href="${link}" target="_blank" rel="noopener">${escapeHtml(a.recording_title)} · ${formatTime(a.start_time)}</a>${
            a.text ? ` ${escapeHtml(a.text)}` : ""}</li>`;
        })
        .join("") || "<li>Nothing carries this code yet.</li>"}</ol>`
    : "";
  return `
    <article class="ccard ccard--code vc--${escapeHtml(item.color || "slate")}${open ? " ccard--open" : ""}"
             data-ref="${escapeHtml(item.ref)}"
             data-theme="${escapeHtml(card.theme_id || "")}"
             style="left:${card.x}px; top:${card.y}px"
             tabindex="0" role="group"
             aria-label="${escapeHtml(item.name)}, ${escapeHtml(uses)}">
      <p class="ccard__text">${item.kind === "video" ? '<span class="ccard__video" title="Video code">▶</span> ' : ""}${escapeHtml(item.name)} ${coderTag(item.coder)}</p>
      <footer class="ccard__foot">
        <span class="ccard__who">${escapeHtml(uses)}</span>
        <button class="icon-btn" data-act="expand" type="button" aria-expanded="${open}"
                title="${open ? "Hide" : "Show"} its ${noun(item)}s">${open ? "▴" : "▾"}</button>
        <button class="icon-btn" data-act="play" type="button" title="Play every ${noun(item)} with this code">▷</button>
        ${readerLink(item)}
      </footer>
      ${applications}
    </article>`;
}

/**
 * An area: a boundary drawn on the surface, with its title bar on top.
 *
 * The chrome is one element rather than a header and a note side by side,
 * because it has to be counter-scaled as a unit -- one transform, and one opaque
 * background so that when zooming out makes the bar taller in canvas units than
 * the room reserved for it, it reads as a title bar over the cards rather than
 * as something broken.
 *
 * Rolled up, the area gets no height at all and CSS sizes it to that bar. A
 * quote can still be dropped on it: the theme is closed, not shut.
 */
function areaMarkup(theme, cards) {
  const spread = new Set(cards.flatMap((c) => ctx.state.byRef.get(c.ref)?.recordings || []));
  // Whose theme this is: yours, common, or (read-only) another coder's.
  const owner = theme.coder;
  const readOnly = owner && !isMine(theme) && !isCommon(theme);
  const height = drawnHeight(theme);
  const shown = theme.collapsed ? [] : laidOut(theme, cards);
  return `
    <section class="area${theme.collapsed ? " area--collapsed" : ""}${isCommon(theme) ? " area--common" : ""}${readOnly ? " area--theirs" : ""}"
             data-theme="${escapeHtml(theme.id)}"
             style="left:${theme.x}px; top:${theme.y}px; width:${theme.w}px${
               height === null ? "" : `; height:${height}px`
             }">
      <div class="area__chrome" data-handle="move">
        <span class="area__grab" title="Drag to move this theme"></span>
        <header class="area__head">
          <button class="icon-btn area__roll" data-act="collapse" type="button"
                  aria-expanded="${theme.collapsed ? "false" : "true"}"
                  title="${theme.collapsed ? "Open this theme" : "Roll this theme up to its title"}"
                  >${theme.collapsed ? "▸" : "▾"}</button>
          <textarea class="area__title" data-act="rename" rows="1" wrap="soft"
                    spellcheck="false" aria-label="Theme name" ${readOnly ? "readonly" : ""}
                    >${escapeHtml(theme.title)}</textarea>
          ${owner && !isMine(theme) ? coderTag(owner) : ""}
          <span class="area__count" title="codes in this theme">${cards.length}</span>
          <span class="area__tools">
            <button class="icon-btn" data-act="play-theme" type="button"
                    title="Play every quote and span in this theme">▷</button>
            ${readOnly ? "" : `<button class="icon-btn" data-act="tidy" type="button"
                    title="Tidy: pack these alphabetically, for good">⊞</button>`}
            ${isMine(theme) ? `<button class="icon-btn" data-act="promote" type="button"
                    title="Move this theme to common…">✓</button>` : ""}
            ${readOnly ? "" : `<button class="icon-btn" data-act="delete" type="button"
                    title="Delete this area">✕</button>`}
          </span>
        </header>
        <div class="area__sub">
          <input class="area__note" value="${escapeHtml(theme.note || "")}" data-act="note"
                 placeholder="What is this theme?" aria-label="What this theme is" ${readOnly ? "readonly" : ""}>
          <span class="area__spread">${spread.size}/${ctx.state.recordings.size} rec</span>
        </div>
      </div>
      ${shown
        .map((card) => {
          const quote = ctx.state.byRef.get(card.ref);
          return quote ? cardMarkup(quote, card) : "";
        })
        .join("")}
      ${theme.collapsed || cards.length ? "" : '<p class="area__empty">Drag codes in here.</p>'}
      ${theme.collapsed ? "" : '<span class="area__grip" data-handle="resize" title="Resize"></span>'}
    </section>`;
}

export function renderCanvas() {
  if (!ctx || !ctx.state.metrics) return;

  // Card size comes from the server, which laid out the file, so the stylesheet
  // is told rather than asked. Restating it in CSS would be one number in two
  // places, and a card a pixel wider there than here drops in the wrong slot.
  const { card_w, card_h } = metrics();
  el.surface.style.setProperty("--card-w", `${card_w}px`);
  el.surface.style.setProperty("--card-h", `${card_h}px`);

  const byTheme = new Map(ctx.state.themes.map((t) => [t.id, []]));
  const loose = [];
  for (const card of ctx.state.cards) {
    if (card.theme_id && byTheme.has(card.theme_id)) byTheme.get(card.theme_id).push(card);
    else if (!card.theme_id) loose.push(card);
  }

  el.surface.innerHTML =
    ctx.state.themes.map((theme) => areaMarkup(theme, byTheme.get(theme.id))).join("") +
    loose
      .map((card) => {
        const quote = ctx.state.byRef.get(card.ref);
        return quote ? cardMarkup(quote, card) : "";
      })
      .join("");

  // An empty plane explains nothing about itself, and this is the one screen in
  // the tool where there is no content to infer the gesture from.
  el.blank.hidden = Boolean(ctx.state.themes.length || ctx.state.cards.length);

  // Two counts, because they answer different questions: how much of the study
  // is filed, and how much is out on the plane but still undecided.
  const undecided = ctx.state.onCanvas.size - ctx.state.placed.size;
  el.progress.textContent = ctx.state.codes.length
    ? `${ctx.state.placed.size} of ${ctx.state.codes.length} codes in a theme` +
      (undecided > 0 ? ` · ${undecided} loose` : "")
    : "no codes yet";

  fitChrome();
  renderTray();
  markMatches();
  if (focused) {
    const node = cardNode(focused.ref, focused.theme_id);
    if (node) node.classList.add("ccard--focus");
    else focused = null;
  }
  showInspector();
}

function cardNode(ref, themeId) {
  return el.surface.querySelector(
    `.ccard[data-ref="${CSS.escape(ref)}"][data-theme="${CSS.escape(themeId || "")}"]`
  );
}

/* ----------------------------------------------------------------- tray -- */

/**
 * The quotes not out on the canvas yet.
 *
 * A study has hundreds of quotes. Scattering all of them across the plane on
 * first open would be a mess nobody would sort, so the canvas starts as whatever
 * you have put on it, and everything else waits in a list you pull from. A quote
 * leaves the list the moment it has a card anywhere -- including parked loose on
 * bare canvas, because a quote you have already pulled out and looked at is one
 * you have dealt with, whether or not a theme claims it.
 *
 * Which is exactly why the list needs narrowing on more than its text. Sorting
 * a pile of three hundred is not one job; it is "everything Priya said about
 * trust", then "the untagged remainder", then "the three long ones I keep
 * putting off". Each of those is a filter, and without them the tray is a scroll
 * bar.
 */

/** What the filter bar is currently asking for. */
function filters() {
  return {
    text: el.traySearch.value.trim().toLowerCase(),
    tag: el.filterTag.value,
    speaker: el.filterSpeaker.value,
    recording: el.filterRecording.value,
    color: el.filterColor.value,
    note: el.filterNote.value,
    sort: el.filterSort.value,
  };
}

function anyFilter(active = filters()) {
  return Boolean(
    active.text || active.tag || active.speaker || active.recording || active.color || active.note
  );
}

/**
 * Whether one quote answers the filter bar.
 *
 * Every dimension is an "and": the reason to have six of them is to arrive at a
 * handful, not at a longer list.
 */
function matches(item, active) {
  // The filter bar was made for quotes; for code cards, "coded" means in use,
  // the note is the code's description, and a recording is one it appears in.
  if (active.tag === "state:any" && !item.count) return false;
  if (active.tag === "state:none" && item.count) return false;
  if (active.tag.startsWith("tag:") && item.kind !== active.tag.slice(4)) return false;
  if (active.recording && !(item.recordings || []).includes(active.recording)) return false;
  if (active.color && item.color !== active.color) return false;
  if (active.note === "yes" && !(item.description || "").trim()) return false;
  if (active.note === "no" && (item.description || "").trim()) return false;
  if (!active.text) return true;
  return (
    (item.name || "").toLowerCase().includes(active.text) ||
    (item.description || "").toLowerCase().includes(active.text)
  );
}

const SORTS = {
  recording: null,
  longest: (a, b) => b.count - a.count,
  shortest: (a, b) => a.count - b.count,
  tags: (a, b) => b.recordings.length - a.recordings.length,
};

function trayQuotes() {
  const active = filters();
  const quotes = ctx.state.codes.filter(
    (item) => !ctx.state.onCanvas.has(item.ref) && matches(item, active)
  );
  const order = SORTS[active.sort];
  return order ? [...quotes].sort(order) : quotes;
}

/**
 * Rebuild a filter's options, keeping whatever was chosen.
 *
 * The corpus changes underneath -- a quote gets a tag in the reader, a recording
 * is added -- and rebuilding on every render would throw away the filter
 * mid-sort. So the options are only replaced when they actually differ, and the
 * selection survives as long as it still exists.
 */
function fillFilter(select, placeholder, options) {
  const signature = JSON.stringify([placeholder, options]);
  if (select.dataset.signature === signature) return;
  const chosen = select.value;
  select.dataset.signature = signature;
  select.innerHTML =
    `<option value="">${escapeHtml(placeholder)}</option>` +
    options
      .map(([value, label]) =>
        value === null
          ? `<option disabled>──────────</option>`
          : `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`
      )
      .join("");
  select.value = [...select.options].some((option) => option.value === chosen) ? chosen : "";
}

function renderFilters() {
  // Counted over the whole corpus rather than the tray, so a number next to a
  // tag means the same thing whatever else is selected.
  fillFilter(el.filterTag, "any code", [
    ["tag:text", "text codes"],
    ["tag:video", "▶ video codes"],
    [null, null],
    ["state:any", "used somewhere"],
    ["state:none", "not used yet"],
  ]);
  fillFilter(el.filterSpeaker, "anyone", []);
  fillFilter(
    el.filterRecording,
    "every recording",
    [...ctx.state.recordings.values()].map((rec) => [rec.id, rec.title])
  );
  fillFilter(
    el.filterColor,
    "any colour",
    [...new Set(ctx.state.codes.map((c) => c.color || "slate"))].sort().map((c) => [c, c])
  );
}

function renderTray() {
  renderFilters();
  const quotes = trayQuotes();
  const active = filters();
  const scroll = el.trayBody.scrollTop;

  el.trayCount.textContent = String(quotes.length);
  el.filterClear.hidden = !anyFilter(active);
  const waiting = ctx.state.codes.filter((c) => !ctx.state.onCanvas.has(c.ref)).length;
  el.traySummary.textContent = anyFilter(active)
    ? `${quotes.length} of ${waiting} waiting`
    : `${waiting} waiting`;

  el.trayBody.innerHTML = quotes.length
    ? quotes
        .map(
          (item) => `
          <article class="tray__item tray__item--code vc--${escapeHtml(item.color || "slate")}"
                   data-ref="${escapeHtml(item.ref)}" tabindex="0"
                   title="Drag onto the canvas, or press enter">
            <p class="tray__text">${item.kind === "video" ? "▶ " : ""}${escapeHtml(item.name)} ${coderTag(item.coder)}</p>
            <p class="tray__meta">
              <span class="tray__who">${item.count} ${noun(item)}${item.count === 1 ? "" : "s"} in ${item.recordings.length} recording${item.recordings.length === 1 ? "" : "s"}</span>
              ${readerLink(item)}
            </p>
            ${item.description ? `<p class="tray__tags">${escapeHtml(item.description)}</p>` : ""}
          </article>`
        )
        .join("")
    : `<p class="empty">${
        anyFilter(active)
          ? "Nothing waiting matches that. Clear the filters to see the rest."
          : "Everything is out on the canvas."
      }</p>`;
  el.trayBody.scrollTop = scroll;
}

/**
 * Mark the cards on the plane that answer the filter too.
 *
 * Filtering a list tells you what is left to do. Filtering a *plane* can tell
 * you something the list cannot: where the quotes you are asking about already
 * ended up. Nothing is hidden and nothing moves -- a filter is a question, and
 * hiding a card would be an answer to a question nobody asked.
 */
function markMatches() {
  const active = filters();
  const on = anyFilter(active);
  el.surface.classList.toggle("canvas__surface--filtering", on);
  for (const node of el.surface.querySelectorAll(".ccard")) {
    const quote = ctx.state.byRef.get(node.dataset.ref);
    node.classList.toggle("ccard--match", on && Boolean(quote) && matches(quote, active));
  }
}

/**
 * What the keyboard is holding, and what can be done to it without a mouse.
 *
 * Dragging is the fast way to arrange a plane and it cannot be the only way. The
 * select here is the same move as a drag between two areas; the arrow keys are
 * the same move as a drag within one.
 */
function showInspector() {
  if (!focused) {
    el.inspector.innerHTML =
      '<p class="empty">Click or tab to a card to move it with the keyboard.</p>';
    return;
  }
  const quote = ctx.state.byRef.get(focused.ref);
  if (!quote) {
    el.inspector.innerHTML = "";
    return;
  }
  el.inspector.innerHTML = `
    <p class="inspector__text">${escapeHtml(quote.text)}</p>
    <label class="inspector__row">
      <span>In</span>
      <select data-act="reparent" aria-label="Which theme this card is in">
        <option value=""${focused.theme_id ? "" : " selected"}>loose on the canvas</option>
        ${ctx.state.themes
          .map(
            (theme) =>
              `<option value="${escapeHtml(theme.id)}"${
                theme.id === focused.theme_id ? " selected" : ""
              }>${escapeHtml(theme.title)}</option>`
          )
          .join("")}
      </select>
    </label>
    <p class="inspector__hint">${
      gridView
        ? "grid view is on · delete puts it away"
        : "arrows move it · shift for fine · delete puts it away"
    }</p>
    <div class="inspector__acts">
      <button class="btn" data-act="duplicate" type="button">Also place in…</button>
      <button class="btn" data-act="remove" type="button">Put away</button>
    </div>`;
}

/* ----------------------------------------------------------- persistence -- */

/**
 * Make a change, and take the whole canvas back as the answer.
 *
 * Structural changes go through here and nowhere else. Moving one card can
 * change two themes' membership, so the reply is the entire state and one place
 * swallows it -- the alternative is a client patching its own copy and quietly
 * disagreeing with the file about where things are.
 */
async function send(path, body, whenBroken) {
  try {
    ctx.adopt(await api(path, { method: "POST", body }));
  } catch (error) {
    ctx.notify(`${whenBroken}: ${error.message}`, { kind: "warn" });
    renderCanvas();
  }
}

/**
 * Positions, written a moment after you stop moving things.
 *
 * Moves accumulate by card so that shoving one card around for ten seconds is
 * still one write, and so a drag that ends up touching several cards -- resizing
 * an area pulls its contents back inside -- lands as a single change to the file
 * rather than a burst the reader could catch halfway through.
 */
const pendingMoves = new Map();
const flushMoves = debounce(async () => {
  const moves = [...pendingMoves.values()];
  pendingMoves.clear();
  if (!moves.length) return;
  try {
    await api("/api/library/canvas/positions", { method: "POST", body: { moves } });
  } catch (error) {
    ctx.notify(`Could not save where things are: ${error.message}`, { kind: "warn" });
  }
}, SAVE_AFTER);

function saveCard(ref, themeId, x, y) {
  const card = ctx.state.cards.find((c) => c.ref === ref && (c.theme_id || null) === (themeId || null));
  if (card) Object.assign(card, { x, y });
  pendingMoves.set(`${themeId || ""}|${ref}`, { ref, theme_id: themeId || null, x, y });
  flushMoves();
}

const pendingShapes = new Map();
const flushShapes = debounce(async () => {
  const shapes = [...pendingShapes.values()];
  pendingShapes.clear();
  for (const shape of shapes) {
    try {
      const { theme } = await api("/api/library/canvas/reshape", { method: "POST", body: shape });
      Object.assign(ctx.state.themes.find((t) => t.id === theme.id) || {}, theme);
    } catch (error) {
      ctx.notify(`Could not save that area: ${error.message}`, { kind: "warn" });
    }
  }
}, SAVE_AFTER);

function saveArea(theme) {
  pendingShapes.set(theme.id, {
    theme_id: theme.id,
    x: theme.x,
    y: theme.y,
    w: theme.w,
    h: theme.h,
  });
  flushShapes();
}

/* ---------------------------------------------------------- dragging it -- */

/**
 * Start a drag of a card, from wherever it currently is.
 *
 * The card being dragged is drawn as a fixed-position ghost in screen space
 * rather than being moved around inside the transformed plane. That is what lets
 * one piece of code drag a quote out of the tray, between two areas, and back
 * into the tray: the ghost does not care which of those it came from, and none of
 * them involve reparenting a node that is holding a pointer capture.
 */
function beginCardDrag(event, { ref, from, grabX, grabY, copy }) {
  const { card_w, card_h } = metrics();
  const quote = ctx.state.byRef.get(ref);
  if (!quote) return;

  const ghost = document.createElement("div");
  ghost.className = `ccard ccard--ghost ccard--code vc--${quote.color || "slate"}`;
  ghost.style.width = `${card_w * view.z}px`;
  ghost.style.height = `${card_h * view.z}px`;
  ghost.innerHTML = `<p class="ccard__text">${escapeHtml(quote.name || quote.text)}</p>`;
  document.body.appendChild(ghost);

  drag = {
    kind: "card",
    ref,
    from,
    copy,
    ghost,
    grabX,
    grabY,
    origin: from === undefined ? null : cardNode(ref, from),
  };
  drag.origin?.classList.add("ccard--lifted");
  moveGhost(event.clientX, event.clientY);
}

function moveGhost(clientX, clientY) {
  drag.ghost.style.left = `${clientX - drag.grabX * view.z}px`;
  drag.ghost.style.top = `${clientY - drag.grabY * view.z}px`;
  const overTray = pointerOver(el.tray, clientX, clientY);
  drag.ghost.classList.toggle("ccard--discard", overTray);

  // Say where it would land before letting go, because on a plane a drop with no
  // preview is a guess -- areas overlap, and the edge of one is not obvious.
  const { card_w, card_h } = metrics();
  const point = toCanvas(clientX - drag.grabX * view.z, clientY - drag.grabY * view.z);
  const target = overTray
    ? null
    : areaAt({ x: point.x + card_w / 2, y: point.y + card_h / 2 });
  for (const node of el.surface.querySelectorAll(".area--target")) {
    node.classList.remove("area--target");
  }
  if (target) {
    el.surface.querySelector(`.area[data-theme="${CSS.escape(target.id)}"]`)
      ?.classList.add("area--target");
  }
}

function pointerOver(node, clientX, clientY) {
  if (!node || node.hidden) return false;
  const rect = node.getBoundingClientRect();
  return (
    clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom
  );
}

async function finishCardDrag(clientX, clientY) {
  const { card_w, card_h } = metrics();
  const { ref, from, copy } = drag;
  const point = toCanvas(clientX - drag.grabX * view.z, clientY - drag.grabY * view.z);
  const overTray = pointerOver(el.tray, clientX, clientY);
  const inViewport = pointerOver(el.viewport, clientX, clientY);

  drag.ghost.remove();
  drag.origin?.classList.remove("ccard--lifted");
  for (const node of el.surface.querySelectorAll(".area--target")) {
    node.classList.remove("area--target");
  }
  const wasNew = from === undefined;
  drag = null;

  // Dropped back on the tray: put this one card away. Other cards for the same
  // quote, in other themes, are none of this card's business.
  if (overTray) {
    if (wasNew) return;
    focused = null;
    await send("/api/library/canvas/unplace", { ref, theme_id: from }, "Could not put that away");
    return;
  }
  if (!inViewport) {
    renderCanvas();
    return;
  }

  const target = areaAt({ x: point.x + card_w / 2, y: point.y + card_h / 2 });
  const theme = target ? ctx.state.themes.find((t) => t.id === target.id) : null;

  // Grid view draws positions rather than reading them, so a drag that stays
  // inside one area has nowhere to put anything: it would land back in its slot
  // and the write would be invisible. Between areas it still means something,
  // and that still happens.
  if (gridView && !wasNew && !copy && (theme?.id || null) === (from || null)) {
    renderCanvas();
    return;
  }

  const body = { ref, theme_id: theme ? theme.id : null };
  if (!theme) {
    Object.assign(body, { x: point.x, y: point.y });
  } else if (!gridView && !theme.collapsed) {
    // Positions are stored against the area's own box, so the clamp is against
    // that -- not against the box grid view or a roll-up happens to be drawing.
    Object.assign(body, clampToArea(theme, point.x - theme.x, point.y - theme.y));
  }
  // Rolled up or in grid view there is no meaningful spot to name, so none is
  // named and the server finds a clear one -- which is where the card turns out
  // to be when the area is opened again.

  // A move names where it came from so the card travels; a copy says nothing, and
  // the server adds a second card for the same quote.
  if (!wasNew && !copy) body.moved_from = from;

  focused = { ref, theme_id: body.theme_id };
  await send("/api/library/canvas/place", body, "Could not move that quote");
}

/* ------------------------------------------------------------- pointers -- */

function bindPointers() {
  el.viewport.addEventListener("pointerdown", onPointerDown);
  el.viewport.addEventListener("pointermove", onPointerMove);
  el.viewport.addEventListener("pointerup", onPointerUp);
  el.viewport.addEventListener("pointercancel", onPointerUp);
  el.tray.addEventListener("pointerdown", onTrayPointerDown);
  el.tray.addEventListener("pointermove", onPointerMove);
  el.tray.addEventListener("pointerup", onPointerUp);
  el.tray.addEventListener("pointercancel", onPointerUp);
}

function onPointerDown(event) {
  if (event.button !== 0 && event.button !== 1) return;
  const card = event.target.closest(".ccard");
  const area = event.target.closest(".area");
  const handle = event.target.closest("[data-handle]")?.dataset.handle;

  // Anything with its own job -- a play button, a title field -- keeps it.
  if (event.target.closest("button, a, input, textarea, select")) return;

  // Everything from here is a drag, so the browser's own idea of what a press
  // means is refused: dragging a card across its own text would otherwise leave
  // a trail of blue selection behind the thing you are moving. Focus is then
  // this function's job, and is given without scrolling -- the plane is moved by
  // its transform and by nothing else.
  event.preventDefault();

  const start = { clientX: event.clientX, clientY: event.clientY };
  el.viewport.setPointerCapture(event.pointerId);

  if (card && event.button === 0) {
    const rect = card.getBoundingClientRect();
    drag = {
      kind: "pending-card",
      start,
      ref: card.dataset.ref,
      from: card.dataset.theme || null,
      grabX: (event.clientX - rect.left) / view.z,
      grabY: (event.clientY - rect.top) / view.z,
      copy: event.altKey,
    };
    card.focus({ preventScroll: true });
    focusCard(card.dataset.ref, card.dataset.theme || null);
    return;
  }

  if (area && event.button === 0) {
    const theme = ctx.state.themes.find((t) => t.id === area.dataset.theme);
    if (theme) {
      // Resizing starts from the box on screen, not the stored one: in grid view
      // those differ, and a grip that jumps when you take hold of it is a grip
      // nobody can aim.
      drag =
        handle === "resize"
          ? { kind: "resize", start, node: area, theme, w: theme.w, h: areaBox(theme).h }
          : { kind: "area", start, node: area, theme, x: theme.x, y: theme.y };
      area.classList.add("area--moving");
      return;
    }
  }

  drag = { kind: "pan", start, x: view.x, y: view.y };
  el.viewport.classList.add("canvas__viewport--panning");
}

function onTrayPointerDown(event) {
  const item = event.target.closest(".tray__item");
  if (!item || event.button !== 0) return;
  // The link out of a tray quote keeps its own job, as on a card.
  if (event.target.closest("a, button")) return;
  event.preventDefault();
  const { card_w, card_h } = metrics();
  item.focus({ preventScroll: true });
  el.tray.setPointerCapture(event.pointerId);
  drag = {
    kind: "pending-card",
    start: { clientX: event.clientX, clientY: event.clientY },
    ref: item.dataset.ref,
    from: undefined,
    // Grab it near its middle: the tray item is a different shape from the card
    // it becomes, so the point you pressed does not mean anything on the plane.
    grabX: card_w / 2,
    grabY: card_h / 2,
    copy: false,
  };
}

function onPointerMove(event) {
  if (!drag) return;

  // A card in flight is the one drag with no origin to measure from: the ghost
  // tracks the pointer in screen space, so it is handled before anything reads
  // the press position.
  if (drag.kind === "card") {
    moveGhost(event.clientX, event.clientY);
    return;
  }

  const dx = event.clientX - drag.start.clientX;
  const dy = event.clientY - drag.start.clientY;

  if (drag.kind === "pending-card") {
    if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
    const { ref, from, grabX, grabY, copy } = drag;
    drag = null;
    beginCardDrag(event, { ref, from, grabX, grabY, copy });
    return;
  }

  if (drag.kind === "pan") {
    view.x = drag.x + dx;
    view.y = drag.y + dy;
    applyView();
    return;
  }

  const { area_min_w, area_min_h } = metrics();
  if (drag.kind === "area") {
    drag.theme.x = drag.x + dx / view.z;
    drag.theme.y = drag.y + dy / view.z;
    drag.node.style.left = `${drag.theme.x}px`;
    drag.node.style.top = `${drag.theme.y}px`;
    return;
  }
  if (drag.kind === "resize") {
    drag.theme.w = Math.max(area_min_w, drag.w + dx / view.z);
    drag.theme.h = Math.max(area_min_h, drag.h + dy / view.z);
    drag.node.style.width = `${drag.theme.w}px`;
    drag.node.style.height = `${drag.theme.h}px`;
  }
}

function onPointerUp(event) {
  if (!drag) return;
  el.viewport.classList.remove("canvas__viewport--panning");
  try {
    event.currentTarget.releasePointerCapture(event.pointerId);
  } catch (_) { /* pointer already gone */ }

  if (drag.kind === "card") {
    finishCardDrag(event.clientX, event.clientY);
    return;
  }
  if (drag.kind === "pending-card" || drag.kind === "pan") {
    drag = null;
    return;
  }

  const { theme, node, kind } = drag;
  drag = null;
  node.classList.remove("area--moving");
  if (kind === "resize") {
    // Pull anything the smaller box no longer covers back inside, so a card
    // cannot end up hidden behind the edge of its own theme.
    for (const card of ctx.state.cards.filter((c) => c.theme_id === theme.id)) {
      const spot = clampToArea(theme, card.x, card.y);
      if (spot.x !== card.x || spot.y !== card.y) {
        Object.assign(card, spot);
        const element = cardNode(card.ref, theme.id);
        if (element) {
          element.style.left = `${card.x}px`;
          element.style.top = `${card.y}px`;
        }
      }
    }
  }
  saveArea(theme);
}

/* --------------------------------------------------------- wheel & keys -- */

function onWheel(event) {
  event.preventDefault();
  // Pinch on a trackpad arrives as ctrl+wheel, which is also how a mouse asks
  // to zoom. Everything else scrolls the plane rather than the page.
  if (event.ctrlKey || event.metaKey) {
    zoomAt(event.clientX, event.clientY, event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
    return;
  }
  view.x -= event.deltaX;
  view.y -= event.deltaY;
  applyView();
}

function focusCard(ref, themeId) {
  for (const node of el.surface.querySelectorAll(".ccard--focus")) {
    node.classList.remove("ccard--focus");
  }
  focused = { ref, theme_id: themeId || null };
  cardNode(ref, focused.theme_id)?.classList.add("ccard--focus");
  showInspector();
}

function onKeyDown(event) {
  if (event.target.closest("input, textarea, select")) return;
  const step = event.shiftKey ? NUDGE_FINE : NUDGE;

  if (event.key === "0" && !event.metaKey && !event.ctrlKey) {
    fit();
    return;
  }
  if (!focused) return;
  const card = ctx.state.cards.find(
    (c) => c.ref === focused.ref && (c.theme_id || null) === focused.theme_id
  );
  if (!card) return;

  const shift = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[
    event.key
  ];
  if (shift && gridView) {
    // Nothing to nudge: grid view is drawing the positions, not reading them.
    event.preventDefault();
    ctx.notify("Grid view packs by speaker and time. Turn it off to arrange by hand.");
    return;
  }
  if (shift) {
    event.preventDefault();
    const theme = focused.theme_id
      ? ctx.state.themes.find((t) => t.id === focused.theme_id)
      : null;
    const next = theme
      ? clampToArea(theme, card.x + shift[0], card.y + shift[1])
      : { x: card.x + shift[0], y: card.y + shift[1] };
    const node = cardNode(focused.ref, focused.theme_id);
    if (node) {
      node.style.left = `${next.x}px`;
      node.style.top = `${next.y}px`;
    }
    saveCard(focused.ref, focused.theme_id, next.x, next.y);
    return;
  }
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    const away = { ...focused };
    focused = null;
    send("/api/library/canvas/unplace", { ref: away.ref, theme_id: away.theme_id },
      "Could not put that away");
  }
}

/* --------------------------------------------------------------- clicks -- */

async function onSurfaceClick(event) {
  const act = event.target.closest("[data-act]")?.dataset.act;
  const card = event.target.closest(".ccard");
  const area = event.target.closest(".area");

  if (act === "play" && card) {
    const item = ctx.state.byRef.get(card.dataset.ref);
    if (item?.applications?.length) ctx.startQueue(item.applications, item.name);
    else if (item) ctx.notify("Nothing carries that code yet.");
    return;
  }
  if (act === "expand" && card) {
    const key = `${card.dataset.ref}|${card.dataset.theme || ""}`;
    if (expanded.has(key)) expanded.delete(key);
    else expanded.add(key);
    renderCanvas();
    return;
  }
  if (!area) return;
  const theme = ctx.state.themes.find((t) => t.id === area.dataset.theme);
  if (!theme) return;

  if (act === "collapse") {
    try {
      const { theme: saved } = await api(`/api/library/themes/${theme.id}`, {
        method: "PATCH",
        body: { collapsed: !theme.collapsed },
      });
      Object.assign(theme, saved);
      renderCanvas();
    } catch (error) {
      ctx.notify(`Could not roll that up: ${error.message}`, { kind: "warn" });
    }
    return;
  }
  if (act === "play-theme") {
    const quotes = theme.refs.flatMap((ref) => ctx.state.byRef.get(ref)?.applications || []);
    if (quotes.length) ctx.startQueue(quotes, theme.title);
    else ctx.notify("Nothing in that theme has a quote or span yet.");
  } else if (act === "promote") {
    await ctx.promoteTheme(theme);
  } else if (act === "tidy") {
    await send("/api/library/canvas/tidy", { theme_id: theme.id }, "Could not tidy that area");
  } else if (act === "delete") {
    await deleteArea(theme);
  }
}

/**
 * Take an area away, and offer to put it back exactly as it was.
 *
 * An area holds a name, a note, and an arrangement -- which quote sits next to
 * which -- and that arrangement is the part that took the time. So the whole box
 * is copied down before it goes, and the undo rebuilds it in one call rather than
 * asking for the sorting again. The quotes themselves were never at risk: they
 * live in the recordings, and without an area to be in they go back to the tray.
 */
async function deleteArea(theme) {
  // Themes are shared between coders; independent mode hides other coders'
  // quotes, and deleting the area would take them out of it unseen.
  if (currentMode() === "independent" && theme.refs.some((ref) => !ctx.state.byRef.has(ref))) {
    ctx.notify("This area holds quotes from other coders that independent mode hides. Switch to Collaborative to delete it.", { kind: "warn" });
    return;
  }
  const snapshot = {
    title: theme.title,
    note: theme.note || "",
    color: theme.color,
    box: { x: theme.x, y: theme.y, w: theme.w, h: theme.h },
    cards: ctx.state.cards
      .filter((card) => card.theme_id === theme.id)
      .map((card) => ({ ref: card.ref, x: card.x, y: card.y })),
  };

  try {
    await api(`/api/library/themes/${theme.id}`, { method: "DELETE" });
  } catch (error) {
    ctx.notify(`Could not delete that area: ${error.message}`, { kind: "warn" });
    return;
  }
  ctx.state.themes = ctx.state.themes.filter((t) => t.id !== theme.id);
  ctx.state.cards = ctx.state.cards.filter((card) => card.theme_id !== theme.id);
  ctx.recount();
  renderCanvas();
  ctx.refreshBoard();

  const count = snapshot.cards.length;
  // Undo recreates a theme as one of your own, so a common theme gets none;
  // putting it back as common would mean promoting it again.
  ctx.notify(
    count
      ? `Deleted “${snapshot.title}”. Its ${count} code${count === 1 ? "" : "s"} are back in the tray.`
      : `Deleted “${snapshot.title}”.`,
    isCommon(theme)
      ? {}
      : {
          action: {
            label: "Undo",
            onAct: () => send("/api/library/themes", snapshot, "Could not put that area back"),
          },
        }
  );
}

const saveField = debounce(async (themeId, patch) => {
  try {
    const { theme } = await api(`/api/library/themes/${themeId}`, { method: "PATCH", body: patch });
    Object.assign(ctx.state.themes.find((t) => t.id === theme.id) || {}, theme);
    ctx.refreshBoard();
  } catch (error) {
    ctx.notify(`Could not save that: ${error.message}`, { kind: "warn" });
  }
}, SAVE_AFTER);

function onSurfaceInput(event) {
  const act = event.target.dataset.act;
  const themeId = event.target.closest(".area")?.dataset.theme;
  if (!themeId || (act !== "rename" && act !== "note")) return;
  if (act === "rename") {
    // A wrapping field will take a newline if pasted one. A title is one line.
    const cleaned = event.target.value.replace(/[\r\n]+/g, " ");
    if (cleaned !== event.target.value) event.target.value = cleaned;
    fitTitles();
    saveField(themeId, { title: cleaned });
    return;
  }
  saveField(themeId, { note: event.target.value });
}

async function onInspectorAction(event) {
  const act = event.target.closest("[data-act]")?.dataset.act;
  if (!focused || !act) return;

  if (act === "remove") {
    const away = { ...focused };
    focused = null;
    await send("/api/library/canvas/unplace", { ref: away.ref, theme_id: away.theme_id },
      "Could not put that away");
  } else if (act === "duplicate") {
    // The keyboard route to what alt-dragging does: the same quote, a second
    // card, somewhere else. It lands loose, next to where it already is, so you
    // can see there are now two and drag the new one where it belongs.
    const card = ctx.state.cards.find(
      (c) => c.ref === focused.ref && (c.theme_id || null) === focused.theme_id
    );
    const theme = focused.theme_id
      ? ctx.state.themes.find((t) => t.id === focused.theme_id)
      : null;
    const { card_w } = metrics();
    const x = (theme ? theme.x + (card?.x || 0) : card?.x || 0) + card_w + 20;
    const y = theme ? theme.y + (card?.y || 0) : card?.y || 0;
    focused = { ref: focused.ref, theme_id: null };
    await send("/api/library/canvas/place", { ref: focused.ref, theme_id: null, x, y },
      "Could not place a second copy");
  }
}

async function onInspectorChange(event) {
  const select = event.target.closest('[data-act="reparent"]');
  if (!select || !focused) return;
  const target = select.value || null;
  if (target === focused.theme_id) return;

  const theme = target ? ctx.state.themes.find((t) => t.id === target) : null;
  const { area_pad, area_head, card_w } = metrics();
  const body = { ref: focused.ref, theme_id: target, moved_from: focused.theme_id };
  // Somewhere sensible: the server finds a free grid slot when nothing is said
  // about position, so nothing is said about position.
  if (theme) Object.assign(body, { x: area_pad, y: area_head });
  else {
    const from = ctx.state.themes.find((t) => t.id === focused.theme_id);
    Object.assign(body, { x: (from?.x || 0) + (from?.w || card_w) + 24, y: from?.y || 0 });
  }
  focused = { ref: focused.ref, theme_id: target };
  await send("/api/library/canvas/place", body, "Could not move that quote");
}

/**
 * Put a quote on the canvas without dragging it there.
 *
 * It lands loose in the middle of the view rather than in a theme, because
 * which theme is the decision being made and it should not be made by
 * whichever area happens to be under the centre of the screen. From there the
 * inspector's select files it.
 */
async function placeFromTray(ref) {
  const { card_w, card_h } = metrics();
  const rect = el.viewport.getBoundingClientRect();
  const middle = toCanvas(rect.left + rect.width / 2, rect.top + rect.height / 2);
  focused = { ref, theme_id: null };
  await send(
    "/api/library/canvas/place",
    { ref, theme_id: null, x: middle.x - card_w / 2, y: middle.y - card_h / 2 },
    "Could not put that on the canvas"
  );
  cardNode(ref, null)?.focus();
}

/**
 * Turn grid view on or off.
 *
 * A view preference, so it stays in this browser rather than in the study's
 * file: whether you are reading the arrangement or making it is about you at
 * this moment, not about the analysis.
 */
function setGridView(on) {
  gridView = on;
  el.grid.setAttribute("aria-pressed", String(on));
  remember(GRID_KEY, on ? "on" : "off");
  renderCanvas();
}

/**
 * Roll every area up, or open every one back up.
 *
 * Whichever there is more of decides: with anything still open this closes the
 * lot, which is the gesture you want after finishing a pass and wanting to see
 * the shape of the whole study at once.
 */
async function rollAll() {
  // Only your own themes: rolling up a common theme would change it for everyone.
  const mine = ctx.state.themes.filter(isMine);
  const collapse = mine.some((theme) => !theme.collapsed);
  const changing = mine.filter((theme) => Boolean(theme.collapsed) !== collapse);
  if (!changing.length) return;
  try {
    await Promise.all(
      changing.map((theme) =>
        api(`/api/library/themes/${theme.id}`, {
          method: "PATCH",
          body: { collapsed: collapse },
        }).then(({ theme: saved }) => Object.assign(theme, saved))
      )
    );
  } catch (error) {
    ctx.notify(`Could not roll those up: ${error.message}`, { kind: "warn" });
  }
  renderCanvas();
}

/* ---------------------------------------------------------------- setup -- */

export function initCanvas(context) {
  ctx = context;
  Object.assign(el, {
    view: $("view-canvas"),
    viewport: $("canvas-viewport"),
    surface: $("canvas-surface"),
    blank: $("canvas-blank"),
    zoomLevel: $("canvas-zoom"),
    progress: $("canvas-progress"),
    addArea: $("canvas-add-area"),
    fit: $("canvas-fit"),
    grid: $("canvas-grid"),
    rollAll: $("canvas-collapse-all"),
    tray: $("tray"),
    trayBody: $("tray-body"),
    traySearch: $("tray-search"),
    trayCount: $("tray-count"),
    traySummary: $("tray-summary"),
    trayToggle: $("tray-toggle"),
    filterTag: $("filter-tag"),
    filterSpeaker: $("filter-speaker"),
    filterRecording: $("filter-recording"),
    filterColor: $("filter-color"),
    filterNote: $("filter-note"),
    filterSort: $("filter-sort"),
    filterClear: $("filter-clear"),
    inspector: $("inspector"),
  });
  bindPointers();

  // Whether the view was remembered has to be read before the first save of it.
  const remembered = recall(VIEW_KEY) !== null;
  restoreView();
  applyView();
  framed = remembered;
  el.tray.hidden = recall(TRAY_KEY, "open") === "shut";
  el.trayToggle.setAttribute("aria-expanded", String(!el.tray.hidden));
  gridView = recall(GRID_KEY, "off") === "on";
  el.grid.setAttribute("aria-pressed", String(gridView));

  el.viewport.addEventListener("wheel", onWheel, { passive: false });
  el.viewport.addEventListener("click", onSurfaceClick);
  el.viewport.addEventListener("input", onSurfaceInput);
  el.viewport.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.dataset.act === "rename") {
      event.preventDefault();
      event.target.blur();
    }
  });
  el.view.addEventListener("keydown", onKeyDown);
  el.surface.addEventListener("focusin", (event) => {
    const card = event.target.closest(".ccard");
    if (card) focusCard(card.dataset.ref, card.dataset.theme || null);
  });
  el.inspector.addEventListener("click", onInspectorAction);
  el.inspector.addEventListener("change", onInspectorChange);

  // Getting a quote out of the tray without a pointer. Without this the
  // keyboard could rearrange the canvas but never add to it, which would make
  // the inspector below a tour of somebody else's sorting.
  el.trayBody.addEventListener("keydown", (event) => {
    const item = event.target.closest(".tray__item");
    if (!item || (event.key !== "Enter" && event.key !== " ")) return;
    // Enter on the link out follows it; enter on the quote places it.
    if (event.target.closest("a, button")) return;
    event.preventDefault();
    placeFromTray(item.dataset.ref);
  });

  const refilter = () => {
    renderTray();
    markMatches();
  };
  el.traySearch.addEventListener("input", debounce(refilter, 120));
  for (const select of [
    el.filterTag, el.filterSpeaker, el.filterRecording,
    el.filterColor, el.filterNote, el.filterSort,
  ]) {
    select.addEventListener("change", refilter);
  }
  el.filterClear.addEventListener("click", () => {
    el.traySearch.value = "";
    for (const select of [
      el.filterTag, el.filterSpeaker, el.filterRecording, el.filterColor, el.filterNote,
    ]) {
      select.value = "";
    }
    refilter();
  });

  el.grid.addEventListener("click", () => setGridView(!gridView));
  el.rollAll.addEventListener("click", rollAll);
  el.trayToggle.addEventListener("click", () => {
    el.tray.hidden = !el.tray.hidden;
    el.trayToggle.setAttribute("aria-expanded", String(!el.tray.hidden));
    remember(TRAY_KEY, el.tray.hidden ? "shut" : "open");
  });

  el.fit.addEventListener("click", fit);
  for (const button of el.view.querySelectorAll("[data-zoom]")) {
    button.addEventListener("click", () => {
      const rect = el.viewport.getBoundingClientRect();
      zoomAt(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
        button.dataset.zoom === "in" ? ZOOM_STEP : 1 / ZOOM_STEP
      );
    });
  }

  // A new area lands in the middle of what you are looking at, not at the
  // origin: the reason to add one is usually the quotes already on screen.
  el.addArea.addEventListener("click", async () => {
    const { area_w, area_h } = metrics();
    const rect = el.viewport.getBoundingClientRect();
    const middle = toCanvas(rect.left + rect.width / 2, rect.top + rect.height / 2);
    try {
      const { theme } = await api("/api/library/themes", {
        method: "POST",
        body: { title: "", box: { x: middle.x - area_w / 2, y: middle.y - area_h / 2 } },
      });
      ctx.state.themes.push(theme);
      renderCanvas();
      ctx.refreshBoard();
      const input = el.surface.querySelector(`.area[data-theme="${CSS.escape(theme.id)}"] .area__title`);
      input?.focus();
      input?.select();
    } catch (error) {
      ctx.notify(`Could not add an area: ${error.message}`, { kind: "warn" });
    }
  });
}

/**
 * Called when the canvas becomes visible.
 *
 * Framing has to happen here rather than at load, because it measures the
 * viewport and a hidden element has no size. Once only: after that the view is
 * where the user left it, and moving it out from under them would be worse than
 * a bad first frame.
 */
export function showCanvas() {
  renderCanvas();
  if (!framed) {
    fit();
    framed = true;
  }
}
