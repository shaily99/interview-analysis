/* Thematic analysis across every recording in the library.
 *
 * Themes hold codes (text and video); their quotes and spans are the evidence.
 * Views, because the work has more than one shape:
 *
 *   Canvas - the themes on a plane. Generative, and the one with no right-hand
 *            edge: columns stop working at the width of the screen, and a plane
 *            does not, so the number of themes is no longer a layout problem.
 *            Where two areas sit relative to each other is itself a claim.
 *   Board  - the same themes as columns. Still the fastest way to sort a pile
 *            when there are few enough themes to see all of them at once.
 *   Matrix - text codes against recordings. Separates a theme many people
 *            raised from one person's preoccupation.
 *   Pairs  - text codes that share quotes. Diagnostic: finds two codes that are really
 *            one code, and codes that always arrive together.
 *
 * Canvas and board are two shapes of one thing, not two groupings: a theme made
 * on either shows up on the other, because membership lives in a single file and
 * a card on the canvas *is* a code's membership of the area holding it.
 *
 * Your themes and common themes are shown in every mode; collaborative mode adds
 * other coders' themes, read-only.
 *
 * What none of them would have on their own is the recording: every quote here
 * can be played where it was said, and a whole theme can be listened to in
 * sequence. Tone is half of what a quote means, and it does not survive being
 * written down.
 */

import { $, api, CODER_KEY, escapeHtml, formatTime, recall } from "./util.js";
import { applyRate, storedRate } from "./player.js";
import { applyStoredTheme, bindThemeToggle, notify } from "./chrome.js";
import { coderTag, currentMode, ensureCoder, isCommon, isMine, mountCoderControls } from "./coder.js";
import { openMoveDialog } from "./commondialog.js";
import { initCanvas, renderCanvas, showCanvas } from "./canvas.js";
import {
  initSemantics,
  clearSelection,
  invalidateSemantics,
  renderGraph,
  renderMap,
  renderSignals,
} from "./semantics.js";

const el = {
  meta: $("meta"),
  notices: $("notices"),
  modes: $("modes"),
  canvas: $("view-canvas"),
  board: $("view-board"),
  boardColumns: $("board-columns"),
  boardFilter: $("board-filter"),
  boardProgress: $("board-progress"),
  addTheme: $("add-theme"),
  mapView: $("view-map"),
  graphView: $("view-graph"),
  signalsView: $("view-signals"),
  matrixView: $("view-matrix"),
  matrix: $("matrix"),
  matrixNote: $("matrix-note"),
  matrixDetail: $("matrix-detail"),
  pairsView: $("view-pairs"),
  pairs: $("pairs"),
  pairsNote: $("pairs-note"),
  queue: $("queue"),
  queueMedia: $("queue-media"),
  queuePlay: $("queue-play"),
  queueNext: $("queue-next"),
  queueClose: $("queue-close"),
  queueLabel: $("queue-label"),
  queueQuote: $("queue-quote"),
  queuePosition: $("queue-position"),
};

const state = {
  quotes: [],
  byRef: new Map(),
  recordings: new Map(),
  tags: [],
  codes: [],
  cooccurrence: [],
  themes: [],
  cards: [],
  //: Quotes that are in a theme -- what the board calls sorted.
  placed: new Set(),
  //: Quotes with a card anywhere, loose ones included -- what the tray hides.
  onCanvas: new Set(),
  metrics: null,
  mode: "canvas",
  queue: [],
  queueIndex: 0,
  queueLabel: "",
};

/* ------------------------------------------------------------- loading -- */

async function load() {
  const [library, quotes, codes, themes] = await Promise.all([
    api(`/api/library?mode=${currentMode()}`),
    api(`/api/library/quotes?mode=${currentMode()}`),
    api(`/api/library/codes?mode=${currentMode()}`),
    api(`/api/library/themes?mode=${currentMode()}`),
  ]);

  // Themes hold codes, so the canvas and board lay out codes; the matrix, pairs,
  // map, graph and signals still read the quotes. One lookup serves both, since
  // code refs ("text:…", "video:…") and quote refs never collide.
  state.quotes = quotes.quotes;
  state.codes = codes.codes;
  state.byRef = new Map([...state.quotes, ...state.codes].map((item) => [item.ref, item]));
  state.recordings = new Map(library.recordings.map((r) => [r.id, r]));
  state.tags = library.tags;
  state.cooccurrence = library.cooccurrence;
  state.metrics = themes.metrics;
  adopt(themes);

  el.meta.textContent = [
    `${library.recordings.length} recordings`,
    `${state.quotes.length} quotes`,
    `${state.codes.length} codes`,
    `${library.tags.length} text codes`,
    library.untagged_count ? `${library.untagged_count} uncoded` : null,
  ].filter(Boolean).join("  ·  ");

  // A link from the library opens one code in the matrix, once: a later reload
  // for a mode switch or Refresh must not pull you back to it.
  const url = new URL(location.href);
  const wanted = url.searchParams.get("tag");
  if (wanted) {
    url.searchParams.delete("tag");
    history.replaceState(null, "", url);
    showMode("matrix");
    renderMatrix(wanted);
  } else {
    renderAll();
  }
}

function renderAll() {
  renderBoard();
  // showCanvas rather than renderCanvas: the canvas is the mode the page opens
  // in, so nothing clicks into it, and framing the work needs doing on the way.
  showCanvas();
  renderMatrix();
  renderPairs();
}

/**
 * Take on a canvas payload as the current truth.
 *
 * Every canvas edit answers with the whole of it, because moving one card can
 * change two themes' membership and a client reassembling that from a narrower
 * reply is a client that can quietly disagree with the file. So there is one
 * place that swallows a reply, and it repaints both shapes of the same data.
 */
function adopt(payload) {
  if (payload.themes) state.themes = payload.themes;
  if (payload.cards) state.cards = payload.cards;
  recount();
  renderBoard();
  renderCanvas();
  if (state.mode === "map") renderMap();
}

/** Re-derive the two "is this quote dealt with" sets from the cards. */
function recount() {
  state.placed = new Set(state.themes.flatMap((theme) => theme.refs));
  state.onCanvas = new Set(state.cards.map((card) => card.ref));
}

/** Whether a theme holds quotes the current mode does not show. Themes are still
 *  shared between coders, so deleting one in independent mode would remove other
 *  coders' quotes from it unseen. */
function hidesOthers(theme) {
  return currentMode() === "independent" && theme.refs.some((ref) => !state.byRef.has(ref));
}

/* ------------------------------------------------- moving themes to common -- */

/**
 * Make one of your themes common, after its codes are.
 *
 * A common theme holds only agreed codes, so a theme still holding codes of your
 * own is not moved: the dialog lists them, each with Move to common, and the
 * theme follows once they are agreed. Otherwise it asks, like moving a code,
 * that the theme is final, for a description, and whether it becomes a new
 * common theme or joins one that exists.
 */
async function promoteTheme(theme) {
  const check = await postTheme(theme.id, { final: false });
  document.getElementById("theme-dialog")?.remove();
  const overlay = document.createElement("div");
  overlay.id = "theme-dialog";
  overlay.className = "login";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  document.body.appendChild(overlay);
  const close = () => overlay.remove();

  if (check.status === 409) {
    overlay.innerHTML = `
      <div class="login__card move">
        <h2 class="login__title">Move “${escapeHtml(theme.title || "Untitled")}” to common</h2>
        <p class="login__lead">A common theme holds only common codes. Move these to common first; the theme can follow once they are.</p>
        <ul class="move__blocking">${check.body.blocking
          .map((b) => `<li><span>${escapeHtml(b.name)}</span> <button class="btn" type="button" data-block="${escapeHtml(b.ref)}">Move to common…</button></li>`)
          .join("")}</ul>
        <div class="login__actions"><button class="btn" type="button" data-cancel>Close</button></div>
      </div>`;
    overlay.querySelector("[data-cancel]").addEventListener("click", close);
    overlay.addEventListener("click", async (event) => {
      const ref = event.target.closest("[data-block]")?.dataset.block;
      if (!ref) return;
      const item = state.byRef.get(ref);
      if (!item) return;
      const common = state.codes.filter((c) => isCommon(c) && c.kind === item.kind).map((c) => ({ ...c, name: c.name }));
      const done = await openMoveDialog({ kind: item.kind, code: item, common });
      if (done) {
        close();
        await load();
        const again = state.themes.find((t) => t.id === theme.id);
        if (again) promoteTheme(again);
      }
    });
    return;
  }

  const commonThemes = state.themes.filter(isCommon);
  overlay.innerHTML = `
    <form class="login__card move" novalidate>
      <h2 class="login__title">Move “${escapeHtml(theme.title || "Untitled")}” to common</h2>
      <fieldset class="move__as"><legend>Move it as</legend>
        <label><input type="radio" name="as" value="new" checked> A new common theme</label>
        <label><input type="radio" name="as" value="merge" ${commonThemes.length ? "" : "disabled"}> Merged into
          <select id="theme-into" ${commonThemes.length ? "" : "disabled"}>${commonThemes
            .map((t) => `<option value="${t.id}">✓ ${escapeHtml(t.title || "Untitled")}</option>`)
            .join("")}</select></label>
      </fieldset>
      <label class="move__check"><input type="checkbox" id="theme-final"> This theme has been discussed and is final.</label>
      <label class="move__desc">Description<textarea id="theme-description" rows="3">${escapeHtml(theme.note || "")}</textarea>
        <span class="login__hint">${theme.note ? "Check your description still holds before moving." : "Say what this theme is, for everyone reading it."}</span></label>
      <p class="login__error" id="theme-error" role="alert" hidden></p>
      <div class="login__actions"><button class="btn" type="button" data-cancel>Cancel</button>
        <button class="btn btn--primary" type="submit" disabled>Move to common</button></div>
    </form>`;
  const form = overlay.querySelector("form");
  form.addEventListener("keydown", (event) => event.stopPropagation());
  overlay.querySelector("[data-cancel]").addEventListener("click", close);
  overlay.querySelector("#theme-final").addEventListener("change", (event) => {
    form.querySelector('[type="submit"]').disabled = !event.target.checked;
  });
  // Merging shows the common theme's description, since that is the one kept;
  // it is sent only if edited, so an untouched one is left as it is.
  const description = overlay.querySelector("#theme-description");
  let edited = false;
  description.addEventListener("input", () => (edited = true));
  const showDescription = () => {
    if (edited) return;
    const merge = form.elements.as.value === "merge";
    const target = commonThemes.find((t) => t.id === overlay.querySelector("#theme-into").value);
    description.value = merge ? target?.note || "" : theme.note || "";
  };
  form.querySelectorAll('[name="as"]').forEach((radio) => radio.addEventListener("change", showDescription));
  overlay.querySelector("#theme-into").addEventListener("change", showDescription);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const merge = form.elements.as.value === "merge";
    const reply = await postTheme(theme.id, {
      final: true,
      ...(edited ? { description: description.value } : {}),
      ...(merge ? { into: overlay.querySelector("#theme-into").value } : {}),
    });
    if (reply.status !== 200) {
      const error = overlay.querySelector("#theme-error");
      error.textContent = reply.body.detail || "Could not move that theme.";
      error.hidden = false;
      return;
    }
    close();
    adopt(reply.body);
    window.dispatchEvent(new CustomEvent("commonchange"));
    notify(el.notices, `Moved “${theme.title || "Untitled"}” to common.`);
  });
}

/** The theme move itself; a theme still holding independent codes comes back as a 409. */
async function postTheme(themeId, body) {
  const response = await fetch(`/api/library/themes/${themeId}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Coder": recall(CODER_KEY) || "", "X-Mode": currentMode() },
    body: JSON.stringify(body),
  });
  return { status: response.status, body: await response.json().catch(() => ({})) };
}

/* --------------------------------------------------------------- modes -- */

function showMode(mode) {
  state.mode = mode;
  el.canvas.hidden = mode !== "canvas";
  el.board.hidden = mode !== "board";
  el.matrixView.hidden = mode !== "matrix";
  el.pairsView.hidden = mode !== "pairs";
  el.mapView.hidden = mode !== "map";
  el.graphView.hidden = mode !== "graph";
  el.signalsView.hidden = mode !== "signals";
  for (const button of el.modes.querySelectorAll("[data-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }

  // These three cost real work -- encoding, a layout simulation -- so they run
  // when asked for rather than on load, and only need measuring once visible.
  if (mode === "canvas") showCanvas();
  if (mode === "map") renderMap();
  if (mode === "graph") renderGraph();
  if (mode === "signals") renderSignals();
}

el.modes.addEventListener("click", (event) => {
  const button = event.target.closest("[data-mode]");
  if (button) showMode(button.dataset.mode);
});

/* ------------------------------------------------------ quote rendering -- */

/**
 * A quote as a board card.
 *
 * ``column`` is the theme this particular card is sitting in, which is not the
 * same question as which theme the quote is in: since the canvas can pin one
 * quote inside two areas, the same quote appears in both of those columns. So
 * the select says where *this* card is and moving it moves only this one --
 * anything else would make touching one column silently empty another.
 */
function quoteCard(quote, { draggable = true, column = null } = {}) {
  const recording = state.recordings.get(quote.recording_id);
  const tags = (quote.tags || [])
    .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
    .join("");

  // Dragging is the fast way to sort, but it cannot be the only way: a board
  // you can only use with a mouse is a board some people cannot use at all.
  const elsewhere = state.themes.filter(
    (theme) => theme.id !== column && theme.refs.includes(quote.ref)
  );
  const mover = draggable
    ? `<select class="qcard__move" data-act="move" aria-label="Move this quote to a theme">
         <option value=""${column ? "" : " selected"}>Unsorted</option>
         ${state.themes
           .map(
             (theme) =>
               `<option value="${theme.id}"${theme.id === column ? " selected" : ""}>${escapeHtml(theme.title)}</option>`
           )
           .join("")}
       </select>`
    : "";

  return `
    <article class="qcard qcard--${quote.color || "amber"}" data-ref="${quote.ref}"
             data-column="${column || ""}"
             ${draggable ? 'draggable="true"' : ""}>
      <p class="qcard__text">${escapeHtml(quote.text)}</p>
      <div class="qcard__meta">
        <span class="qcard__where">${escapeHtml(recording ? recording.title : quote.recording_id)}</span>
        <time>${formatTime(quote.start_time)}</time>
        <span class="qcard__tools">
          <button class="icon-btn" data-act="play" title="Play this quote">▶</button>
          <a class="icon-btn" target="_blank" rel="noopener"
             href="/reader?recording=${encodeURIComponent(quote.recording_id)}&t=${quote.start_time}"
             title="Open in the transcript, in a new tab">↗</a>
        </span>
      </div>
      ${quote.note ? `<p class="qcard__note">${escapeHtml(quote.note)}</p>` : ""}
      ${
        elsewhere.length
          ? `<p class="qcard__also">also in ${elsewhere
              .map((theme) => escapeHtml(theme.title))
              .join(", ")}</p>`
          : ""
      }
      <div class="tags">${tags}</div>
      ${mover}
    </article>`;
}

/**
 * Move one card from the column it is in to another, or out of the board.
 *
 * The same operation the canvas performs, and deliberately the same call: the
 * board is a second shape for the canvas, not a second store. Dropping into a
 * theme says nothing about position, and the server finds a free slot in that
 * area -- so a quote sorted here is somewhere sensible when you next open the
 * plane, rather than stacked on top of whatever is already at the origin.
 */
async function moveCard(ref, from, to) {
  const path = to
    ? "/api/library/canvas/place"
    : "/api/library/canvas/unplace";
  const body = to ? { ref, theme_id: to, moved_from: from } : { ref, theme_id: from };
  try {
    adopt(await api(path, { method: "POST", body }));
  } catch (error) {
    notify(el.notices, `Could not move that quote: ${error.message}`, { kind: "warn" });
  }
}

/* ------------------------------------------------------------ the board -- */

function boardQuotes() {
  const filter = el.boardFilter.value;
  return state.codes.filter((item) => {
    if (filter === "unplaced") return !state.placed.has(item.ref);
    if (filter === "tagged") return item.count > 0;
    if (filter === "untagged") return item.count === 0;
    return true;
  });
}

/** A code as a board card: what it is, whose, and how widely it is used. */
function codeCard(item, { column = null } = {}) {
  const movable = (theme) => theme.id !== column && (isMine(theme) || isCommon(theme));
  const mover = `<select class="qcard__move" data-act="move" aria-label="Move this code to a theme">
       <option value=""${column ? "" : " selected"}>Unsorted</option>
       ${state.themes
         .filter((theme) => theme.id === column || movable(theme))
         .map((theme) => `<option value="${theme.id}"${theme.id === column ? " selected" : ""}>${escapeHtml(theme.title || "Untitled")}</option>`)
         .join("")}
     </select>`;
  const noun = item.kind === "video" ? "span" : "quote";
  return `
    <article class="qcard qcard--code vc--${escapeHtml(item.color || "slate")}" data-ref="${item.ref}"
             data-column="${column || ""}" draggable="true">
      <p class="qcard__text">${item.kind === "video" ? "▶ " : ""}${escapeHtml(item.name)} ${coderTag(item.coder)}</p>
      <div class="qcard__meta">
        <span class="qcard__where">${item.count} ${noun}${item.count === 1 ? "" : "s"} · ${item.recordings.length} rec</span>
        <span class="qcard__tools">
          <button class="icon-btn" data-act="play" title="Play every ${noun} with this code">▷</button>
          <a class="icon-btn" target="_blank" rel="noopener" href="/codebook?kind=${item.kind}&code=${encodeURIComponent(item.id)}"
             title="Open this code's page, in a new tab">↗</a>
        </span>
      </div>
      ${item.description ? `<p class="qcard__note">${escapeHtml(item.description)}</p>` : ""}
      ${mover}
    </article>`;
}

function renderBoard() {
  const visible = boardQuotes();
  const shown = new Set(visible.map((quote) => quote.ref));

  // Built from the themes outwards rather than from the quotes, because a quote
  // pinned in two areas on the canvas is genuinely in two columns here, and
  // asking each quote for "its" theme could only ever return one of them.
  const inTheme = new Map(
    state.themes.map((theme) => [
      theme.id,
      theme.refs.filter((ref) => shown.has(ref)).map((ref) => state.byRef.get(ref)).filter(Boolean),
    ])
  );
  const unsorted = visible.filter((quote) => !state.placed.has(quote.ref));

  el.boardProgress.textContent = state.codes.length
    ? `${state.placed.size} of ${state.codes.length} codes placed`
    : "no codes yet";

  const columns = [
    `<section class="column column--unsorted" data-theme-id="">
       <header class="column__head">
         <h2 class="column__title">Unsorted</h2>
         <span class="column__count">${unsorted.length}</span>
       </header>
       <div class="column__body" data-drop="">
         ${unsorted.map((q) => codeCard(q)).join("") ||
           '<p class="empty">Nothing left here.</p>'}
       </div>
     </section>`,
  ];

  for (const theme of state.themes) {
    const quotes = inTheme.get(theme.id) || [];
    const recordings = new Set(quotes.flatMap((q) => q.recordings || []));
    // Other coders' themes are shown in collaborative mode, and are read-only.
    const readOnly = !isMine(theme) && !isCommon(theme);
    columns.push(`
      <section class="column${isCommon(theme) ? " column--common" : ""}" data-theme-id="${theme.id}">
        <header class="column__head">
          <input class="column__title-input" value="${escapeHtml(theme.title)}"
                 data-act="rename" aria-label="Theme name" ${readOnly ? "readonly" : ""}>
          ${isMine(theme) ? "" : coderTag(theme.coder)}
          <span class="column__count">${quotes.length}</span>
          <button class="icon-btn" data-act="play-theme" title="Play every quote and span in this theme">▷</button>
          ${isMine(theme) ? '<button class="icon-btn" data-act="promote-theme" title="Move this theme to common…">✓</button>' : ""}
          ${readOnly ? "" : '<button class="icon-btn" data-act="delete-theme" title="Delete theme">✕</button>'}
        </header>
        <p class="column__spread">${recordings.size} of ${state.recordings.size} recordings</p>
        <textarea class="column__note" rows="1" placeholder="What is this theme?"
                  data-act="note" ${readOnly ? "readonly" : ""}>${escapeHtml(theme.note || "")}</textarea>
        <div class="column__body" data-drop="${readOnly ? "none" : theme.id}">
          ${quotes.map((q) => codeCard(q, { column: theme.id })).join("") ||
            '<p class="empty">Drag codes here.</p>'}
        </div>
      </section>`);
  }

  el.boardColumns.innerHTML = columns.join("");
}

el.boardFilter.addEventListener("change", renderBoard);

el.addTheme.addEventListener("click", async () => {
  try {
    const { theme } = await api("/api/library/themes", { method: "POST", body: { title: "" } });
    state.themes.push(theme);
    renderBoard();
    renderCanvas();
    const input = el.boardColumns.querySelector(`[data-theme-id="${theme.id}"] .column__title-input`);
    input?.focus();
    input?.select();
  } catch (error) {
    notify(el.notices, `Could not add a theme: ${error.message}`, { kind: "warn" });
  }
});

// Drag and drop between columns. What is dragged is one card, so the column it
// came out of travels with it -- a quote in two columns must lose only the one
// you actually picked up.
let dragging = null;

el.boardColumns.addEventListener("dragstart", (event) => {
  const card = event.target.closest(".qcard");
  if (!card) return;
  dragging = { ref: card.dataset.ref, from: card.dataset.column || null };
  card.classList.add("qcard--dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", dragging.ref);
});

el.boardColumns.addEventListener("dragend", (event) => {
  event.target.closest(".qcard")?.classList.remove("qcard--dragging");
  el.boardColumns.querySelectorAll(".column__body--over")
    .forEach((node) => node.classList.remove("column__body--over"));
  dragging = null;
});

el.boardColumns.addEventListener("dragover", (event) => {
  const body = event.target.closest("[data-drop]");
  if (!body) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  body.classList.add("column__body--over");
});

el.boardColumns.addEventListener("dragleave", (event) => {
  event.target.closest("[data-drop]")?.classList.remove("column__body--over");
});

el.boardColumns.addEventListener("drop", async (event) => {
  const body = event.target.closest("[data-drop]");
  if (!body) return;
  event.preventDefault();
  body.classList.remove("column__body--over");

  const card = dragging || { ref: event.dataTransfer.getData("text/plain"), from: null };
  if (!card.ref) return;
  if (body.dataset.drop === "none") return; // another coder's theme is read-only
  const to = body.dataset.drop || null;
  if (to !== card.from) await moveCard(card.ref, card.from, to);
});

// The same move, without a mouse.
el.boardColumns.addEventListener("change", (event) => {
  const select = event.target.closest('[data-act="move"]');
  const card = event.target.closest(".qcard");
  if (!select || !card) return;
  const to = select.value || null;
  const from = card.dataset.column || null;
  if (to !== from) moveCard(card.dataset.ref, from, to);
});

el.boardColumns.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-act]")?.dataset.act;
  const column = event.target.closest("[data-theme-id]");
  const card = event.target.closest(".qcard");

  if (action === "play" && card) {
    const item = state.byRef.get(card.dataset.ref);
    if (item?.applications?.length) startQueue(item.applications, item.name);
    else if (item?.text && item.start_time != null) startQueue([item], item.text.slice(0, 40));
    else notify(el.notices, "Nothing carries that code yet.");
    return;
  }
  if (!column || !column.dataset.themeId) return;
  const theme = state.themes.find((t) => t.id === column.dataset.themeId);
  if (!theme) return;

  if (action === "play-theme") {
    const quotes = theme.refs.flatMap((ref) => state.byRef.get(ref)?.applications || []);
    if (quotes.length) startQueue(quotes, theme.title);
    else notify(el.notices, "Nothing in that theme has a quote or span yet.");
  } else if (action === "promote-theme") {
    await promoteTheme(theme);
  } else if (action === "delete-theme") {
    if (hidesOthers(theme)) {
      notify(el.notices, "This theme holds quotes from other coders that independent mode hides. Switch to Collaborative to delete it.", { kind: "warn" });
      return;
    }
    try {
      await api(`/api/library/themes/${theme.id}`, { method: "DELETE" });
      state.themes = state.themes.filter((t) => t.id !== theme.id);
      state.cards = state.cards.filter((card) => card.theme_id !== theme.id);
      recount();
      renderBoard();
      renderCanvas();
    } catch (error) {
      notify(el.notices, `Could not delete that theme: ${error.message}`, { kind: "warn" });
    }
  }
});

el.boardColumns.addEventListener("change", async (event) => {
  const field = event.target.closest("[data-act]");
  const column = event.target.closest("[data-theme-id]");
  // The move select also lives inside a theme column and also fires change.
  if (!field || !column?.dataset.themeId) return;
  if (field.dataset.act !== "rename" && field.dataset.act !== "note") return;
  const patch = field.dataset.act === "rename" ? { title: field.value } : { note: field.value };
  try {
    const { theme } = await api(`/api/library/themes/${column.dataset.themeId}`, {
      method: "PATCH",
      body: patch,
    });
    Object.assign(state.themes.find((t) => t.id === theme.id) || {}, theme);
    renderCanvas();
  } catch (error) {
    notify(el.notices, `Could not rename that theme: ${error.message}`, { kind: "warn" });
  }
});

/* ----------------------------------------------------------- the matrix -- */

function renderMatrix(focusTag = null) {
  const recordings = [...state.recordings.values()];
  if (!state.tags.length) {
    el.matrix.innerHTML = "";
    el.matrixNote.textContent = "";
    el.matrixDetail.innerHTML =
      '<p class="empty">No text codes yet. Code some quotes in the reader and they will show up here.</p>';
    return;
  }

  el.matrixNote.textContent =
    "sorted by how many recordings share the text code — the top rows are the findings";

  const head = `<thead><tr><th class="matrix__corner">text code</th>${recordings
    .map((r) => `<th class="matrix__rec"><span>${escapeHtml(r.title)}</span></th>`)
    .join("")}<th class="matrix__total">total</th></tr></thead>`;

  const rows = state.tags
    .map((entry) => {
      const cells = recordings
        .map((recording) => {
          const count = entry.recordings[recording.id] || 0;
          // Weight is what turns a table of numbers into a shape you can read
          // down a column: a sparse row is one person, a solid row is a finding.
          const weight = count ? Math.min(1, 0.25 + count / 6) : 0;
          return `<td class="matrix__cell${count ? " matrix__cell--on" : ""}"
                      data-tag="${escapeHtml(entry.tag)}" data-recording="${recording.id}"
                      style="--weight:${weight.toFixed(2)}">${count || ""}</td>`;
        })
        .join("");
      return `<tr${focusTag === entry.tag ? ' class="matrix__row--focus"' : ""}>
        <th class="matrix__tag" data-tag="${escapeHtml(entry.tag)}">
          <span class="dot dot--${entry.color || "amber"}"></span>${escapeHtml(entry.tag)}
          <span class="matrix__spread">${entry.recording_count}/${recordings.length}</span>
        </th>${cells}
        <td class="matrix__total">${entry.quote_count}</td></tr>`;
    })
    .join("");

  el.matrix.innerHTML = head + `<tbody>${rows}</tbody>`;
  if (focusTag) showMatrixDetail(focusTag, null);
}

function showMatrixDetail(tag, recordingId) {
  const quotes = state.quotes.filter(
    (quote) =>
      (quote.tags || []).includes(tag) && (!recordingId || quote.recording_id === recordingId)
  );
  const where = recordingId ? state.recordings.get(recordingId)?.title : "every recording";
  el.matrixDetail.innerHTML = `
    <header class="sheet__head">
      <h2 class="sheet__title">${escapeHtml(tag)} · ${escapeHtml(where || "")}</h2>
      <span class="sheet__count">${quotes.length}</span>
      <button class="btn" id="play-tag" type="button">Play all ${quotes.length}</button>
    </header>
    <div class="qgrid">${quotes.map((q) => quoteCard(q, { draggable: false })).join("")}</div>`;

  $("play-tag")?.addEventListener("click", () => {
    if (quotes.length) startQueue(quotes, `${tag} · ${where}`);
  });
}

el.matrix.addEventListener("click", (event) => {
  const cell = event.target.closest("[data-tag]");
  if (!cell) return;
  showMatrixDetail(cell.dataset.tag, cell.dataset.recording || null);
});

el.matrixDetail.addEventListener("click", (event) => {
  const card = event.target.closest(".qcard");
  if (card && event.target.closest('[data-act="play"]')) {
    const quote = state.byRef.get(card.dataset.ref);
    if (quote) startQueue([quote], quote.text.slice(0, 40));
  }
});

/* ------------------------------------------------------------ the pairs -- */

function renderPairs() {
  if (!state.cooccurrence.length) {
    el.pairs.innerHTML =
      '<p class="empty">No two text codes share a quote yet. This view fills in once quotes carry more than one text code.</p>';
    el.pairsNote.textContent = "";
    return;
  }

  el.pairsNote.textContent = "two codes that always arrive together are often one code";
  const strongest = state.cooccurrence[0].count;

  el.pairs.innerHTML = state.cooccurrence
    .map((pair) => {
      const share = Math.max(0.08, pair.count / strongest);
      return `
        <button class="pair" type="button" data-a="${escapeHtml(pair.a)}" data-b="${escapeHtml(pair.b)}">
          <span class="pair__names">
            <span class="tag">${escapeHtml(pair.a)}</span>
            <span class="pair__link" style="--share:${share.toFixed(2)}"></span>
            <span class="tag">${escapeHtml(pair.b)}</span>
          </span>
          <span class="pair__counts">${pair.count} quote${pair.count === 1 ? "" : "s"}
            · ${pair.recording_count} recording${pair.recording_count === 1 ? "" : "s"}</span>
        </button>
        <div class="pair__quotes" hidden></div>`;
    })
    .join("");
}

el.pairs.addEventListener("click", (event) => {
  const button = event.target.closest(".pair");
  if (!button) return;
  const panel = button.nextElementSibling;
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  const { a, b } = button.dataset;
  const quotes = state.quotes.filter(
    (quote) => (quote.tags || []).includes(a) && (quote.tags || []).includes(b)
  );
  panel.innerHTML = `<div class="qgrid">${quotes.map((q) => quoteCard(q, { draggable: false })).join("")}</div>`;
  panel.hidden = false;
});

/* --------------------------------------------------- listening in a row -- */

/** Which media file holds a session time, and where inside it. */
function locate(quote) {
  const recording = state.recordings.get(quote.recording_id);
  const parts = recording?.parts || [];
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    if (quote.start_time >= parts[i].offset && parts[i].media_name) {
      return { index: i, local: quote.start_time - parts[i].offset };
    }
  }
  return parts.length && parts[0].media_name ? { index: 0, local: quote.start_time } : null;
}

function startQueue(quotes, label) {
  state.queue = quotes;
  state.queueIndex = 0;
  state.queueLabel = label || "";
  el.queue.hidden = false;
  playCurrent();
}

async function playCurrent() {
  const quote = state.queue[state.queueIndex];
  if (!quote) {
    stopQueue();
    return;
  }

  el.queueLabel.textContent =
    `${state.queueLabel} — ${state.recordings.get(quote.recording_id)?.title || ""}`;
  el.queueQuote.textContent = quote.text;
  el.queuePosition.textContent = `${state.queueIndex + 1}/${state.queue.length}`;

  const spot = locate(quote);
  if (!spot) {
    notify(el.notices, "That recording has no media to play.", { kind: "warn" });
    return;
  }

  const url = `/api/recordings/${quote.recording_id}/parts/${spot.index}/media`;
  if (el.queueMedia.dataset.src !== url) {
    el.queueMedia.dataset.src = url;
    el.queueMedia.src = url;
    await new Promise((resolve) => {
      el.queueMedia.addEventListener("loadedmetadata", resolve, { once: true });
      el.queueMedia.addEventListener("error", resolve, { once: true });
    });
  }
  // A little before the first word, as in the reader, at the speed set there.
  applyRate(el.queueMedia, storedRate());
  el.queueMedia.currentTime = Math.max(0, spot.local - 0.75);
  el.queueMedia.play().catch(() => {});
}

// Each quote stops at its own end rather than running into whatever follows.
el.queueMedia.addEventListener("timeupdate", () => {
  const quote = state.queue[state.queueIndex];
  if (!quote) return;
  const spot = locate(quote);
  if (!spot) return;
  const stopAt = spot.local + (quote.end_time - quote.start_time) + 0.4;
  if (el.queueMedia.currentTime >= stopAt) advance();
});

function advance() {
  if (state.queueIndex + 1 < state.queue.length) {
    state.queueIndex += 1;
    playCurrent();
  } else {
    el.queueMedia.pause();
    el.queuePosition.textContent = "done";
  }
}

function stopQueue() {
  el.queueMedia.pause();
  el.queue.hidden = true;
  state.queue = [];
}

el.queueNext.addEventListener("click", advance);
el.queueClose.addEventListener("click", stopQueue);
el.queuePlay.addEventListener("click", () => {
  if (el.queueMedia.paused) el.queueMedia.play().catch(() => {});
  else el.queueMedia.pause();
});
el.queueMedia.addEventListener("play", () => (el.queuePlay.textContent = "❚❚"));
el.queueMedia.addEventListener("pause", () => (el.queuePlay.textContent = "▶"));

/* ---------------------------------------------------------------- start -- */

const shared = {
  state,
  startQueue,
  notify: (message, options) => notify(el.notices, message, options),
  refreshBoard: renderBoard,
  adopt,
  recount,
  promoteTheme,
};

initCanvas(shared);
initSemantics({ ...shared, quoteCard });

applyStoredTheme();
bindThemeToggle($("theme-toggle"));
const loadThemes = () =>
  load().catch((error) => notify(el.notices, `Could not load the library: ${error.message}`, { kind: "warn" }));
// Quotes and codes follow the mode switch; themes themselves stay shared until
// they are rebuilt from codes.
const reloadForMode = async () => {
  // A lasso made in the other mode may hold quotes this mode hides; making a
  // theme from it would move them without anyone seeing.
  clearSelection();
  invalidateSemantics();
  await loadThemes();
  // The analysis views draw only when shown, so the one on screen is redrawn.
  if (["map", "graph", "signals"].includes(state.mode)) showMode(state.mode);
};
ensureCoder()
  .then(() => {
    mountCoderControls($("theme-toggle").parentElement, { onRefresh: reloadForMode });
    window.addEventListener("modechange", reloadForMode);
    window.addEventListener("coderchange", reloadForMode);
    loadThemes();
  })
  .catch((error) => notify(el.notices, `Could not load the coders: ${error.message}`, { kind: "warn" }));
