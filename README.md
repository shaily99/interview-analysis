# subtitle-search

A reading-first viewer for Zoom transcripts. The text is the document; the
recording is a reference you drop into when you need to hear how something was
said. Point it at a recording folder, read, pull quotes.

```bash
uv sync                                  # exact versions from uv.lock
uv run subtitle-search /path/to/folder
```

Or with pip, if you would rather not add a tool:

```bash
pip install -e .
subtitle-search /path/to/recording-folder
```

It opens `http://127.0.0.1:8765`.

## The workflow

**Download → anonymize → analyze.** Three steps, and the middle one is why the
folder layout matters: the anonymizer is told exactly one thing, and it reads it
off the folder name.

```
study/
  raw/                                        ← 1. downloaded from Zoom
    P01/
      GMT20240301-140000_Recording.transcript.vtt
      GMT20240301-140000_Recording_1920x1080.mp4
    P02/
      GMT20240302-093000_Recording.transcript.vtt
      GMT20240302-093000_Recording.m4a
  anonymized/                                 ← 2. written by the script
    P01/
      GMT20240301-140000_Recording.transcript.vtt
      GMT20240301-140000_Recording_1920x1080.mp4
    P02/
      …
```

```bash
python scripts/anonymize_zoom.py raw/ anonymized/ --dry-run   # 2. look first
python scripts/anonymize_zoom.py raw/ anonymized/

subtitle-search anonymized/                                   # 3. read it
```

**One folder per participant, and the folder name is the participant ID.** That
is the whole of what you tell the script; everything else it works out from the
transcripts inside. `raw/P01/` becomes `anonymized/P01/`, and `P01` is what
replaces that person's name wherever it appears. Sub-folders are walked and the
shape is preserved, so whatever Zoom handed you can go in as it came — the tool
reads those `GMT…` timestamps to lay [interrupted
sessions](#interrupted-sessions) end to end.

**The pass replaces speaker names, and nothing else.** In the attribution, in
the body of the transcript, and in filenames. Places, employers, job titles and
anyone mentioned who never speaks all survive untouched, on purpose — this is a
first sweep over transcripts you are going to read anyway, not a
de-identification pass, and it does not make a transcript safe to hand on
unread. Whatever it could not resolve is printed under **REVIEW**, which is the
part to actually read. There is more on what it refuses and what it only reports
in [Anonymizing a set of interviews](#anonymizing-a-set-of-interviews).

**Keep the two trees apart.** The script never writes into its input, and
refuses to write into an output folder that already holds files unless you pass
`--force`. Point the reader at `anonymized/` and the real names stay in one
directory you can delete when the study is over.

The reader will happily open `raw/` — a folder holding a transcript *is* a
recording, and a folder of those is a library — so nothing stops you reading the
un-anonymized copy. Which is the point of keeping them in separate trees with
different names.

## Dependencies

Tiered, so the part you use daily has the fewest ways to break:

| | |
|---|---|
| core | `fastapi`, `uvicorn`, `rapidfuzz` — reading, correcting, quoting |
| `analysis` | `numpy`, `scikit-learn` — the map, graph and signals |
| `neural` | `sentence-transformers` — paraphrase-aware similarity |
| `align` | `torch`, `transformers` — [measured word timings](#word-timings) |
| `dev` | `pytest`, `httpx` |

```bash
uv sync --all-extras                          # everything
pip install -e '.[analysis,neural,align]'     # the pip equivalent
```

`align` also wants `ffmpeg` on `PATH`, which is not a Python package and so cannot
be pinned in the lockfile.

The extras are genuinely optional: `semantics.py` and `alignment.py` import their
heavy dependencies *inside* their functions, so the app starts, the reader and
library work, and only the features that need them return a 503 that names the
fix. Every rung degrades to the one below — UMAP to t-SNE to PCA, the language
model to word overlap, measured timings to interpolated ones.

Every requirement carries an upper bound, and `uv.lock` pins the resolved graph
of all 80 packages. This matters more than install size for a tool meant to open
a study years after it was recorded: without a ceiling, a future scikit-learn
that changed a clustering default would quietly reshuffle your map.

## What it expects

A folder with at least one `.vtt` transcript and its media. Video is preferred
(`.mp4`, `.m4v`, `.mov`, `.webm`, `.mkv`) with audio as a fallback (`.m4a`,
`.mp3`, `.wav`, …) — a typical Zoom folder with both an `.mp4` and an `.m4a`
uses the `.mp4`.

**Everything in the folder is one session.** Zoom splits a meeting into several
recordings when it gets interrupted, and each part restarts its transcript at
00:00. The folder's files are grouped into parts, ordered, and laid end to end on
a single continuous timeline.

## Interrupted sessions

Part two starts where part one's *recording* ended — not where its last caption
ended, since a recording usually runs on past the final word. Durations are read
straight out of the MP4/M4A container, so this needs no ffprobe; if a part's
media is missing, its last caption plus a short tail is used instead.

```
part 1   0:00:00 ──────────────── 1:30:00
         [ 10 min interruption, shown as a marker ]
part 2   1:30:00 ──────────────── 2:15:00
```

Times never restart, and the timeline has **no holes** — every session timestamp
has audio behind it, so scrubbing anywhere lands on something. The real
interruption is shown where it happened, as a rule across the reading column
reading *"Recording resumed after 10 minutes · 2:12 PM"*. Playback crosses the
seam by itself: playing off the end of one recording continues into the next, and
seeking or jumping to a quote switches files transparently.

Blocks never merge across a break, even when the same person is still talking.

Parts are ordered by the GMT timestamp Zoom stamps into cloud filenames
(`GMT20240301-140000_Recording.transcript.vtt`), which also pairs each transcript
with its media. Failing that, transcripts and media are paired on a shared stem
once Zoom's decoration is stripped, then by trailing counter and modified time.
Wall-clock interruption lengths need the filename timestamps; without them the
marker says which part is starting but not how long the gap was.

## Your recordings stay put

The rule is about your data, not about your dependencies. **Nothing from a
recording — audio, transcript, quotes, notes, codes — is ever sent anywhere.** The
server binds to `127.0.0.1` only, and the page has no CDN assets, web fonts, or
analytics, so it is drawn entirely with fonts already on your machine and nothing
about what you are reading is observable off it.

Downloads in the other direction are fine and are a normal install step: the
`neural` similarity backend fetches a sentence-transformer model once, then runs
it in this process. Inbound and local — the model comes to the quotes, never the
other way round.

## Reading

The transcript is grouped into blocks: contiguous captions from one speaker read
as a single passage, with paragraph breaks inserted where the speaker paused for
more than a couple of seconds. Underneath, every original caption is kept
separately — that is what lets an arbitrary text selection resolve to a point in
the recording.

Blocks hang off a **time spine** down the left, with timestamps as ticks. The
marker on the spine shows where you are.

Playback speed sits in the transport bar, from 0.5x for a mumbled passage up to
2x for a stretch you already know. Pitch correction is on, so a voice at 1.5x
still sounds like that person rather than a cartoon — tone is half of why the
recording is here. The speed is remembered between sessions, survives crossing
from one recording into the next, and the themes page plays quotes at it too.

The reader has three panes: the video with its video code rows, the transcript,
and the text codes. Drag a divider (or focus it and use the arrow keys) to
resize; double-click resets it. – collapses a pane to a labelled strip; click
the strip to reopen it. Sizes are remembered per browser.

### Two modes

Reading is the default:

- **Reading** — the cursor follows where you are in the text, and the player is
  silently cued to match. Press play and it starts where you are looking.
- **Following** — starting playback switches here: the transcript keeps up with
  the audio and scrolls itself.

Scrolling or moving the cursor by hand drops back to Reading **without stopping
playback**, and a "Follow along" button appears to re-attach.

### Keys

| | |
|---|---|
| `j` / `k` | previous / next block |
| `Space` | play / pause |
| `←` / `→` | seek ∓5s |
| `/` | search |
| `e` | correct the current block |
| `h` | save the selection as a quote |
| `c` | copy the selection with speaker and timestamp |
| `f` | toggle following |
| `[` / `]` | slower / faster |
| `s` | break this block into its captions |
| double-click | cut the caption in front of that word |
| `1`…`9` | assign this block to a speaker, then move on |
| `Esc` | clear selection |
| `i` / `o` | start / end a [video code](#video-codes) at the playhead |
| `x` | delete your video code under the playhead |

## Correcting the transcript

Press `e` on a block to fix what the transcript got wrong. Reading shows merged
prose, but a correction has to land on a single caption, since that is what gets
written back — so edit mode opens the block into the captions underneath it, each
with its own timestamp and a button to replay just that line. Caption boundaries
become visible exactly when they matter and stay invisible the rest of the time.

Enter saves a line and moves to the next; Esc abandons the line you are on and
finishes. Focusing a line cues the player to it without interrupting playback, so
you can correct while listening.

### Cutting a caption in two

Zoom's worst habit is putting the end of one person's turn and the start of the
next inside a *single* caption — `So walk me through it. Sure, I read it first.`
No amount of reattributing captions separates those, because they are one
caption. So the caption itself has to divide first.

**Double-click the first word of the second turn.** That word begins the new
caption, the cursor lands on it, and a speaker key names it — so `1 2 1 2` carries
on without ever leaving the reading view. Pointing inside a word cuts in front of
that whole word rather than mid-word, and pointing at the space between two words
counts as pointing at the one after it. The reading column shows an I-beam, since
the prose is there to be pointed at and selected rather than clicked.

The same cut is available on the caret in edit mode, with **⌘⏎** (`Ctrl+Enter`) —
useful when you are already correcting the words and can see the caption
boundaries. Double-click means *select a word* while editing, as it should.

The boundary comes from the audio. Splitting first measures that caption's words,
so the cut lands in the real silence between the two speakers: the first half ends
on its last word, the second begins on its first, and the pause between them
belongs to neither — which is more accurate than Zoom's own abutting captions. If
alignment is unavailable the two halves share one interpolated boundary instead,
and the toast says which of the two you got. Both halves keep the speaker label,
so the file still re-parses as it did. The cut point travels with the text it was
measured against, so it lands between the same two words even if the line has
unsaved typing in it.

The toast that reports a cut carries an **Undo**, since the moment after cutting
is when you find out you did not mean it. It puts the two halves back and restores
the transcript byte for byte. Undo carries the two halves *as written*, so if
anything else moved the captions in between it refuses rather than joining
whichever two now hold those numbers.

### Handing a passage to whoever said it

The commonest version of this problem while *reading* rather than editing: half of
what Zoom filed under one person was plainly said by the other, and you can see
exactly which words.

Select them. The quote bar that appears offers **said by** and the speakers it
knows about — the roster first, then anyone else the transcript names — and picking
one hands those words over. Whatever captions have to be cut for the passage to be
its own are cut, which is usually one caption into three: what came before, the
passage, and what came after. Only the speakers the passage is *not* already
credited to are offered, since handing it to whoever has it does nothing.

A selection that starts or ends mid-word takes the whole word with it, because a
caption boundary inside a word leaves two fragments. The cuts are measured cuts
like any other, so the new caption starts on its first word rather than in the
tail of somebody else's sentence.

The speakers on offer come from the roster and the transcript's own labels, so a
transcript with neither has nothing to offer yet — name someone with **+ Speaker**
in the strip under the header first.

**Quotes are recredited, not just re-anchored.** A quote inside the passage follows
its words *and* changes hands, because a quote stores who said it and that is what
gets copied out and read months later. A misattributed quote is the one error here
that could end up in something published, so reattributing captions carries through
to every quote anchored in them — including from the speaker field in edit mode and
the speaker keys, which had been leaving quotes crediting the wrong person.

### Joining captions back together

Zoom's opposite failure is chopping one sentence across three captions, so a quote
that reads as a single thought is three anchors underneath.

In edit mode, **⌫ at the start of a line joins it to the line above** — what
backspace means in every text editor, applied to captions. The joined caption runs
from the first one's start to the last one's end, and the words are joined in
order.

One caption carries one speaker, so joining across two of them keeps the first
label and drops the other. That is said out loud in a warning rather than left to
be discovered, because it is usually a sign the join was not what you wanted.

Joining is how Undo works, and both go through the same request. Quotes follow
their words: a quote inside an absorbed caption moves to where those words now sit,
one spanning the whole run collapses into the single caption, and ids after it shift
back. **Measured word timings need no attention at all** — joining leaves the
recording's sequence of words identical, so every measurement still describes the
same word, exactly as with a split.

Splitting and joining are the two edits that change the *number* of captions,
which renumbers every caption after them. Quotes are re-anchored across that
shift rather than left pointing at their old numbers: a quote after a cut follows
its caption, and a quote inside the split one lands in whichever half now holds
its words — including one that straddles the cut, which ends up spanning both.

## Word timings

Zoom times a caption's *edges* honestly and tells you nothing about its interior.
Ask where the word `Sure` is, thirty characters into a ten-second caption, and the
answer is a proportion of the way through — which is wrong by roughly the length
of whatever pause the speaker took. In interview audio that is regularly a second
or more, and it is worst exactly where you care: the handover between two people.

**Measure word timings** in the strip under the header fixes that. It aligns the
transcript to the audio and stores where every word actually falls, after which a
quote's timestamp is a measurement rather than an estimate. Measured blocks are
marked: their timestamp is underlined, so you can tell which kind of number you
are looking at without having to ask.

This is *forced alignment*, not re-transcription — an important difference. The
words are an **input**: they come from the transcript, Zoom's plus your
corrections, and only timing comes back. Nothing in the process can change a word,
which is why it is safe to run on a folder you have already been quoting from. A
re-transcription pass would return a different set of words and strand every quote
anchored into the old ones.

It runs on **demand and in batches**, never automatically. The button walks the
session a couple of dozen captions at a time so there is progress to watch and a
Stop that keeps everything measured so far. Splitting a caption measures that one
caption first, on its own, because a cut is where a guess does the most lasting
damage — it becomes a timestamp in the file that outlives the decision.

Timings live in `session.words.json` beside the recording, never in the VTT, so
the transcript stays the file Zoom wrote plus your corrections and a folder
without that file behaves exactly as it always did.

Each word is addressed by its position in the recording's word sequence rather
than by caption, which is what makes measuring one caption at a time safe:
splitting a caption in two leaves the sequence of words *identical*, so every
timing stays valid with nothing to remap. Rewording a caption does change the
sequence, so those timings shift — and because every stored timing carries the
word it was measured against, a desync is caught on load and falls back to
interpolation instead of quietly reporting the wrong second of audio.

**What it needs and what it costs.** `ffmpeg` on `PATH`, the `align` extra, and a
one-time ~360MB model download. After that, roughly 0.2s per caption on a laptop
CPU, so a one-hour interview is a minute or two — and the audio is read locally
and never sent anywhere. Where two people talk over each other it will place words
confidently in the wrong gap, and words it has no letters for (`2024`, `50%`) get
no timing and are interpolated across. Anything unmeasured simply behaves as it
did before, caption by caption.

**The original is preserved.** Before the first change to a transcript, it is
copied to `<name>_original.vtt`. That copy is written once and never touched
again, so it always holds the file as it came off Zoom regardless of how many
corrections follow. Backups are skipped by folder discovery, so they never get
read as another recording.

**Edits are spliced, not regenerated.** Only the edited caption's text is
replaced; every other byte stays as it was, including line endings and any block
the parser could not read. Rebuilding the file from parsed data would silently
drop those, and a transcript is the wrong place to lose things quietly.

**Quotes follow their words.** Correcting a line re-anchors every quote
overlapping it, so a quote keeps covering the same words rather than drifting by
however many characters you inserted. The quote's saved text is refreshed too, so
it reflects the correction instead of preserving the error.

Timestamps are not editable, but **who said a line is**. Zoom segments badly: a
trailing clause routinely lands under whoever spoke before it. In edit mode each
line carries a speaker field alongside its words, so the fix is to say who
actually said it.

The name is written into the transcript as a normal label *and* into a
`NOTE speakers:` roster at the top of the file. Detection has to stay
conservative — it would never accept a one-word name on a single line — but an
assignment is a decision, not a guess, so the roster makes it survive re-parsing.
It is standard WebVTT, ignored by anything that plays the file.

## Recordings made in a room

An in-person session recorded through one laptop comes back with the whole room
filed under whoever started the meeting. One speaker label carrying several
people is worse than none, so:

- **a transcript with exactly one speaker is never joined** — every caption
  stands alone, because joining on that label would invent a monologue out of a
  conversation
- **joining resumes the moment a second speaker exists**, so blocks reform as you
  assign them
- **the anonymizer labels a lone speaker `unknown`** rather than the participant
  ID, since the label identifies nobody in particular

A transcript with *no* speaker labels at all is a different situation and still
breaks on long pauses, which at least reads.

### Labelling with the keyboard

Name the speakers in the strip under the header — **+ Speaker** — and each one
gets a key. Rename with ✎, remove with ✕. Nothing here needs the transcript
opened; the roster is stored in it as a standard `NOTE speakers:` line, but that
is a storage detail rather than somewhere to type.

Where a transcript already names several speakers, they are offered as one-click
adds so they only need keys. Where it names exactly one, they are not: that label
is the room rather than a person, and rostering it would let its lines join and
defeat labelling them apart.

Digits by default — nothing else in the reader uses them, and `1`–`5` fall under
the hand that is not on the trackpad. Any single character works if you would
rather: `q=Note taker`. A bare name gets the next free digit, so the shorthand
`NOTE speakers: Interviewer, Participant` is enough.

The keys are shown in a strip under the header, so the bindings are never
something to remember. Pressing one assigns the block at the cursor and moves to
the next, which makes a labelling pass a run of keypresses: `1 2 1 2` down the
transcript.

When a block holds more than one person — Zoom routinely files a trailing
comment under whoever spoke before it — `s` breaks it into its captions so each
can be assigned separately, and `1 2 1 2` carries on. Splitting writes nothing:
it only exposes the seams, and the assignment is what persists. The keys need a
roster, so a transcript with no `NOTE speakers:` line has nothing to press.

When two people share a *single* caption, exposing the seams is not enough —
there is no seam. That case needs the caption cut, which is
[⌘⏎ in edit mode](#cutting-a-caption-in-two).

Once anyone is on the roster, **joining follows assignment**: only rostered
speakers merge, and whatever label the transcript arrived with stays line by
line. Without that, the first assignment would collapse every unlabelled line
after it into one block and the next keypress would relabel all of them at once.
A transcript with no roster joins exactly as it always did.

## Quotes

Select any text — across caption or speaker boundaries — and a bar appears
showing the estimated timestamp. Pick a color to save it. The quote card appears
in the Text codes pane, level with its block, and is briefly marked. Keyboard
focus stays in the transcript unless you chose "Save with note".

- A quote carries any number of *text codes*: codes from your text codebook,
  each with a name, colour and description. Type in the card's field to pick one
  or add a new one.
- The strip at the top of the pane lists the codebook with counts; click a chip
  to filter. Each code's ⋯ menu renames, describes, recolours, merges, moves it
  to common, or deletes it once nothing uses it.
- Quotes are saved on every change to `<recording>/coders/<you>/quotes.json`.
  There is no save step.

Timestamps come from where your selection falls inside a caption rather than
snapping to the caption's start — on Zoom's longer captions that is a difference
of several seconds. By default that position is interpolated, which is a guess;
[measuring the word timings](#word-timings) replaces it with the real thing.
Playback seeks 0.75s early either way, so the first word is not clipped.

## Video codes

Quotes code what was *said*. Video codes code what *happened* — `scroll` from
1:02 to 1:30, `hesitates` over a button — as spans of the recording's time rather
than of its words.

Video codes and text codes are separate codebooks, never merged.

**Marking.** Press `i` where something starts and `o` where it ends, both at the
playhead. An open start shows as a mark on the code rows. `o` pauses playback and
opens a picker listing your video codes; the span ends where you pressed `o`.
Type to narrow it, `Enter` to choose, or type a new name to add it. `Esc` in the
picker keeps the start open, `Esc` elsewhere drops it. A code can carry its own
key: with a start open, that key ends the span and codes it. `x` deletes your
span under the playhead, with an Undo. Spans may overlap.

**Where they show.** Never on the words.

- **Rows**, under the video: one row per coder (✓ Common when it has spans, you,
  then others in collaborative mode) over a zoomable window that follows the
  playhead. Click a span to seek; drag an end of your own span to move it. A name
  too long for its span is cut off; hover shows code, coder and times.
- **List**, the tab beside Rows: every span with its code, times, note, and a
  menu to recode or delete it.

**Coding without the transcript.** **▶ Code video** opens `/code`: the video and
its code rows only. Same keys, plus `,`/`.` for one-second steps and `m` to mute.
Both pages re-read the files when they regain focus.

**The codebook.** Each coder has their own; edit it from the strip's ⋯ menu or
the [Codebook page](#coders-modes-and-common-codes). Spans refer to a code by
id, so a rename follows everywhere. A code in use cannot be deleted; merge it
into another, or remove its spans first. Keys the pages already use (`j`, `k`,
`h`, `i`, `o`, `m`, digits, …) cannot be code keys.

**Storage.** Your codebook is `<study>/coders/<you>/video_codebook.json`; your
spans are `<recording>/coders/<you>/video_codes.json`, in session seconds, so a
span can cross an interruption and correcting a caption never moves one.

## Coders, modes and common codes

- **Login.** On first open, pick your name or type a new one; initials are made
  from it. There is no password. Click your name in the header to switch.
- **Modes.** The header's switch applies to every page. *Independent* shows your
  work and common codes. *Collaborative* adds everyone else's, labelled with
  initials and read-only.
- **Refresh.** ↻ Refresh reads what collaborators have synced, pushes your common
  changes to the shared files, and collects items returned to you. The header
  shows when the folder was last read and warns about sync conflict copies.
- **Codebook page** (`/codebook`). One codebook at a time, list beside the code
  page: description, the themes holding the code, and every quote or span that
  carries it, with checkboxes to remove them in bulk.
- **Move to common** (⋯ menu). Confirm the code is final and give a description.
  It becomes a new common code, or merges into an existing one; a name clash asks
  merge or rename. Its quotes or spans move with it. Exact duplicates from two
  coders combine: text on the same words, video with both ends within 0.5 s.
  Anyone can edit common codes.
- **Using a common code** in independent mode creates your own code of the same
  name; move it to common later to merge.
- **Return to coders** (⋯ menu on a common code). Each contributor gets their
  quotes or spans back under their own code with the common code's name and
  description. Common codes keep a History of moves, returns, edits, deletions
  and removals.
- **Themes** follow the same modes. [Common themes](#themes-analysis-across-recordings)
  hold only common codes; a theme can be moved to common once all its codes are.
- Files from before coders (`session.highlights.json`, `session.video_codes.json`,
  `library.video_codebook.json`, `library.themes.json`) are ignored and left on
  disk.

## Finding quotes

Exact matches rank first, then close ones (for when the transcript did not hear
the word the way you remember it). Matching runs across block text, so a phrase
split across two captions is still findable. `.*` switches to regex. Matches
drop down under the search box.

## Checking the parse

Speaker detection is a heuristic. Zoom writes the speaker either as a
`Name:` prefix inside the caption or as a `<v Name>` tag; the prefix form is
ambiguous, because a sentence like *"So here's my point: I disagreed"* looks
identical to one. Candidate prefixes are therefore collected across the whole
file and only promoted to speakers if they read as a proper name or recur, so a
stray mid-sentence colon cannot invent a speaker. Names that differ only in case
(`PILOT3`, `Pilot3`) are one speaker, spelled as on the roster, or else as first
seen; the file is left as it is.

The app reports what it decided in a banner on first open. To check a folder
without starting the server:

```bash
subtitle-search --dump-parse /path/to/recording-folder
```

It prints the speakers, cue counts and quote counts per coder, plus the part layout — which recording
starts at which session time, how long each runs, and how long each interruption
was. A wrong order or a wrong duration silently shifts every timestamp after it,
so that layout is the thing worth checking on real files.

## Other flags

```
--port N      default 8765
--host ADDR   default 127.0.0.1
--no-open     do not open a browser
--reload      restart when the Python source changes
```

### Working on the tool

`--reload` restarts the server whenever a `.py` file in the package changes, so a
backend change is live in about a second instead of after remembering to restart —
which is the sort of thing you only remember after debugging the old code for a
while.

```bash
uv run subtitle-search ~/study/P01 --reload
```

It logs each reload, deliberately: a reload you cannot see happening is worse than
none. The browser tab is opened once by the parent process, so a reload does not
keep opening new ones.

**Only this package is watched.** Your recording folder is not, on purpose —
quotes and word timings are written into it constantly, and saving a quote should
not restart the server that just saved it. Frontend files need no reload at all:
the HTML, CSS and JS are read from disk per request, so a browser refresh is
enough.

Reloading re-imports the app in a fresh process, which means it cannot be handed
an app that is already built. The folder therefore travels in
`SUBTITLE_SEARCH_FOLDER` and the child process builds its own registry from it —
so every reload also re-reads the folder, and a transcript corrected outside the
tool shows up. `watchfiles` in the `dev` extra makes the watching event-based;
without it uvicorn polls instead and reload still works.

## A library of recordings

Point it at a folder of recording folders and it opens as a library instead of a
single reader:

```bash
subtitle-search ~/study/            # a folder of participant folders
subtitle-search ~/study/P01/        # a single recording
```

Either way the home page is the library, and a recording opens from there. One
entry point beats a home page that changes shape depending on how many folders it
found.

There is nothing to configure: a folder holding a transcript *is* a recording, so
a folder that holds none is read as a library of the folders beneath it (two
levels deep, which is how these arrive). A folder whose transcript cannot be
parsed is reported and skipped rather than taking the whole library down.

The library lists every recording with its duration, speakers, quote count,
text codes and video codes, following the mode switch, and searches every
transcript at once. Results link straight to the moment
in the reader.

## Sharing a study through Google Drive

The tool itself never sends anything anywhere. But everything it saves is a plain
file in the recording folder, so if that folder is synced by **Google Drive for
desktop**, the sync client uploads each save and a collaborator sees it. Each
person runs this fork on their own machine, pointed at their synced copy of the
same study folder. There is no server to host.

**Check your approvals first.** Syncing puts the recordings and transcripts on
Google's servers. Make sure your IRB protocol or data management plan allows
that, and use the Google account it covers (usually the institutional one). If
Drive is not allowed, the steps are the same for any synced folder that is,
such as Box.

### One-time setup, for each person

1. **Install this fork.**

   ```bash
   git clone https://github.com/shaily99/interview-analysis.git
   cd interview-analysis
   uv sync
   ```

   Later, `git pull` picks up changes to the fork.

2. **Install Google Drive for desktop.** Download it from
   <https://www.google.com/drive/download/> or run `brew install --cask google-drive`,
   then sign in. On Apple silicon Macs, approve the system extension under
   **System Settings → Privacy & Security** if asked.

3. **Get the study folder.** The owner shares the study folder (for example
   `pilots/`) with each collaborator, with **Editor** access. A collaborator who
   has it under *Shared with me* adds it to their own drive with
   **Organize → Add shortcut → My Drive** in the web view, so it appears on
   disk.

4. **Keep it on disk.** In Finder, right-click the study folder and choose
   **Available offline**. By default Drive streams files on demand, which is fine
   for small JSON files but not for scrubbing through an 80 MB video.

### Folder layout

```
pilots/                             ← point the tool here
  coders/<id>/                      ← one folder per coder, written only by that coder
    coder.json                      ← name and initials
    text_codebook.json, video_codebook.json, themes.json, history.json
    common/                         ← that coder's copy of the common files
  common/                           ← shared common codes and themes, written on Refresh
  P'1/
    GMT…_Recording.mp4
    GMT…_Recording.transcript.vtt   ← each folder needs a .vtt to open
    coders/<id>/quotes.json, video_codes.json, common/
    common/                         ← shared common quotes, spans and returned items
    session.words.json              ← measured word timings
    audio…_original.vtt             ← backup written on the first correction
```

Folders without a `.vtt` are skipped, and Google Docs shortcuts (`.gdoc`) are
ignored. When you move a recording in, bring its whole folder, and keep the
top-level `coders/` and `common/` at the top of the study folder.

### Running it

Drive mounts under `~/Library/CloudStorage/`. Quote the path, because it has
spaces and participant folders often have apostrophes:

```bash
uv run subtitle-search "$HOME/Library/CloudStorage/GoogleDrive-<you@example.edu>/My Drive/<path>/pilots"
```

Point it at the **study folder**, not a participant folder. Opened alone, a
participant folder has its own coders and codebooks.

To see what a collaborator saved, wait for Drive to finish syncing, then press
↻ Refresh.

### Working at the same time

- Coding is safe in parallel: each coder writes only their own `coders/<id>/`
  files, and common changes go to your copy first.
- Transcript corrections are shared. Two people correcting the same recording
  at once can conflict; take turns.
- If a file like `… (1).json` appears, the sync client kept two versions. The
  header warns about those beside the common files. Compare them by hand before deleting either.

## Themes: analysis across recordings

`/themes` groups codes into themes, in several views. A theme holds codes, text
and video; their quotes and spans come along as evidence. A code can be in
several themes. The canvas and the board show the same themes.

- **Your themes** hold your codes and common codes. Collaborative mode adds
  other coders' themes, read-only.
- **Common themes** (✓) hold only common codes and anyone can edit them. ✓ on
  your theme moves it to common, or merges it into a common theme, once all its
  codes are common; until then it lists the codes to move first.
- Moving, merging, deleting or returning a code updates the themes that hold it.

### Canvas — affinity diagramming on a plane

Code cards on a surface that pans and zooms. Areas are themes. Where things sit
is up to you: two areas side by side, or a card parked between them, is part of
the analysis.

- **New area** makes a theme in the middle of the view.
- The **tray** on the left lists codes not yet on the canvas. Drag one onto the
  plane. A code card shows name, colour, ✓ or initials, and count; ▶ marks a
  video code. Open a card to read its quotes and spans, by speaker then time;
  **↗** opens each in the reader.
- Drag an area **by the strip across the top of its title bar** and its cards
  move with it.
- **Resize** from the bottom-right corner; cards outside the new box are pulled
  back inside.
- **▾** rolls an area up to its title, note and count. **Roll up all** does
  every one of your own areas.
- **⊞ tidy** packs an area's cards alphabetically and saves the layout.
- Drag a card **back to the tray** to remove that card, or onto **bare canvas**
  to park it outside any theme.
- **✕** deletes an area. For your own themes it offers an Undo that restores
  the box, note and cards.

### Theme names do not shrink

An area's title bar stays the same size at any zoom. On a narrow bar the note and
recording spread go first, then the count, then the ▶ ⊞ ✕ buttons. A long name
wraps, and shrinks (never below 9px) only when one word is wider than the area.

### Grid, which changes nothing

**Grid** draws every area's cards packed alphabetically and **saves nothing**.
Turning it off restores the free layout. `⊞ tidy` saves the same order. In grid
view a drag inside one area does nothing; a drag between areas still moves a
card.

### Filters

The tray narrows by text (name and description), kind (text or ▶ video, used
or not yet used), recording, colour, and whether the code has a description. It
orders by recording, by count, or by how many recordings a code appears in.
Filtering also highlights matching cards already on the plane.

**A code can be on the canvas in two areas.** Alt-drag (or **Also place in…**)
leaves the card where it is and adds a second card elsewhere.

Every **↗** opens the transcript in a **new tab**, keeping your place here.

Without a mouse: tab to a code in the tray and press enter to place it; tab to a
card and the panel under the tray moves it between themes, arrow keys nudge it
(shift for fine), delete removes it. Drag the background to pan, ctrl- or
⌘-scroll to zoom, `0` to frame everything.

Rolled-up areas are saved in the themes file. Zoom, pan and grid view stay in
the browser.

### Board — columns, for when there are few enough to see

The same themes as columns. Drag a card, or use its menu to move it between your
themes and common themes. Choosing a theme on the board moves the card out of
its other themes; use the canvas to put a code in two. Each column shows how
many recordings it draws on.

### Matrix — for when they do

Text codes down the side, recordings across the top, counts in the cells. It
follows the mode switch; in collaborative mode same-named codes of different
coders are separate rows, labelled with initials. Rows are
sorted by how many recordings share the code, so the findings float to the top and
the singletons sink. Cell weight is ink, not a colour ramp, so a row reads at a
glance without matching swatches to a legend.

Click a code for every quote carrying it; click a cell for one participant's.

### Pairs — for when the codebook has drifted

Text codes that share a quote, strongest first. A pair counts only within one
coder's quotes, or within common quotes. Two codes that always arrive together
are usually one code wearing two names, or a cause and its effect. It is the
cheapest signal that a codebook needs consolidating.

### Map — for arguing with your codebook

```bash
pip install -e '.[analysis]'          # the map, graph and signals
pip install -e '.[analysis,neural]'   # + understands paraphrase
```

Every quote placed by what it *says*, not by how you coded it. The clusters
here are formed by the language, so they can disagree with your themes — and
where they do is either a theme you missed or a distinction you decided not to
make. Drag a loop around a group to make a theme from those quotes' codes.

Colour by theme (the first theme holding any of a quote's codes), by the clusters the language forms, or by recording.
Click any point for the quote and its nearest neighbours in meaning, and click
through those to walk the corpus by similarity rather than by code.

Two backends. **Word overlap** is the default: instant, local, no download, and
honestly limited — it cannot tell that *"it never works"* and *"constantly
broken"* are the same complaint. The **language model** can, and the difference
is stark; on the same pair of quotes about consent and cloud storage, word
overlap scores `0.00` and the model scores `0.34`.

The model is the one thing in this tool that touches the network: it downloads
once, then lives on your machine, and vectors are cached in
`library.embeddings.npz` so it is paid for once. **Your quotes are never sent
anywhere** — encoding happens in this process.

### Graph — the shape of the codebook

Text codes as a network, pulled together by the quotes they share. The same numbers as
Pairs, arranged so you can see what clumps, what dangles off the side, and what
sits on its own.

### Signals — is the study finished?

The grounded-theory question, drawn as the curve it actually is: cumulative
distinct text codes against interviews, in the order they were recorded. A curve still
climbing at the last participant is the study saying it is not done. It measures
the codebook rather than the world, so a flat curve can equally mean you stopped
noticing — worth reading as a prompt, not a verdict.

Underneath: the quotes least like anything else. Negative cases are where a
theme's real boundary is, and they are the easiest thing to lose because nothing
groups them.

**Nothing here files anything.** Every cluster and every ranking is a proposal;
a person accepts it or does not.

### Listening to a theme

Any theme, code, or cell can be played straight through: each quote or span in
turn, in the recording it came from, stopping at its own end. Tone is half of what a quote
means and it does not survive being written down — being able to hear a theme
rather than only read it is the reason the recordings are still attached.

Your themes are stored in `<study>/coders/<you>/themes.json`; common themes in
`<study>/common/themes.json` and `theme_cards.json`. A theme holds code
references, never copies.

## Anonymizing a set of interviews

`scripts/anonymize_zoom.py` takes a directory of downloaded Zoom folders and
writes anonymized copies. Each folder under the input path is one participant,
and **the folder's name is that participant's ID**.

```bash
python scripts/anonymize_zoom.py raw/ anonymized/ --dry-run   # look first
python scripts/anonymize_zoom.py raw/ anonymized/
```

Media is copied verbatim. Transcripts are rewritten with `Sireesh Gururaja` and
`Jordan Taylor` replaced by `interviewer`, and the participant replaced by the
folder name. Names found in *filenames* are replaced the same way.

Names come **only from speaker labels**. Nothing else is treated as a name, so
places, employers, and products survive untouched for a human pass to judge.
Replacement covers both the speaker attribution and the same names spoken in the
body of the transcript.

### Who gets which label

Every decision is made from one folder's own transcripts — nothing is inferred by
comparing folders — so a folder anonymizes the same way whether it runs alone or
alongside fifty others. Within a folder, speakers are labeled by the order they
first speak:

```
first speaker    -> interviewer 1
second speaker   -> the participant (the folder name)
third onwards    -> interviewer 2, interviewer 3, ...
```

That ordering assumes an interviewer opens. When it is wrong — the participant
spoke first, or a colleague joined before them — name the interviewers:

```bash
python scripts/anonymize_zoom.py raw/ anonymized/ --interviewer "Ada Lovelace"
```

Named speakers are interviewers wherever they fall, in every folder, and the
participant becomes the first speaker not named. Someone named but never speaking
is still replaced if they are mentioned aloud. Because labels are per-folder,
`interviewer 2` is not necessarily the same person across folders unless you name
them.

The dry run prints the mapping for every folder before anything is written, which
is the place to catch a folder where the order was guessed wrong.

### Leaving folders out

```bash
python scripts/anonymize_zoom.py raw/ anonymized/ --skip-single-speaker
```

A transcript with only one voice usually means the recording captured one side of
the call. The ordering rule would label that lone person the participant, which
is as likely to be the interviewer talking to themselves — so this leaves those
folders out entirely.

Labels are merged before counting, so a participant who rejoins as `Alex` after
being `Alex Chen` is one speaker, not two. An interrupted session is counted
across all its transcripts together, so a folder where each part has one speaker
but the parts differ is kept.

Excluding is not a failure: those folders are listed separately and the exit
status stays zero.

### What it refuses, and what it only reports

A folder produces **no output at all** rather than a partial result. Nothing is
written until every file in it has passed, so a refusal never leaves
half-anonymized files behind. A folder is refused when it has no transcript, when
its output folder already has files (use `--force`), or when every speaker was
named as an interviewer so no participant is left.

Everything else is reported under `REVIEW` and still written, since deciding it
needs a reader:

- a line that still looks like `Somebody: text` where `Somebody` was never
  detected as a speaker. Detection is deliberately conservative, so this catches
  both a faint name and an ordinary line like `Correction:`
- a first name that is also a common word

The exit status is non-zero if any folder was refused. Other folders still
process, so one bad folder does not stop the run.

### What never reaches the output

Chat logs, `_original.vtt` transcript backups, and any file type the script does
not specifically handle. Backups matter here: they hold the transcript as it
arrived, so copying one would reintroduce every name the pass just removed.
Everything skipped is reported by name, so nothing disappears quietly.

### Left for your pass

This is a first sweep over transcripts you are going to read anyway, not a
substitute for reading them.

Given names that are also ordinary words (`Mark`, `Summer`, `Rose`) are **not**
replaced when they appear alone, because doing so would corrupt normal sentences
— "please mark that down". The full name is still replaced, and the script prints
which single names it left behind so your own pass knows where to look.

Zoom display names are pulled apart before matching, so
`B.F. (Jim) Lightning, Brown U. USA` contributes `B.F. Lightning`, `Lightning`
and the nickname `Jim` as names to replace — but not `Brown` or `USA`, which are
affiliation and would otherwise be rewritten wherever those words appeared.
Pronoun tags like `(she/her)` are recognized as tags, not nicknames.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests run against synthetic VTT fixtures in `tests/fixtures.py`, including the
mid-sentence-colon trap, CRLF endings, voice tags, speakerless transcripts, and
byte-exact HTTP Range serving.
