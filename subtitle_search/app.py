"""FastAPI application.

Routes are namespaced by recording id from the start, even though single-folder
mode registers exactly one. That keeps library mode an addition rather than a
migration.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .editing import (
    EditError,
    apply_cue_edit,
    apply_cue_merge,
    apply_cue_split,
    apply_roster_edit,
    apply_selection_speaker,
    apply_speaker_edit,
)
from .alignment import AlignmentError
from .alignment import available as alignment_available
from .codebook import CodebookError
from .common import COMMON, ClashError, _record_history, claim_returns, history_for, move_code, return_code
from .coders import CoderError
from .highlights import COLORS, HighlightError
from .library import (
    ThemeStore,
    all_quotes,
    cooccurrence,
    metrics as canvas_metrics,
    MODES,
    code_items,
    code_packing_order,
    tag_index,
    untagged,
    video_index,
    vocabulary,
)
from .semantics import (
    EMBEDDINGS_FILENAME,
    Semantics,
    SemanticsUnavailable,
    VectorCache,
    neural_available,
    saturation,
)
from .media import serve_media
from .search import regex_search, search
from .session import Recording, RecordingRegistry
from .timings import coverage, unmeasured
from .video_codes import (
    COLORS as VIDEO_CODE_COLORS,
    RESERVED_KEYS as VIDEO_CODE_RESERVED_KEYS,
    VideoCodeError,
    delete_code,
    merge_code,
    usage_counts,
)

STATIC_DIR = Path(__file__).parent / "static"

#: The HTML entry points. Held to the same revalidation rule as the scripts, so a
#: page and its modules can never come from two different versions of the tool.
PAGES = {"/", "/reader", "/themes", "/code", "/codebook"}

#: Tells "the client said nothing about where this card came from" apart from
#: "it came from the bare canvas", which is a real answer and arrives as null.
_MOVED_ABSENT = object()


#: Where the reloading server leaves the folder it was pointed at.
#:
#: Reload works by re-importing the app in a fresh process, which means the app
#: cannot be handed to uvicorn already built -- it has to be buildable from
#: nothing but an import string. So the one piece of runtime configuration
#: travels in the environment instead of as an argument.
FOLDER_ENV = "SUBTITLE_SEARCH_FOLDER"


def from_environment() -> FastAPI:
    """Build the app from the environment. The entry point uvicorn reloads.

    Every reload re-reads the folder from disk, so a transcript corrected outside
    the tool -- or a quotes file written by another copy of it -- is picked up.
    """
    folder = os.environ.get(FOLDER_ENV)
    if not folder:
        raise RuntimeError(
            f"{FOLDER_ENV} is not set; start the server with `subtitle-search <folder>`"
        )
    registry = RecordingRegistry()
    registry.add_library(Path(folder))
    return create_app(registry)


def create_app(registry: RecordingRegistry) -> FastAPI:
    app = FastAPI(title="subtitle-search", docs_url=None, redoc_url=None)
    app.state.registry = registry
    #: When the folder was last read: at startup, then on each Refresh.
    synced = {"at": datetime.now(timezone.utc).isoformat()}

    def require(recording_id: str) -> Recording:
        recording = registry.get(recording_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="unknown recording")
        return recording

    def current_coder(request: Request) -> str:
        """Who is writing. Reads need nobody; every write names a known coder."""
        coder_id = request.headers.get("x-coder", "")
        if not coder_id or registry.coders.get(coder_id) is None:
            raise HTTPException(status_code=401, detail="choose who is coding first")
        return coder_id

    @app.get("/api/config")
    def get_config() -> dict:
        default = registry.default
        return {
            "recordings": [r.summary() for r in registry.list()],
            "default_recording_id": default.id if default else None,
            "colors": list(COLORS),
            "legacy_files": registry.legacy_files,
        }

    # -- coders ------------------------------------------------------------

    @app.get("/api/coders")
    def list_coders() -> dict:
        return {"coders": registry.coders.list()}

    @app.get("/api/coders/suggest")
    def suggest_initials(name: str = Query(""), coder: str = Query("")) -> dict:
        return {"initials": registry.coders.suggest(name, exclude=coder or None)}

    @app.post("/api/coders", status_code=201)
    def create_coder(payload: dict = Body(...)) -> dict:
        try:
            coder = registry.coders.create(payload.get("name"), payload.get("initials"))
        except CoderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"coder": coder}

    @app.patch("/api/coders/{coder_id}")
    def update_coder(coder_id: str, request: Request, payload: dict = Body(...)) -> dict:
        if current_coder(request) != coder_id:
            raise HTTPException(status_code=403, detail="you can only change your own name and initials")
        try:
            coder = registry.coders.update(coder_id, payload)
        except CoderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"coder": coder}

    @app.post("/api/refresh")
    def refresh(request: Request) -> dict:
        """Re-read what collaborators have synced in, take what was returned to you,
        and write the combined common records into the shared files."""
        registry.refresh()
        coder = request.headers.get("x-coder", "")
        claimed = claim_returns(registry, coder) if coder and registry.coders.get(coder) else 0
        registry.push_common()
        synced["at"] = datetime.now(timezone.utc).isoformat()
        return {
            "coders": registry.coders.list(),
            "legacy_files": registry.legacy_files,
            "claimed": claimed,
            "conflicts": registry.common_conflicts(),
        }

    @app.get("/api/recordings/{recording_id}")
    def get_recording(recording_id: str) -> dict:
        return require(recording_id).payload()

    @app.get("/api/recordings/{recording_id}/parts/{part_index}/media")
    def get_part_media(recording_id: str, part_index: int, request: Request):
        recording = require(recording_id)
        path = recording.media_path(part_index)
        if path is None:
            raise HTTPException(status_code=404, detail="no media file for this part")
        return serve_media(path, request.headers.get("range"))

    @app.get("/api/recordings/{recording_id}/media")
    def get_media(recording_id: str, request: Request):
        """The first part's media. Kept so a single-recording folder has a plain URL."""
        return get_part_media(recording_id, 0, request)

    @app.get("/api/recordings/{recording_id}/search")
    def get_search(
        recording_id: str,
        q: str = Query("", description="query text"),
        mode: str = Query("fuzzy", pattern="^(fuzzy|regex)$"),
        limit: int = Query(60, ge=1, le=500),
    ) -> dict:
        recording = require(recording_id)
        if mode == "regex":
            try:
                results = regex_search(recording.transcript, q, limit=limit)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            results = search(recording.transcript, q, limit=limit)
        return {"query": q, "mode": mode, "results": results}

    @app.patch("/api/recordings/{recording_id}/cues/{cue_id}")
    def edit_cue(recording_id: str, cue_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Correct one line of the transcript and save it to the source file."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            return apply_cue_edit(recording, cue_id, payload.get("text", ""))
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc

    @app.put("/api/recordings/{recording_id}/roster")
    def edit_roster(recording_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Set the speakers and their keys, without opening the transcript."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            result = apply_roster_edit(recording, payload.get("speakers") or [])
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Rostering changes what joins, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    #: Captions measured per request. Small enough that the reader can show
    #: progress and stop partway, large enough that the model load amortizes.
    ALIGN_BATCH = 25

    @app.get("/api/recordings/{recording_id}/alignment")
    def alignment_state(recording_id: str) -> dict:
        """Whether word timings can be measured here, and how many exist."""
        recording = require(recording_id)
        ok, reason = alignment_available()
        return {
            "available": ok,
            "reason": reason,
            "coverage": coverage(recording.transcript),
        }

    @app.post("/api/recordings/{recording_id}/align")
    def align(recording_id: str, payload: dict = Body(default={})) -> dict:
        """Measure word timings for some captions, or for the next batch of them.

        Handed out in batches rather than run to completion in one request: the
        reader loops until nothing is left, which gives it progress to show and
        makes stopping halfway keep everything measured so far.
        """
        recording = require(recording_id)
        ok, reason = alignment_available()
        if not ok:
            raise HTTPException(status_code=503, detail=reason)

        requested = payload.get("cue_ids")
        if requested:
            targets = [c for c in (recording.transcript.cue(i) for i in requested) if c]
        else:
            limit = max(1, min(int(payload.get("limit") or ALIGN_BATCH), 200))
            targets = unmeasured(recording.transcript, limit)

        try:
            measured = recording.measure(targets)
        except AlignmentError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the timings: {exc}"
            ) from exc

        return {
            "measured": measured,
            "captions": len(targets),
            "coverage": coverage(recording.transcript),
            "remaining": len(unmeasured(recording.transcript, 10_000)),
        }

    @app.post("/api/recordings/{recording_id}/cues/{cue_id}/split")
    def split_cue(recording_id: str, cue_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Cut one caption in two, so two speakers in one block can be separated."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            result = apply_cue_split(
                recording,
                cue_id,
                payload.get("offset", 0),
                payload.get("text"),
                align=bool(payload.get("align", True)),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # A split renumbers every later cue, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.post("/api/recordings/{recording_id}/cues/{cue_id}/merge")
    def merge_cues(recording_id: str, cue_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Join a run of captions into one -- undoing a split, or repairing Zoom's."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            result = apply_cue_merge(
                recording,
                cue_id,
                payload.get("through") or cue_id,
                payload.get("expect"),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # A join renumbers every later cue, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.post("/api/recordings/{recording_id}/selection/speaker")
    def reattribute_selection(recording_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Hand a selected passage to another speaker, cutting captions to fit it."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            result = apply_selection_speaker(
                recording,
                payload.get("start_cue_id", ""),
                int(payload.get("start_char_offset") or 0),
                payload.get("end_cue_id", ""),
                int(payload.get("end_char_offset") or 0),
                payload.get("speaker", ""),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Cutting and regrouping changes every block, so the reader takes it back.
        return {**result, "recording": recording.payload()}

    @app.patch("/api/recordings/{recording_id}/cues/{cue_id}/speaker")
    def edit_speaker(recording_id: str, cue_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Reattribute a line, or a run of them, to a different speaker."""
        recording = require(recording_id)
        # Captions are shared; the corrector is named, and common quotes it moves
        # are written to their own copy of common.
        recording.store.acting = current_coder(request)
        try:
            result = apply_speaker_edit(
                recording, cue_id, payload.get("speaker", ""), payload.get("through")
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Regrouping changes every block, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.get("/api/recordings/{recording_id}/highlights")
    def list_highlights(recording_id: str) -> dict:
        recording = require(recording_id)
        return {"highlights": recording.store.list(), "stale": recording.store.stale}

    @app.post("/api/recordings/{recording_id}/highlights", status_code=201)
    def create_highlight(recording_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        recording = require(recording_id)
        try:
            highlight = recording.store.create(coder, payload)
        except HighlightError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"highlight": highlight}

    @app.patch("/api/recordings/{recording_id}/highlights/{highlight_id}")
    def update_highlight(recording_id: str, highlight_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        recording = require(recording_id)
        try:
            highlight = recording.store.update(coder, highlight_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown highlight") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except HighlightError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"highlight": highlight}

    @app.delete("/api/recordings/{recording_id}/highlights/{highlight_id}")
    def delete_highlight(recording_id: str, highlight_id: str, request: Request) -> JSONResponse:
        coder = current_coder(request)
        recording = require(recording_id)
        try:
            deleted = recording.store.delete(coder, highlight_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown highlight") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="unknown highlight")
        return JSONResponse({"deleted": highlight_id})

    # -- codebooks: each coder's text codes and video codes -----------------

    def owner_of(pairs, code_id: str) -> str:
        """The coder a code belongs to, or 404."""
        owner = next((coder for coder, code in pairs if code["id"] == code_id), None)
        if owner is None:
            raise HTTPException(status_code=404, detail="unknown code")
        return owner

    def own_code(request: Request, pairs, code_id: str) -> str:
        """The caller, if the code is theirs; other coders' codes are read-only."""
        coder = current_coder(request)
        if owner_of(pairs, code_id) != coder:
            raise HTTPException(status_code=403, detail="that code belongs to another coder")
        return coder

    def drop_from_themes(coder: str, owner: str, ref: str) -> None:
        """A deleted code leaves the themes it was in: its owner's, or the common ones."""
        if owner == COMMON:
            registry.common_themes.writer = coder
            registry.common_themes.remove_ref(ref)
        else:
            registry.theme_store(coder).remove_ref(ref)

    def editor_of(request: Request, pairs, code_id: str) -> tuple[str, str]:
        """The caller and the code's owner, if the caller may edit it: their own, or a common one."""
        coder = current_coder(request)
        owner = owner_of(pairs, code_id)
        if owner not in (coder, COMMON):
            raise HTTPException(status_code=403, detail="that code belongs to another coder")
        return coder, owner

    def text_counts() -> dict[str, int]:
        counts: dict[str, int] = {}
        for recording in registry.list():
            for highlight in recording.store.list():
                for code_id in highlight.get("codes") or []:
                    counts[code_id] = counts.get(code_id, 0) + 1
        return counts

    def text_codebook_state() -> dict:
        counts = text_counts()
        return {
            "codes": [
                {**code, "coder": coder, "quote_count": counts.get(code["id"], 0)}
                for coder, code in registry.books.all_text()
            ],
            "colors": list(VIDEO_CODE_COLORS),
        }

    @app.get("/api/library/text-codebook")
    def get_text_codebook() -> dict:
        return text_codebook_state()

    @app.post("/api/library/text-codebook", status_code=201)
    def add_text_code(request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        try:
            code = registry.books.text(coder).add(payload)
        except CodebookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"code": {**code, "coder": coder}, **text_codebook_state()}

    def record_edit(coder: str, kind: str, before: dict, after: dict) -> None:
        """One history entry for an edit of a common code, naming the fields it changed."""
        changes = [f for f in ("name", "color", "description") if before.get(f) != after.get(f)]
        if changes:
            _record_history(registry, coder, {"action": "edited", "kind": kind, "code_id": after["id"], "name": after["name"],
                                              "changes": changes, **({"from": before["name"]} if "name" in changes else {})})

    @app.patch("/api/library/text-codebook/{code_id}")
    def update_text_code(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder, owner = editor_of(request, registry.books.all_text(), code_id)
        try:
            if owner == COMMON:
                before = registry.books.common_text.get(code_id)
                code = registry.books.common_text.update(code_id, payload, coder=coder)
                record_edit(coder, "text", before, code)
            else:
                code = registry.books.text(coder).update(code_id, payload)
        except CodebookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"code": {**code, "coder": owner}, **text_codebook_state()}

    @app.delete("/api/library/text-codebook/{code_id}")
    def delete_text_code(code_id: str, request: Request) -> dict:
        coder, owner = editor_of(request, registry.books.all_text(), code_id)
        used = text_counts().get(code_id, 0)
        if used:
            raise HTTPException(
                status_code=400,
                detail=f"{used} quote{'s' if used != 1 else ''} still use this code -- remove it from them or merge it into another",
            )
        if owner == COMMON:
            name = registry.books.common_text.get(code_id)["name"]
            registry.books.common_text.remove(code_id, coder=coder)
            _record_history(registry, coder, {"action": "deleted", "kind": "text", "code_id": code_id, "name": name})
        else:
            registry.books.text(coder).remove(code_id)
        drop_from_themes(coder, owner, f"text:{code_id}")
        return {"deleted": code_id, **text_codebook_state()}

    @app.post("/api/library/text-codebook/{code_id}/merge")
    def merge_text_code(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = own_code(request, registry.books.all_text(), code_id)
        into = str(payload.get("into") or "")
        book = registry.books.text(coder)
        if into == code_id:
            raise HTTPException(status_code=400, detail="a code cannot be merged into itself")
        if book.get(into) is None:
            raise HTTPException(status_code=400, detail="you can only merge into another of your own codes")
        moved = sum(r.store.reassign_code(code_id, into) for r in registry.list())
        book.remove(code_id)
        registry.theme_store(coder).replace_ref(f"text:{code_id}", f"text:{into}")
        return {"merged": code_id, "moved": moved, **text_codebook_state()}

    # -- every use of a code, across the study -----------------------------

    def split_ref(ref: str) -> tuple[Recording, str]:
        recording_id, _, item_id = str(ref).partition(":")
        return require(recording_id), item_id

    @app.get("/api/library/text-codebook/{code_id}/applications")
    def text_code_applications(code_id: str) -> dict:
        owner_of(registry.books.all_text(), code_id)
        return {
            "applications": [
                {**q, "ref": f"{r.id}:{q['id']}", "recording_id": r.id, "recording_title": r.title}
                for r in registry.list()
                for q in r.store.list()
                if code_id in (q.get("codes") or [])
            ]
        }

    @app.post("/api/library/text-codebook/{code_id}/applications/remove")
    def remove_text_code_applications(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Take the code off the chosen quotes. The quotes stay, with anything else on them."""
        coder, owner = editor_of(request, registry.books.all_text(), code_id)
        removed = 0
        for ref in payload.get("refs") or []:
            recording, quote_id = split_ref(ref)
            if owner == COMMON:
                removed += int(bool(recording.common_quotes.remove_code(coder, quote_id, code_id)))
                continue
            quote = next((q for q in recording.store.list() if q["id"] == quote_id), None)
            if quote is None or code_id not in (quote.get("codes") or []):
                continue
            recording.store.update(coder, quote_id, {"codes": [c for c in quote["codes"] if c != code_id]})
            removed += 1
        if owner == COMMON and removed:
            _record_history(registry, coder, {"action": "removed", "kind": "text", "code_id": code_id,
                                              "name": registry.books.common_text.get(code_id)["name"], "removed": removed})
        return {"removed": removed, **text_codebook_state()}

    @app.get("/api/library/video-codebook/{code_id}/applications")
    def video_code_applications(code_id: str) -> dict:
        owner_of(registry.books.all_video(), code_id)
        return {
            "applications": [
                {**s, "ref": f"{r.id}:{s['id']}", "recording_id": r.id, "recording_title": r.title}
                for r in registry.list()
                for s in r.video_codes.list()
                if s.get("code_id") == code_id
            ]
        }

    @app.post("/api/library/video-codebook/{code_id}/applications/remove")
    def remove_video_code_applications(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        """Delete the chosen spans. A span carries one code, so removing the code removes the span."""
        coder, owner = editor_of(request, registry.books.all_video(), code_id)
        removed = 0
        for ref in payload.get("refs") or []:
            recording, span_id = split_ref(ref)
            if owner == COMMON:
                record = recording.common_spans.get(span_id)
                if record and record["code_id"] == code_id:
                    recording.common_spans.remove(coder, span_id)
                    removed += 1
                continue
            span = next((s for s in recording.video_codes.list() if s["id"] == span_id), None)
            if span is None or span.get("code_id") != code_id:
                continue
            recording.video_codes.remove(coder, span_id)
            removed += 1
        if owner == COMMON and removed:
            _record_history(registry, coder, {"action": "removed", "kind": "video", "code_id": code_id,
                                              "name": registry.books.common_video.get(code_id)["name"], "removed": removed})
        return {"removed": removed, **video_codebook_state()}

    # -- common codes: moving agreed codes in, and giving them back -------------

    def pairs_for(kind: str):
        return registry.books.all_text() if kind == "text" else registry.books.all_video()

    def state_for(kind: str) -> dict:
        return text_codebook_state() if kind == "text" else video_codebook_state()

    def move(kind: str, code_id: str, request: Request, payload: dict, dry_run: bool) -> dict:
        coder = own_code(request, pairs_for(kind), code_id)
        try:
            summary = move_code(
                registry, kind, coder, code_id,
                final=bool(payload.get("final")),
                description=payload.get("description"),
                name=payload.get("name") or None,
                into=payload.get("into") or None,
                dry_run=dry_run,
            )
        except ClashError as exc:
            return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)
        except CodebookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**summary, **({} if dry_run else state_for(kind))}

    def give_back(kind: str, code_id: str, request: Request) -> dict:
        coder = current_coder(request)
        if owner_of(pairs_for(kind), code_id) != COMMON:
            raise HTTPException(status_code=400, detail="only a common code can be returned to its coders")
        try:
            return {**return_code(registry, kind, coder, code_id), **state_for(kind)}
        except CodebookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/library/text-codebook/{code_id}/move")
    def move_text_code(code_id: str, request: Request, dry_run: bool = Query(False), payload: dict = Body(...)):
        return move("text", code_id, request, payload, dry_run)

    @app.post("/api/library/video-codebook/{code_id}/move")
    def move_video_code(code_id: str, request: Request, dry_run: bool = Query(False), payload: dict = Body(...)):
        return move("video", code_id, request, payload, dry_run)

    @app.post("/api/library/text-codebook/{code_id}/return")
    def return_text_code(code_id: str, request: Request) -> dict:
        return give_back("text", code_id, request)

    @app.post("/api/library/video-codebook/{code_id}/return")
    def return_video_code(code_id: str, request: Request) -> dict:
        return give_back("video", code_id, request)

    @app.get("/api/library/text-codebook/{code_id}/history")
    def text_code_history(code_id: str) -> dict:
        return {"entries": history_for(registry, "text", code_id)}

    @app.get("/api/library/video-codebook/{code_id}/history")
    def video_code_history(code_id: str) -> dict:
        return {"entries": history_for(registry, "video", code_id)}

    @app.get("/api/common/status")
    def common_status() -> dict:
        """When the folder was last read, and any sync conflicts."""
        return {"synced_at": synced["at"], "conflicts": registry.common_conflicts()}

    # -- video codes: spans of time, separate from quotes and their codes --

    def video_codebook_state() -> dict:
        counts = usage_counts(registry)
        return {
            "codes": [
                {**code, "coder": coder, "span_count": counts.get(code["id"], 0)}
                for coder, code in registry.books.all_video()
            ],
            "colors": list(VIDEO_CODE_COLORS),
            "reserved_keys": sorted(VIDEO_CODE_RESERVED_KEYS),
        }

    @app.get("/api/library/video-codebook")
    def get_video_codebook() -> dict:
        return video_codebook_state()

    @app.post("/api/library/video-codebook", status_code=201)
    def add_video_code(request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        try:
            code = registry.books.video(coder).add(payload)
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"code": {**code, "coder": coder}, **video_codebook_state()}

    @app.patch("/api/library/video-codebook/{code_id}")
    def update_video_code(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder, owner = editor_of(request, registry.books.all_video(), code_id)
        try:
            if owner == COMMON:
                before = registry.books.common_video.get(code_id)
                code = registry.books.common_video.update(code_id, payload, coder=coder)
                record_edit(coder, "video", before, code)
            else:
                code = registry.books.video(coder).update(code_id, payload)
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"code": {**code, "coder": owner}, **video_codebook_state()}

    @app.delete("/api/library/video-codebook/{code_id}")
    def delete_video_code(code_id: str, request: Request) -> dict:
        coder, owner = editor_of(request, registry.books.all_video(), code_id)
        if owner == COMMON:
            used = usage_counts(registry).get(code_id, 0)
            if used:
                raise HTTPException(status_code=400, detail=f"{used} span{'s' if used != 1 else ''} still use this code -- remove them first")
            name = registry.books.common_video.get(code_id)["name"]
            registry.books.common_video.remove(code_id, coder=coder)
            _record_history(registry, coder, {"action": "deleted", "kind": "video", "code_id": code_id, "name": name})
            drop_from_themes(coder, owner, f"video:{code_id}")
            return {"deleted": code_id, **video_codebook_state()}
        try:
            delete_code(registry, registry.books.video(coder), code_id)
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        drop_from_themes(coder, owner, f"video:{code_id}")
        return {"deleted": code_id, **video_codebook_state()}

    @app.post("/api/library/video-codebook/{code_id}/merge")
    def merge_video_code(code_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = own_code(request, registry.books.all_video(), code_id)
        try:
            moved = merge_code(registry, registry.books.video(coder), code_id, str(payload.get("into") or ""))
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        registry.theme_store(coder).replace_ref(f"video:{code_id}", f"video:{payload.get('into')}")
        return {"merged": code_id, "moved": moved, **video_codebook_state()}

    @app.get("/api/recordings/{recording_id}/video-codes")
    def list_video_codes(recording_id: str) -> dict:
        return {"spans": require(recording_id).video_codes.list()}

    @app.post("/api/recordings/{recording_id}/video-codes", status_code=201)
    def add_video_span(recording_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        recording = require(recording_id)
        try:
            span = recording.video_codes.add(
                coder, payload, registry.books.video(coder), recording.transcript.duration
            )
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"span": span}

    @app.patch("/api/recordings/{recording_id}/video-codes/{span_id}")
    def update_video_span(recording_id: str, span_id: str, request: Request, payload: dict = Body(...)) -> dict:
        coder = current_coder(request)
        recording = require(recording_id)
        try:
            span = recording.video_codes.update(
                coder, span_id, payload, registry.books.video(coder), recording.transcript.duration
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown span") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except VideoCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"span": span}

    @app.delete("/api/recordings/{recording_id}/video-codes/{span_id}")
    def delete_video_span(recording_id: str, span_id: str, request: Request) -> JSONResponse:
        coder = current_coder(request)
        try:
            deleted = require(recording_id).video_codes.remove(coder, span_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown span") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="unknown span")
        return JSONResponse({"deleted": span_id})

    # -- the library, and the themes built across it ----------------------

    def corpus() -> list[dict]:
        """Every coder's quotes, for pruning and packing, which must never be filtered."""
        return all_quotes(registry)

    def view(request: Request) -> tuple[str | None, str | None]:
        """The mode a page asked for, and who is asking. No mode means everything, unlabelled.

        Pages send the mode as ``?mode=`` on reads and as an ``X-Mode`` header on
        every request, so a write can answer with what the page is showing.
        """
        mode = request.query_params.get("mode") or request.headers.get("x-mode")
        return (mode if mode in MODES else None), request.headers.get("x-coder")

    def corpus_for(request: Request) -> list[dict]:
        """The quotes the asking page's mode shows."""
        mode, coder = view(request)
        return all_quotes(registry, mode, coder)

    @app.get("/api/library")
    def get_library(request: Request) -> dict:
        quotes = corpus_for(request)
        return {
            "root": str(registry.root) if registry.root else None,
            "is_library": registry.is_library,
            "recordings": [r.summary() for r in registry.list()],
            "unreadable": [{"folder": name, "reason": why} for name, why in registry.failures],
            "quote_count": len(quotes),
            # Per recording, as the mode shows them: a card should not count
            # quotes the page is not showing.
            "quote_counts": {r.id: sum(1 for q in quotes if q["recording_id"] == r.id) for r in registry.list()},
            "untagged_count": len(untagged(quotes)),
            "tags": tag_index(quotes),
            "cooccurrence": cooccurrence(quotes, minimum=1),
            "video": video_index(registry, *view(request)),
            "colors": list(COLORS),
        }

    @app.get("/api/library/vocabulary")
    def get_vocabulary() -> dict:
        """Every coder's text codes, for picking while you code."""
        return {"tags": vocabulary(registry)}

    @app.get("/api/library/quotes")
    def get_library_quotes(request: Request) -> dict:
        return {"quotes": corpus_for(request)}

    @app.get("/api/library/search")
    def search_library(q: str = Query(""), limit: int = Query(40, ge=1, le=200)) -> dict:
        """Search every transcript at once, newest-scoped results first."""
        results = []
        for recording in registry.list():
            for hit in search(recording.transcript, q, limit=limit):
                results.append({**hit, "recording_id": recording.id, "recording_title": recording.title})
        results.sort(key=lambda r: (0 if r["kind"] == "exact" else 1, -r["score"]))
        return {"query": q, "results": results[:limit]}

    # -- themes: each coder's own, and the common ones -------------------------
    #
    # A theme holds codes. Each coder drafts themes in their own file; agreed
    # themes are common, and hold only common codes. The page is sent every theme
    # its mode shows, each saying whose it is, and a change is made in the store
    # the theme belongs to -- the caller's own, or common, never another coder's.

    @app.get("/api/library/codes")
    def get_codes(request: Request) -> dict:
        """The codes the themes page lays out as cards."""
        return {"codes": code_items(registry, *view(request))}

    def code_refs(owner: str) -> set[str]:
        """What a theme of this owner may hold: common codes, plus the owner's own."""
        refs = {f"text:{c['id']}" for c in registry.books.common_text.list()}
        refs |= {f"video:{c['id']}" for c in registry.books.common_video.list()}
        if owner != COMMON:
            refs |= {f"text:{c['id']}" for c in registry.books.text(owner).list()}
            refs |= {f"video:{c['id']}" for c in registry.books.video(owner).list()}
        return refs

    def codebooks_readable(owner: str) -> bool:
        common = not (registry.books.common_text.incomplete or registry.books.common_video.incomplete)
        if owner == COMMON:
            return common
        return common and not (registry.books.text(owner).unreadable or registry.books.video(owner).unreadable)

    def stores_for(request: Request) -> list[tuple[str, ThemeStore]]:
        """The theme stores the asking page's mode shows, with whose each is."""
        mode, coder = view(request)
        known = registry.coders.get(coder) if coder else None
        stores = []
        if known:
            stores.append((coder, registry.theme_store(coder)))
        stores.append((COMMON, registry.common_themes))
        if mode == "collaborative":
            stores += [(c, registry.theme_store(c, readonly=True)) for c in registry.theme_owners() if c != coder]
        return stores

    def deleted_common_refs() -> set[str]:
        """Common codes known to have been deleted, from their deletion markers.

        Common themes are written by every coder, so they can hold a common code
        whose codebook copy has not synced here yet. Such a theme drops a code
        only once it is known gone; one merely not here yet stays, and is not
        drawn until it arrives.
        """
        refs = set()
        for kind, book in (("text", registry.books.common_text), ("video", registry.books.common_video)):
            refs |= {f"{kind}:{rid}" for rid, r in book._merged.items() if r.get("deleted")}
        return refs

    def themes_state(request: Request) -> dict:
        """Every theme and card the page shows, each labelled with its owner."""
        _, coder = view(request)
        known_coder = bool(coder and registry.coders.get(coder))
        gone = deleted_common_refs()
        themes, cards = [], []
        for owner, store in stores_for(request):
            # Only a store this coder may write is tidied, and only while what it
            # depends on could be read. Your own themes hold only codes your own
            # files already have, so any other code in them is gone; common
            # themes drop only codes known to be deleted.
            if known_coder and owner in (coder, COMMON) and not store.unreadable and codebooks_readable(owner):
                if owner == coder:
                    store.prune(code_refs(owner))
                else:
                    present = {c["ref"] for c in store.cards()} | {r for t in store.list() for r in t.get("refs", [])}
                    if present & gone:
                        store.writer = coder
                        store.prune(present - gone)
            themes += [{**t, "coder": owner} for t in store.list()]
            cards += [{**c, "coder": owner} for c in store.cards()]
        return {
            "themes": themes,
            "cards": cards,
            "placed": sorted({r for t in themes for r in t.get("refs", [])}),
            "on_canvas": sorted({c["ref"] for c in cards}),
            "metrics": canvas_metrics(),
        }

    def store_of(request: Request, theme_id: str | None) -> tuple[str, ThemeStore]:
        """The store a theme lives in, if the caller may change it; loose cards are the caller's."""
        coder = current_coder(request)
        if theme_id is None:
            return coder, registry.theme_store(coder)
        if registry.theme_store(coder)._find(theme_id):
            return coder, registry.theme_store(coder)
        if registry.common_themes._find(theme_id):
            registry.common_themes.writer = coder
            return COMMON, registry.common_themes
        if any(registry.theme_store(c, readonly=True)._find(theme_id) for c in registry.theme_owners()):
            raise HTTPException(status_code=403, detail="that theme belongs to another coder")
        raise HTTPException(status_code=404, detail="unknown theme")

    def check_ref(ref: str, owner: str) -> None:
        if ref not in code_refs(owner):
            raise HTTPException(
                status_code=400,
                detail="a common theme holds only common codes" if owner == COMMON
                else "a theme holds your own codes and common ones",
            )

    @app.get("/api/library/themes")
    def get_themes(request: Request) -> dict:
        return themes_state(request)

    @app.post("/api/library/themes", status_code=201)
    def create_theme(request: Request, payload: dict = Body(default={})) -> dict:
        coder = current_coder(request)
        cards = payload.get("cards")
        for card in cards or []:
            check_ref(str(card.get("ref")), coder)
        theme = registry.theme_store(coder).create(
            payload.get("title", ""), payload.get("color"), payload.get("box"), payload.get("note", ""), cards,
        )
        return {"theme": {**theme, "coder": coder}, **themes_state(request)}

    @app.patch("/api/library/themes/{theme_id}")
    def update_theme(theme_id: str, request: Request, payload: dict = Body(...)) -> dict:
        owner, store = store_of(request, theme_id)
        return {"theme": {**store.update(theme_id, payload), "coder": owner}}

    @app.delete("/api/library/themes/{theme_id}")
    def delete_theme(theme_id: str, request: Request) -> JSONResponse:
        _, store = store_of(request, theme_id)
        store.delete(theme_id)
        return JSONResponse({"deleted": theme_id})

    @app.post("/api/library/themes/assign")
    def assign_code(request: Request, payload: dict = Body(...)) -> dict:
        ref = payload.get("ref")
        if not ref:
            raise HTTPException(status_code=400, detail="a code reference is required")
        owner, store = store_of(request, payload.get("theme_id") or None)
        check_ref(str(ref), owner)
        store.assign(str(ref), payload.get("theme_id"), payload.get("index"))
        return themes_state(request)

    @app.post("/api/library/themes/order")
    def reorder_themes(request: Request, payload: dict = Body(...)) -> dict:
        registry.theme_store(current_coder(request)).reorder(list(payload.get("order") or []))
        return themes_state(request)

    @app.post("/api/library/themes/{theme_id}/move")
    def move_theme(theme_id: str, request: Request, payload: dict = Body(...)):
        """Make one of your themes common, or merge it into a common theme.

        Only once every code in it is common: a common theme holds agreed codes
        only, so the codes are agreed first and the theme after.
        """
        coder = current_coder(request)
        own = registry.theme_store(coder)
        theme = own._find(theme_id)
        if theme is None:
            raise HTTPException(status_code=404, detail="unknown theme")
        common_refs = code_refs(COMMON)
        blocking = [r for r in theme.get("refs", []) if r not in common_refs]
        if blocking:
            names = {i["ref"]: i["name"] for i in code_items(registry)}
            return JSONResponse(
                {"detail": "move these codes to common first", "blocking": [{"ref": r, "name": names.get(r, r)} for r in blocking]},
                status_code=409,
            )
        if not payload.get("final"):
            raise HTTPException(status_code=400, detail="confirm the theme has been discussed and is final")
        common = registry.common_themes
        common.writer = coder
        cards = [c for c in own.cards() if c.get("theme_id") == theme_id]
        into = payload.get("into")
        if into:
            if common._find(into) is None:
                raise HTTPException(status_code=400, detail="that common theme no longer exists")
            for card in cards:
                common.place(card["ref"], into, None, None)
            if payload.get("description") is not None:
                common.update(into, {"note": payload["description"]})
        else:
            box = {k: theme.get(k) for k in ("x", "y", "w", "h")}
            common.create(
                payload.get("title") or theme.get("title", ""), theme.get("color"), box,
                theme.get("note", "") if payload.get("description") is None else payload["description"],
                [{"ref": c["ref"], "x": c["x"], "y": c["y"]} for c in cards],
            )
        own.delete(theme_id)
        return themes_state(request)

    # -- the canvas: the same themes, laid out on a plane -------------------
    #
    # Every one of these answers with the whole canvas rather than the piece it
    # touched. Dragging a card from one area to another changes two themes'
    # membership, and a client rebuilding that from a narrower reply is a client
    # that can drift from the file.

    def _ref(payload: dict) -> str:
        ref = payload.get("ref")
        if not ref:
            raise HTTPException(status_code=400, detail="a code reference is required")
        return str(ref)

    def _theme_id(payload: dict, key: str = "theme_id") -> str | None:
        value = payload.get(key)
        return str(value) if value else None

    @app.post("/api/library/canvas/place")
    def place_card(request: Request, payload: dict = Body(...)) -> dict:
        """Put a card down, in an area or loose on the canvas.

        ``moved_from`` present means a drag: the card it names is picked up
        rather than copied. Absent means a fresh card, which is how the same
        code comes to sit in two themes at once. A drag between your own themes
        and common ones takes the card out of one store and puts it in the other.
        """
        ref = _ref(payload)
        theme_id = _theme_id(payload)
        owner, target = store_of(request, theme_id)
        check_ref(ref, owner)
        moved = _theme_id(payload, "moved_from") if "moved_from" in payload else _MOVED_ABSENT
        try:
            source = store_of(request, moved)[1] if moved is not _MOVED_ABSENT else target
            # Across stores, the card is placed before it is unplaced, so a failed write leaves it in both themes.
            target.place(ref, theme_id, payload.get("x"), payload.get("y"),
                         **({"moved_from": moved} if moved is not _MOVED_ABSENT and source is target else {}))
            if source is not target:
                source.unplace(ref, moved)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc
        return themes_state(request)

    @app.post("/api/library/canvas/unplace")
    def unplace_card(request: Request, payload: dict = Body(...)) -> dict:
        """Take one card off the canvas, leaving other cards for the same code."""
        theme_id = _theme_id(payload)
        _, store = store_of(request, theme_id)
        store.unplace(_ref(payload), theme_id)
        return themes_state(request)

    @app.post("/api/library/canvas/positions")
    def move_cards(request: Request, payload: dict = Body(...)) -> dict:
        moves = payload.get("moves")
        if not isinstance(moves, list):
            raise HTTPException(status_code=400, detail="moves must be a list")
        by_store: dict[int, tuple[ThemeStore, list]] = {}
        for move in (m for m in moves if isinstance(m, dict)):
            _, store = store_of(request, move.get("theme_id") or None)
            by_store.setdefault(id(store), (store, []))[1].append(move)
        for store, own_moves in by_store.values():
            store.reposition(own_moves)
        return themes_state(request)

    @app.post("/api/library/canvas/reshape")
    def reshape_area(request: Request, payload: dict = Body(...)) -> dict:
        """Move or resize a theme's area. Its codes travel with it."""
        theme_id = _theme_id(payload)
        if theme_id is None:
            raise HTTPException(status_code=400, detail="a theme is required")
        owner, store = store_of(request, theme_id)
        return {"theme": {**store.reshape(theme_id, payload), "coder": owner}}

    @app.post("/api/library/canvas/tidy")
    def tidy_area(request: Request, payload: dict = Body(...)) -> dict:
        """Pack one area's code cards into a grid, alphabetically, and grow it to fit."""
        theme_id = _theme_id(payload)
        if theme_id is None:
            raise HTTPException(status_code=400, detail="a theme is required")
        _, store = store_of(request, theme_id)
        store.tidy(theme_id, code_packing_order(code_items(registry)))
        return themes_state(request)

    # -- reading the corpus by meaning ------------------------------------

    def semantics_for(quotes: list[dict], neural: bool):
        """Vectors for the current corpus, rebuilt only when it changes.

        Encoding is the slow part, so the result is held against a fingerprint
        of the quote texts and the backend that produced it.
        """
        fingerprint = (
            tuple(sorted(f"{q['ref']}:{hash(q.get('text',''))}" for q in quotes)),
            bool(neural),
        )
        cached = getattr(app.state, "semantics", None)
        if cached and cached[0] == fingerprint:
            return cached[1]
        model = Semantics.build(quotes, app.state.vectors, prefer_neural=neural)
        app.state.semantics = (fingerprint, model)
        return model

    @app.get("/api/library/semantics")
    def get_semantics(
        request: Request,
        neural: bool = Query(False, description="use the sentence-transformer model"),
        clusters: int = Query(0, ge=0, le=20),
    ) -> dict:
        quotes = corpus_for(request)
        try:
            model = semantics_for(quotes, neural)
            coords = model.project()
            labels, count = model.cluster(clusters or None)
            terms = model.cluster_terms(labels)
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        grouped: dict[int, list[str]] = {}
        for ref, label in zip(model.refs, labels):
            grouped.setdefault(int(label), []).append(ref)

        return {
            "backend": model.backend,
            "projector": model.projector,
            "neural_available": neural_available(),
            "points": [
                {"ref": ref, "x": round(x, 5), "y": round(y, 5), "cluster": int(label)}
                for ref, (x, y), label in zip(model.refs, coords, labels)
            ],
            "clusters": [
                {
                    "id": label,
                    "size": len(refs),
                    "terms": terms.get(label, []),
                    "refs": refs,
                }
                for label, refs in sorted(grouped.items())
            ],
            "cluster_count": count,
            "loneliest": model.loneliest(),
            "saturation": saturation(quotes, [r.summary() for r in registry.list()]),
        }

    @app.get("/api/library/similar")
    def get_similar(
        request: Request, ref: str = Query(...), k: int = Query(6, ge=1, le=40), neural: bool = Query(False)
    ) -> dict:
        try:
            model = semantics_for(corpus_for(request), neural)
            return {"ref": ref, "similar": model.similar(ref, count=k)}
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/library/suggestions")
    def get_suggestions(request: Request, neural: bool = Query(False)) -> dict:
        """Where each unsorted quote would go, judged by the company it keeps."""
        quotes = corpus()
        placed = {ref: theme["id"] for _, store in stores_for(request) for theme in store.list() for ref in theme["refs"]}
        if not placed:
            return {"suggestions": []}
        try:
            model = semantics_for(quotes, neural)
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        out = []
        for quote in quotes:
            if quote["ref"] in placed:
                continue
            hint = model.suggest_theme(quote["ref"], placed)
            if hint:
                out.append({"ref": quote["ref"], **hint})
        out.sort(key=lambda s: -s["confidence"])
        return {"suggestions": out}

    @app.post("/api/library/themes/from-refs", status_code=201)
    def theme_from_refs(request: Request, payload: dict = Body(...)) -> dict:
        """Make one of your themes from the codes on a set of quotes, as lassoed on the map.

        A theme holds codes, so it takes the codes those quotes carry -- your own
        and common ones -- rather than the quotes themselves.
        """
        coder = current_coder(request)
        wanted = {str(ref) for ref in (payload.get("refs") or [])}
        if not wanted:
            raise HTTPException(status_code=400, detail="no quotes were selected")
        allowed = code_refs(coder)
        refs = []
        for quote in corpus():
            if quote["ref"] in wanted:
                for code_id in quote.get("codes") or []:
                    ref = f"text:{code_id}"
                    if ref in allowed and ref not in refs:
                        refs.append(ref)
        if not refs:
            raise HTTPException(status_code=400, detail="none of those quotes carries a code of yours or a common one")
        store = registry.theme_store(coder)
        theme = store.create(payload.get("title", ""), payload.get("color"))
        # Added, not moved: a code can be in several themes, so lassoing it into
        # a new one leaves it in the themes it was already in.
        for ref in refs:
            store.place(ref, theme["id"], None, None)
        return {"theme": {**store._find(theme["id"]), "coder": coder}, **themes_state(request)}

    # -- pages -------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        # Always the library, whatever it holds. One entry point beats a home
        # page that changes shape depending on how many folders it found.
        return FileResponse(STATIC_DIR / "library.html")

    @app.get("/reader")
    def reader() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/themes")
    def themes_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "themes.html")

    @app.get("/codebook")
    def codebook_page() -> FileResponse:
        # Each codebook in full, with every use of a code across the study.
        return FileResponse(STATIC_DIR / "codebook.html")

    @app.get("/code")
    def coding_page() -> FileResponse:
        # Coding the video on its own: the recording and its video codes, no
        # transcript. It writes the same files the reader shows.
        return FileResponse(STATIC_DIR / "coding.html")

    @app.middleware("http")
    async def revalidate_assets(request: Request, call_next):
        """Never let a page link a cached script against a newer one.

        The frontend is ES modules importing each other by name. A browser holding
        yesterday's ``util.js`` next to today's ``app.js`` does not degrade -- the
        import fails to link and the *entire* module graph dies, so the page loads
        and then does nothing at all, with an error that names no file. Asking for
        revalidation on every asset makes that state unreachable; with ETags
        already in place it costs one 304 per file.

        Media is left alone: those are large, immutable, and range-requested.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path in PAGES:
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    root = registry.root or Path.cwd()
    app.state.vectors = VectorCache(root / EMBEDDINGS_FILENAME)
    app.state.semantics = None
    return app
