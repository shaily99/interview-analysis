/* A token field for the text codes on a quote.
 *
 * Suggestions come from the text codebook (yours and common names), ranked
 * prefix first, then by how widely each is used, so the established code is the
 * obvious pick. A common name picked here creates your own code of that name.
 */

import { escapeHtml } from "./util.js";

const MAX_SUGGESTIONS = 8;

function normalize(tag) {
  return tag.trim().replace(/\s+/g, " ");
}

/** Rank vocabulary against what has been typed: prefix beats substring. */
function rank(vocabulary, query, chosen) {
  const needle = query.trim().toLowerCase();
  const taken = new Set(chosen.map((t) => t.toLowerCase()));

  return vocabulary
    .filter((entry) => !taken.has(entry.tag.toLowerCase()))
    .map((entry) => {
      const name = entry.tag.toLowerCase();
      if (!needle) return { entry, score: 0 };
      if (name.startsWith(needle)) return { entry, score: 2 };
      if (name.includes(needle)) return { entry, score: 1 };
      return null;
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score || b.entry.recording_count - a.entry.recording_count)
    .slice(0, MAX_SUGGESTIONS)
    .map((hit) => hit.entry);
}

/**
 * Turn every `.tagfield` placeholder under `root` into a working field.
 *
 * @param root         element containing the placeholders
 * @param getTags      (id) -> the tags currently on that quote
 * @param getVocabulary() -> [{tag, quote_count, recording_count}]
 * @param onCommit     (id, tags) -> persist
 * @param decorate     (tag) -> {before, after} html around a chip's name, e.g. a
 *                     colour swatch and the coder's initials
 * @param noun         what a token is called in the field's labels
 */
export function mountTagFields(root, { getTags, getVocabulary, onCommit, decorate, noun = "tag" }) {
  for (const host of root.querySelectorAll(".tagfield:not([data-ready])")) {
    mount(host, { getTags, getVocabulary, onCommit, decorate, noun });
  }
}

function mount(host, { getTags, getVocabulary, onCommit, decorate, noun }) {
  const id = host.dataset.id;
  host.dataset.ready = "1";
  let tags = [...(getTags(id) || [])];
  let active = -1;

  const listId = `tags-${id}`;
  host.innerHTML = `
    <div class="tagfield__chips"></div>
    <input class="tagfield__input" type="text" role="combobox" autocomplete="off"
           aria-expanded="false" aria-controls="${listId}" aria-autocomplete="list"
           placeholder="Add a ${noun}">
    <ul class="tagfield__list" id="${listId}" role="listbox" hidden></ul>`;

  const chips = host.querySelector(".tagfield__chips");
  const input = host.querySelector(".tagfield__input");
  const list = host.querySelector(".tagfield__list");

  const drawChips = () => {
    chips.innerHTML = tags
      .map(
        (tag, index) => {
          const { before = "", after = "" } = decorate?.(tag) || {};
          return `<span class="chip">${before}${escapeHtml(tag)}${after}<button type="button" class="chip__x"
             data-index="${index}" aria-label="Remove ${escapeHtml(tag)}">✕</button></span>`;
        }
      )
      .join("");
  };

  const closeList = () => {
    list.hidden = true;
    active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };

  const drawList = () => {
    const query = input.value;
    const matches = rank(getVocabulary(), query, tags);
    const typed = normalize(query);
    const exact = matches.some((m) => m.tag.toLowerCase() === typed.toLowerCase());

    const rows = matches.map(
      (entry, index) => `
      <li class="tagfield__option" role="option" id="${listId}-${index}"
          aria-selected="${index === active}" data-tag="${escapeHtml(entry.tag)}">
        <span class="tagfield__tag">${escapeHtml(entry.tag)}</span>
        <span class="tagfield__use">${
          entry.recording_count
            ? `${entry.quote_count} in ${entry.recording_count} recording${entry.recording_count === 1 ? "" : "s"}`
            : "used before, nothing on it now"
        }</span>
      </li>`
    );

    // Offer to coin a new one only when it is genuinely new, so the established
    // tag is always the easier choice.
    if (typed && !exact && !tags.some((t) => t.toLowerCase() === typed.toLowerCase())) {
      rows.push(`
        <li class="tagfield__option tagfield__option--new" role="option"
            id="${listId}-${rows.length}" aria-selected="${rows.length === active}"
            data-tag="${escapeHtml(typed)}" data-new="1">
          <span class="tagfield__tag">${escapeHtml(typed)}</span>
          <span class="tagfield__use">new ${noun}</span>
        </li>`);
    }

    if (!rows.length) {
      closeList();
      return;
    }
    list.innerHTML = rows.join("");
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `${listId}-${active}`);
  };

  const commit = () => onCommit(id, [...tags]);

  const add = (tag) => {
    const clean = normalize(tag);
    if (!clean) return;
    if (!tags.some((t) => t.toLowerCase() === clean.toLowerCase())) {
      tags.push(clean);
      drawChips();
      commit();
    }
    input.value = "";
    closeList();
  };

  const remove = (index) => {
    tags.splice(index, 1);
    drawChips();
    commit();
  };

  drawChips();

  chips.addEventListener("click", (event) => {
    const button = event.target.closest(".chip__x");
    if (button) remove(Number(button.dataset.index));
  });

  input.addEventListener("input", () => {
    active = -1;
    drawList();
  });

  input.addEventListener("focus", drawList);

  input.addEventListener("blur", () => {
    // Late enough for a click on an option to land first.
    setTimeout(() => {
      if (!host.contains(document.activeElement)) closeList();
    }, 140);
  });

  input.addEventListener("keydown", (event) => {
    const options = [...list.querySelectorAll(".tagfield__option")];

    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) drawList();
      const count = list.querySelectorAll(".tagfield__option").length;
      if (!count) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      // From nothing selected, down lands on the first option and up on the
      // last. Getting this wrong skips the top suggestion -- which is the most
      // established tag, and the one the field exists to steer you towards.
      active = active < 0 ? (step > 0 ? 0 : count - 1) : (active + step + count) % count;
      drawList();
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      const options2 = [...list.querySelectorAll(".tagfield__option")];
      if (active >= 0 && options2[active]) add(options2[active].dataset.tag);
      else add(input.value);
      return;
    }

    if (event.key === "," || event.key === "Tab") {
      if (input.value.trim()) {
        event.preventDefault();
        add(input.value);
      }
      return;
    }

    if (event.key === "Escape") {
      if (!list.hidden) {
        event.stopPropagation();
        closeList();
      }
      return;
    }

    // Backspace on an empty box takes the last chip back off.
    if (event.key === "Backspace" && !input.value && tags.length) {
      event.preventDefault();
      remove(tags.length - 1);
    }
  });

  list.addEventListener("mousedown", (event) => {
    const option = event.target.closest(".tagfield__option");
    if (option) {
      event.preventDefault();
      add(option.dataset.tag);
      input.focus();
    }
  });
}
