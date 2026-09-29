/* Saved quotes: the selection bar, the quote cards in the Text codes pane, and
 * their edits.
 *
 * Cards sit level with their transcript block and scroll with it. Every change
 * writes to disk immediately, so there is no save step.
 */

import { api, escapeHtml, formatTime } from "./util.js";
import {
  applyHighlights,
  flashCue,
  scrollToChunk,
  selectionAnchors,
  setCursor,
} from "./transcript.js";
import { seekAndPlay } from "./player.js";
import { mountTagFields } from "./tagfield.js";
import { coderTag, initialsOf, isCommon, isMine, visibleNow } from "./coder.js";
import { openMoveDialog, returnToCoders } from "./commondialog.js";
import { renderStrip } from "./codestrip.js";

export function initHighlights(ctx) {
  renderSwatches(ctx);
  bindQuoteBar(ctx);
  bindList(ctx);
  bindLevelling(ctx);
  renderList(ctx);
}

/* --------------------------------------------------- cards beside the text -- */

/* Each quote card sits level with the transcript block its quote starts in, and
 * the two panes scroll together, so the codes on a passage are read beside it.
 * Cards that would overlap are pushed down, never reordered. With the transcript
 * collapsed there is nothing to line up with, and the cards fall back to a list. */
const CARD_GAP = 6;

function levelQuotes(ctx) {
  const list = ctx.el.highlightList;
  const reader = ctx.el.reader;
  const cards = [...list.querySelectorAll(".quote")];
  const canLevel = cards.length && reader.offsetParent && list.offsetParent && ctx.chunkEls?.length;
  list.classList.toggle("quotes-level--on", Boolean(canLevel));
  if (!canLevel) return;

  const readerTop = reader.getBoundingClientRect().top;
  const listTop = list.getBoundingClientRect().top;
  const shift = readerTop - listTop;
  let floor = 0;
  for (const card of cards) {
    const highlight = ctx.highlights.find((h) => h.id === card.dataset.id);
    const cue = highlight && ctx.el.chunks.querySelector(`[data-cue-id="${highlight.start_cue_id}"]`);
    const block = cue?.closest(".chunk");
    const want = block ? block.getBoundingClientRect().top - readerTop + reader.scrollTop + shift : floor;
    const top = Math.max(want, floor);
    card.style.top = `${top}px`;
    floor = top + card.offsetHeight + CARD_GAP;
  }
  list.style.setProperty("--level-height", `${Math.max(floor, reader.scrollHeight + shift)}px`);
  list.scrollTop = reader.scrollTop;
}

function bindLevelling(ctx) {
  const list = ctx.el.highlightList;
  const reader = ctx.el.reader;
  // The pane you are using leads and the other follows it. Deciding by intent,
  // not by whichever scrolled last, keeps a smooth scroll of one from being
  // snapped back by the echo of the other.
  let leader = "reader";
  ctx.leadScroll = (who) => (leader = who);
  for (const type of ["wheel", "pointerdown", "touchstart", "keydown"]) {
    list.addEventListener(type, () => (leader = "list"), { passive: true });
    reader.addEventListener(type, () => (leader = "reader"), { passive: true });
  }
  reader.addEventListener("scroll", () => {
    if (leader !== "reader" || !list.classList.contains("quotes-level--on")) return;
    list.scrollTop = reader.scrollTop;
  });
  list.addEventListener("scroll", () => {
    if (leader !== "list" || !list.classList.contains("quotes-level--on")) return;
    // Instant: the reader scrolls smoothly by default, which would lag behind.
    reader.scrollTo({ top: list.scrollTop, behavior: "instant" });
  });
  ctx.onGeometry = () => levelQuotes(ctx);
  let pending;
  list.addEventListener("input", () => {
    clearTimeout(pending);
    pending = setTimeout(() => levelQuotes(ctx), 150);
  });
}

/* --------------------------------------------------------- selection bar -- */

function renderSwatches(ctx) {
  ctx.el.quotebarColors.innerHTML = ctx.colors
    .map(
      (color) =>
        `<button class="swatch swatch--${color}" type="button" data-color="${color}" title="Save as ${color}" aria-label="Save as ${color}"></button>`
    )
    .join("");
}

function bindQuoteBar(ctx) {
  const bar = ctx.el.quotebar;

  document.addEventListener("selectionchange", () => {
    // Let the selection settle before measuring it.
    clearTimeout(ctx.selectionTimer);
    ctx.selectionTimer = setTimeout(() => refreshQuoteBar(ctx), 120);
  });

  bar.addEventListener("mousedown", (event) => event.preventDefault());

  ctx.el.quotebarColors.addEventListener("click", (event) => {
    const swatch = event.target.closest(".swatch");
    if (swatch) save(ctx, { color: swatch.dataset.color });
  });

  ctx.el.quotebarNote.addEventListener("click", () => save(ctx, { focusNote: true }));
  ctx.el.quotebarCopy.addEventListener("click", () => copySelection(ctx));

  ctx.el.quotebarHand.addEventListener("click", (event) => {
    const who = event.target.closest("[data-hand-to]");
    if (who) handOverSelection(ctx, who.dataset.handTo);
  });
}

/** The speakers a selection can be handed to: the roster, then whoever else the
 *  transcript names. Nothing to offer means no roster and no labels yet. */
function handCandidates(ctx) {
  const names = (ctx.data?.transcript?.roster || []).map((entry) => entry.name);
  for (const name of ctx.data?.transcript?.speakers || []) {
    if (!names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Offer to hand the selected words to someone else.
 *
 * Reading along, you see that half of what Zoom filed under one person was
 * actually said by the other. This is where that gets fixed: select the words and
 * hand them over. Whatever captions have to be cut to make the passage its own
 * are cut -- usually one caption into three.
 *
 * Only the speaker the passage is *not* currently credited to is offered, because
 * handing a passage to whoever already has it does nothing.
 */
function renderHandOver(ctx, anchors) {
  const candidates = handCandidates(ctx).filter((name) => name !== anchors.speaker);
  ctx.el.quotebarHand.hidden = false;

  // A transcript naming one person -- the in-person case, a whole room under one
  // label -- has nobody to hand words *to* yet. Saying so beats the control
  // silently not existing, which reads as the feature being missing.
  if (!candidates.length) {
    ctx.el.quotebarHand.innerHTML =
      `<span class="quotebar__lead">said by</span>` +
      `<span class="quotebar__none">name someone in the strip above first</span>`;
    return;
  }

  const roster = ctx.data?.transcript?.roster || [];
  ctx.el.quotebarHand.innerHTML =
    `<span class="quotebar__lead">said by</span>` +
    candidates
      .map((name) => {
        const key = roster.find((entry) => entry.name === name)?.key;
        return `<button class="quotebar__who" type="button" data-hand-to="${escapeAttr(name)}"
                        title="These words were said by ${escapeAttr(name)}">${
          key ? `<kbd>${escapeAttr(key)}</kbd>` : ""
        }${escapeHtml(name)}</button>`;
      })
      .join("");
}

const escapeAttr = (value) => String(value).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function handOverSelection(ctx, speaker) {
  const anchors = ctx.pendingSelection || selectionAnchors(ctx);
  if (!anchors) return;

  window.getSelection()?.removeAllRanges();
  hideQuoteBar(ctx);
  // Cutting a caption measures it against the audio first, and the first
  // measurement in a session waits for the model to load. Several seconds of
  // silence after a click is indistinguishable from a click that did nothing.
  const done = ctx.working(`Giving those words to ${speaker}\u2026`);
  try {
    const result = await api(`/api/recordings/${ctx.recordingId}/selection/speaker`, {
      method: "POST",
      body: {
        start_cue_id: anchors.start_cue_id,
        start_char_offset: anchors.start_char_offset,
        end_cue_id: anchors.end_cue_id,
        end_char_offset: anchors.end_char_offset,
        speaker,
      },
    });
    if (result.backup_created) {
      ctx.notify(`Original transcript saved as ${result.backup_created}.`);
    }
    const cuts = result.splits
      ? `, cutting ${result.splits} caption${result.splits === 1 ? "" : "s"}`
      : "";
    ctx.notify(`Given to ${result.speaker}${cuts}.`);
    ctx.onTranscriptChanged?.(result.recording);
    const landed = ctx.chunks.findIndex((c) => c.cue_ids.includes(result.cue_ids[0]));
    if (landed >= 0) setCursor(ctx, landed, { scroll: true });
  } catch (error) {
    ctx.notify(`Could not hand those words over: ${error.message}`, {
      kind: "warn",
      key: null,
    });
  } finally {
    done();
  }
}

function refreshQuoteBar(ctx) {
  const anchors = selectionAnchors(ctx);
  ctx.pendingSelection = anchors;
  const bar = ctx.el.quotebar;

  if (!anchors) {
    bar.hidden = true;
    return;
  }

  ctx.el.quotebarTime.textContent = `→ ${formatTime(anchors.estimated_start)}`;
  renderHandOver(ctx, anchors);
  bar.hidden = false;

  const rect = anchors.rect;
  const width = bar.offsetWidth || 260;
  const left = Math.min(
    Math.max(8, rect.left + rect.width / 2 - width / 2),
    window.innerWidth - width - 8
  );
  const above = rect.top - bar.offsetHeight - 10;
  bar.style.left = `${left}px`;
  bar.style.top = `${above > 8 ? above : rect.bottom + 10}px`;
}

export function hideQuoteBar(ctx) {
  ctx.el.quotebar.hidden = true;
  ctx.pendingSelection = null;
}

export async function save(ctx, { color, focusNote = false } = {}) {
  const anchors = ctx.pendingSelection || selectionAnchors(ctx);
  if (!anchors) {
    ctx.notify("Select some text first.");
    return;
  }

  try {
    const { highlight } = await api(
      `/api/recordings/${ctx.recordingId}/highlights`,
      {
        method: "POST",
        body: {
          text: anchors.text,
          start_cue_id: anchors.start_cue_id,
          start_char_offset: anchors.start_char_offset,
          end_cue_id: anchors.end_cue_id,
          end_char_offset: anchors.end_char_offset,
          speaker: anchors.speaker,
          color: color || ctx.colors[0],
        },
      }
    );
    ctx.highlights.push(highlight);
    window.getSelection()?.removeAllRanges();
    hideQuoteBar(ctx);
    // Mark it inline too, so the transcript and the sidebar agree on which quote
    // was just added.
    ctx.activeHighlightId = highlight.id;
    applyHighlights(ctx);
    ctx.showTab("highlights");
    renderList(ctx);
    revealQuote(ctx, highlight.id, { focusNote });
  } catch (error) {
    ctx.notify(`Could not save the quote: ${error.message}`);
  }
}

export function copySelection(ctx) {
  const anchors = ctx.pendingSelection || selectionAnchors(ctx);
  if (!anchors) return;
  const speaker = anchors.speaker ? `${anchors.speaker} ` : "";
  const line = `"${anchors.text}" — ${speaker}(${formatTime(anchors.estimated_start)})`;
  navigator.clipboard?.writeText(line).then(
    () => ctx.notify("Quote copied."),
    () => ctx.notify("Could not reach the clipboard.")
  );
}

/* ----------------------------------------------------------------- list -- */

/* Text codes come from each coder's own codebook; the strip and the chips read
 * them from here, loaded from /api/library/text-codebook. */
export async function refreshTextCodes(ctx, { render = true } = {}) {
  const [book, vocabulary] = await Promise.all([
    api("/api/library/text-codebook"),
    api("/api/library/vocabulary"),
  ]);
  ctx.textCodes = book.codes;
  ctx.codeColors = book.colors;
  ctx.vocabulary = vocabulary.tags;
  ctx.textCodesLoaded = true;
  if (render) renderList(ctx);
  else {
    renderCodeStrip(ctx);
    levelQuotes(ctx);
  }
}

const codeById = (ctx, id) => (ctx.textCodes || []).find((c) => c.id === id);
const myCodeByName = (ctx, name) =>
  (ctx.textCodes || []).find((c) => isMine(c) && c.name.toLowerCase() === name.trim().toLowerCase());

/** A code chip that cannot be edited: colour, name, and whose it is. */
function staticChip(code) {
  if (!code) return "";
  return `<span class="chip"><i class="chip__swatch vc--${escapeHtml(code.color)}"></i>${escapeHtml(code.name)}${coderTag(code.coder)}</span>`;
}

/** The strip alone, so a code edit on one quote does not rebuild every card. */
function renderCodeStrip(ctx) {
  const inMode = ctx.highlights.filter(visibleNow);
  const counts = new Map();
  for (const highlight of inMode) {
    for (const id of highlight.codes || []) counts.set(id, (counts.get(id) || 0) + 1);
  }
  // A filter whose code just left every visible quote would strand the list
  // showing nothing, with no way back except a chip that is now gone.
  const stripCodes = (ctx.textCodes || [])
    .filter((c) => (isMine(c) || isCommon(c) ? true : visibleNow(c) && counts.has(c.id)))
    .map((c) => ({ ...c, count: counts.get(c.id) || 0, total: c.quote_count }));
  if (ctx.codeFilter && !stripCodes.some((c) => c.id === ctx.codeFilter)) ctx.codeFilter = null;
  renderStrip(ctx.el.tagFilters, {
    codes: stripCodes,
    filter: ctx.codeFilter,
    colors: ctx.codeColors || [],
    noun: "text code",
    kind: "text",
    onFilter: (id) => {
      ctx.codeFilter = id;
      renderList(ctx);
    },
    onAction: (action, code, value) => codebookAction(ctx, action, code, value),
  });
  return inMode;
}

export function renderList(ctx) {
  const list = ctx.el.highlightList;
  // Independent mode shows only your own quotes; collaborative shows everyone's.
  const inMode = renderCodeStrip(ctx);
  ctx.el.highlightCount.textContent = String(inMode.length);

  const visible = ctx.codeFilter ? inMode.filter((h) => (h.codes || []).includes(ctx.codeFilter)) : inMode;

  if (!visible.length) {
    list.innerHTML = inMode.length
      ? '<p class="empty">No quotes with that text code.</p>'
      : '<p class="empty">Select text in the transcript to save a quote. Your quotes are saved in your own folder inside the recording.</p>';
    levelQuotes(ctx);
    return;
  }

  const ordered = [...visible].sort((a, b) => a.start_time - b.start_time);
  list.innerHTML = ordered.map((highlight) => (isMine(highlight) ? ownCard(ctx, highlight) : otherCard(ctx, highlight))).join("");

  // Until the codebook has loaded, a field would show a quote's codes as empty,
  // and saving from it would erase them.
  if (!ctx.textCodesLoaded) {
    levelQuotes(ctx);
    return;
  }
  mountTagFields(list, {
    noun: "text code",
    getTags: (id) =>
      (ctx.highlights.find((h) => h.id === id)?.codes || []).map((cid) => codeById(ctx, cid)?.name).filter(Boolean),
    // Your own codes, and the common ones: picking a common name codes with your
    // own same-named code, which is later merged into the common one.
    getVocabulary: () => {
      const mine = (ctx.vocabulary || []).filter(isMine);
      const names = new Set(mine.map((e) => e.tag.toLowerCase()));
      return [...mine, ...(ctx.vocabulary || []).filter((e) => isCommon(e) && !names.has(e.tag.toLowerCase()))];
    },
    decorate: (name) => {
      const code = myCodeByName(ctx, name);
      return { before: `<i class="chip__swatch vc--${escapeHtml(code?.color || "slate")}"></i>` };
    },
    onCommit: (id, names) => {
      const highlight = ctx.highlights.find((h) => h.id === id);
      if (highlight) setCodes(ctx, highlight, names);
    },
  });
  levelQuotes(ctx);
}

function ownCard(ctx, highlight) {
  return `
      <article class="quote quote--${highlight.color}" data-id="${highlight.id}">
        <p class="quote__text" data-action="jump">${escapeHtml(highlight.text)}</p>
        <div class="quote__meta">
          <span class="quote__speaker">${escapeHtml(highlight.speaker || "—")}</span>
          <time>${formatTime(highlight.start_time)}</time>
          <span class="quote__tools">
            <span class="swatches">
              ${ctx.colors
                .map(
                  (color) =>
                    `<button class="swatch swatch--${color}" type="button" data-action="color" data-color="${color}" aria-pressed="${color === highlight.color}" aria-label="${color}"></button>`
                )
                .join("")}
            </span>
            <button class="icon-btn" type="button" data-action="copy" title="Copy quote">⧉</button>
            <button class="icon-btn" type="button" data-action="delete" title="Delete quote">✕</button>
          </span>
        </div>
        <textarea class="quote__note" rows="1" placeholder="Note" data-action="note">${escapeHtml(highlight.note || "")}</textarea>
        <div class="tagfield" data-id="${highlight.id}"></div>
      </article>`;
}

/** Another coder's quote in collaborative mode, or a common one: visible, labelled, read-only. */
function otherCard(ctx, highlight) {
  const chips = (highlight.codes || []).map((id) => staticChip(codeById(ctx, id))).join("");
  const common = isCommon(highlight);
  // A common quote's note is each contributor's own, labelled with who wrote it.
  const notes = common
    ? (highlight.notes || []).map((n) => `<p class="quote__note-text"><b>${escapeHtml(initialsOf(n.coder))}</b> ${escapeHtml(n.note)}</p>`).join("")
    : "";
  return `
      <article class="quote quote--${highlight.color} quote--theirs" data-id="${highlight.id}">
        <p class="quote__text" data-action="jump">${escapeHtml(highlight.text)}</p>
        <div class="quote__meta">
          <span class="quote__speaker">${escapeHtml(highlight.speaker || "—")}</span>
          <time>${formatTime(highlight.start_time)}</time>
          <span class="quote__by">${common ? `${coderTag("common")} from ${(highlight.contributors || []).map((c) => escapeHtml(initialsOf(c))).join(", ")}` : `by ${coderTag(highlight.coder)}`}</span>
          <span class="quote__tools">
            <button class="icon-btn" type="button" data-action="copy" title="Copy quote">⧉</button>
          </span>
        </div>
        ${highlight.note ? `<p class="quote__note-text">${escapeHtml(highlight.note)}</p>` : ""}
        ${notes}
        ${chips ? `<div class="chips">${chips}</div>` : ""}
      </article>`;
}

/** Save the codes named in a quote's field, adding any new names to your codebook first. */
async function setCodes(ctx, highlight, names) {
  try {
    const ids = [];
    for (const name of names) {
      let code = myCodeByName(ctx, name);
      if (!code) {
        // A name taken from a common code keeps that code's colour and description.
        const common = (ctx.textCodes || []).find((c) => isCommon(c) && c.name.toLowerCase() === name.trim().toLowerCase());
        const body = common ? { name, color: common.color, description: common.description } : { name };
        const result = await api("/api/library/text-codebook", { method: "POST", body });
        ctx.textCodes = result.codes;
        code = result.code;
      }
      ids.push(code.id);
    }
    await patch(ctx, highlight, { codes: ids }, { rerender: false });
    // Only the strip: rebuilding the cards would take focus out of the field
    // you are typing in, and the next keys would go to the reader's shortcuts.
    await refreshTextCodes(ctx, { render: false });
  } catch (error) {
    ctx.notify(`Could not save the text codes: ${error.message}`, { kind: "warn" });
    renderList(ctx);
  }
}

/** The strip's ⋯ menu, applied to your own codebook or to a common code. */
async function codebookAction(ctx, action, code, value) {
  const base = `/api/library/text-codebook/${code.id}`;
  try {
    if (action === "move" || action === "return") {
      const done =
        action === "move"
          ? await openMoveDialog({ kind: "text", code, common: (ctx.textCodes || []).filter(isCommon) })
          : await returnToCoders("text", code);
      if (!done) return;
      ctx.highlights = (await api(`/api/recordings/${ctx.recordingId}`)).highlights;
      applyHighlights(ctx);
      ctx.notify(action === "move" ? `Moved “${code.name}” to common.` : `Returned ✓ ${code.name} to its coders.`);
    }
    if (action === "rename") await api(base, { method: "PATCH", body: { name: value } });
    if (action === "describe") await api(base, { method: "PATCH", body: { description: value } });
    if (action === "color") await api(base, { method: "PATCH", body: { color: value } });
    if (action === "delete") await api(base, { method: "DELETE" });
    if (action === "merge") {
      const result = await api(`${base}/merge`, { method: "POST", body: { into: value } });
      ctx.highlights = (await api(`/api/recordings/${ctx.recordingId}/highlights`)).highlights;
      applyHighlights(ctx);
      ctx.notify(`Merged “${code.name}” (${result.moved} quote${result.moved === 1 ? "" : "s"}).`);
    }
    if (action === "delete" && ctx.codeFilter === code.id) ctx.codeFilter = null;
  } catch (error) {
    ctx.notify(error.message, { kind: "warn" });
  }
  window.dispatchEvent(new CustomEvent("commonchange"));
  await refreshTextCodes(ctx);
}

/** Bring a newly saved quote into view in the sidebar and mark it. */
export function revealQuote(ctx, highlightId, { focusNote = false } = {}) {
  const card = ctx.el.highlightList.querySelector(`[data-id="${highlightId}"]`);
  if (!card) return;
  ctx.leadScroll?.("list");
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  card.classList.add("quote--new");
  setTimeout(() => card.classList.remove("quote--new"), 1800);
  // Keyboard focus is only taken when a note was explicitly asked for --
  // otherwise the next `j` would type into a textarea instead of moving on.
  if (focusNote) card.querySelector(".quote__note")?.focus();
}

function bindList(ctx) {
  ctx.el.highlightList.addEventListener("click", async (event) => {
    const card = event.target.closest(".quote");
    if (!card) return;
    const highlight = ctx.highlights.find((h) => h.id === card.dataset.id);
    if (!highlight) return;
    const action = event.target.closest("[data-action]")?.dataset.action;
    // Another coder's quote is read-only; only jumping and copying apply.
    if (!isMine(highlight) && action !== "jump" && action !== "copy") return;

    if (action === "jump") {
      jumpToHighlight(ctx, highlight);
    } else if (action === "color") {
      await patch(ctx, highlight, { color: event.target.dataset.color });
    } else if (action === "copy") {
      const speaker = highlight.speaker ? `${highlight.speaker} ` : "";
      navigator.clipboard
        ?.writeText(`"${highlight.text}" — ${speaker}(${formatTime(highlight.start_time)})`)
        .then(() => ctx.notify("Quote copied."));
    } else if (action === "delete") {
      await remove(ctx, highlight);
    }
  });

  const commit = async (event) => {
    const field = event.target.closest("[data-action]");
    if (!field) return;
    const card = field.closest(".quote");
    const highlight = ctx.highlights.find((h) => h.id === card?.dataset.id);
    if (!highlight) return;

    if (field.dataset.action === "note" && field.value !== (highlight.note || "")) {
      await patch(ctx, highlight, { note: field.value }, { rerender: false });
    }
  };

  ctx.el.highlightList.addEventListener("change", commit);
  ctx.el.highlightList.addEventListener("focusout", commit);
}

function jumpToHighlight(ctx, highlight) {
  const cue = ctx.cueById.get(highlight.start_cue_id);
  if (!cue) return;
  const chunk = ctx.chunks.find((c) => c.cue_ids.includes(cue.id));
  ctx.activeHighlightId = highlight.id;
  if (chunk) scrollToChunk(ctx, chunk.id);
  flashCue(ctx, cue.id);
  seekAndPlay(ctx, highlight.start_time);
  applyHighlights(ctx);
}

async function patch(ctx, highlight, body, { rerender = true } = {}) {
  try {
    const response = await api(
      `/api/recordings/${ctx.recordingId}/highlights/${highlight.id}`,
      { method: "PATCH", body }
    );
    Object.assign(highlight, response.highlight);
    applyHighlights(ctx);
    if (rerender) renderList(ctx);
  } catch (error) {
    ctx.notify(`Could not update the quote: ${error.message}`);
  }
}

async function remove(ctx, highlight) {
  try {
    await api(`/api/recordings/${ctx.recordingId}/highlights/${highlight.id}`, { method: "DELETE" });
    ctx.highlights = ctx.highlights.filter((h) => h.id !== highlight.id);
    applyHighlights(ctx);
    renderList(ctx);
  } catch (error) {
    ctx.notify(`Could not delete the quote: ${error.message}`);
  }
}
