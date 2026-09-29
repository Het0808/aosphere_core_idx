"""The Prompts screen: what it must not get wrong (AOSNG-3442).

The page is a Python string of HTML+JS, so these are source-level assertions in the style the
repo already uses for web.py. They pin the handful of properties where a mistake would be
either invisible or harmful, rather than re-testing that the markup exists.

The screen edits the prompt that decides what the assistant asserts about regulated content.
That makes three things load-bearing: an empty box must CLEAR the override rather than install
an empty prompt, a refusal must read as a refusal rather than a bug, and the tab must not
appear to users the API will refuse.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src/aosphere_core_index/service/web.py"
RAW = WEB.read_text(encoding="utf-8")


def _no_comments(src: str) -> str:
    return "\n".join("" if ln.lstrip().startswith(("//", "#")) else ln for ln in src.split("\n"))


SRC = _no_comments(RAW)


def test_an_empty_box_clears_the_override_rather_than_saving_an_empty_prompt():
    """THE one that matters. An empty system prompt would strip the grounding and citation
    rules entirely — the assistant would answer from outside knowledge with nothing to stop
    it. Clearing must be a DELETE, and the user must be asked first."""
    i = SRC.index("async function savePrompt(")
    body = SRC[i:SRC.index("\nfunction renderPrompts(")]
    assert 'const clearing = body === ""' in body
    assert 'method:"DELETE"' in body, "an empty box must DELETE the override, not PUT an empty one"
    assert "confirm(" in body, "clearing a prompt must be confirmed"
    # and the PUT path must never be reachable with an empty body
    assert re.search(r'clearing\s*\?\s*\{method:"DELETE"', body), \
        "the DELETE/PUT choice must be driven by `clearing`"


def test_a_403_reads_as_a_refusal_not_a_failure():
    """Editing is admin-only server-side. A non-admin who reaches the screen should be told
    they are not permitted, not shown a generic error that looks like a bug."""
    i = SRC.index("async function savePrompt(")
    body = SRC[i:SRC.index("\nfunction renderPrompts(")]
    assert "r.status===403" in body and "restricted to admin" in body


def test_the_tab_is_hidden_until_the_server_permits_it():
    """Probed, not assumed from a client-side role: the UI must not offer a screen the API
    will refuse, and must not be persuadable into showing one."""
    assert 'class="tab prompttab" data-mode="prompts" style="display:none"' in RAW
    i = SRC.index("async function revealPromptsTab(")
    body = SRC[i:SRC.index("\nasync function loadPrompts(")]
    assert "if(!r.ok) return;" in body, "a non-200 must leave the tab hidden"
    assert 'style.display="inline-flex"' in body


def test_the_reveal_probe_cannot_be_cancelled_by_a_screen_change():
    """It runs at startup alongside loadRegions/loadAi. Cancelling it on the first tab click
    would leave the tab permanently hidden for an admin."""
    i = SRC.index("async function revealPromptsTab(")
    body = SRC[i:SRC.index("\nasync function loadPrompts(")]
    assert "NO_CANCEL" in body


def test_switching_product_with_unsaved_edits_asks_first():
    """The list is one click from the textarea; losing an SME's wording silently is the
    obvious way to make this screen untrustworthy."""
    i = SRC.index("async function loadOnePrompt(")
    body = SRC[i:SRC.index("\nasync function savePrompt(")]
    assert "prDirty()" in body and "confirm(" in body


def test_the_screen_refuses_to_paint_when_it_is_not_visible():
    """Same rule as the other screens: an async response landing after the user has left must
    not repaint #detail."""
    i = SRC.index("function renderPrompts(")
    assert 'classList.contains("promptmode")' in SRC[i:i + 260]


def test_product_names_ride_in_data_attributes_not_inline_handlers():
    """Product names carry spaces, ampersands, parentheses and commas — '160_Bank_
    Confidentiality_&_Outsourcing', 'Canada (Alberta, British Columbia…)'. An inline onclick
    with such a value terminates the attribute and silently kills the handler, which is the
    bug that made the run bar dead for weeks."""
    i = SRC.index("function renderPrompts(")
    body = SRC[i:SRC.index("\nfunction renderPromptPane(")]
    assert 'data-prod="' in body and "escA(p.product)" in body
    assert "onclick=" not in body, "no inline handler may carry a product name"


def test_the_multi_product_rule_is_explained_on_the_screen():
    """A user editing one product's prompt has to know it does NOT apply to a question that
    spans products — otherwise they will write a prompt, ask a cross-product question, and
    reasonably conclude the feature is broken."""
    i = SRC.index("function renderPrompts(")
    body = SRC[i:SRC.index("\nfunction renderPromptPane(")]
    assert "one</b>" in body and "default" in body


def test_the_default_row_sits_above_the_products_and_is_never_editable():
    """The row exists to READ the prompt every product falls back to. Rendering the editor,
    the Save button or the Revert button on it would offer an edit that has nowhere to go —
    the store holds overrides only — and would read as a way to change every product at once."""
    i = SRC.index("function renderPrompts(")
    body = SRC[i:SRC.index("\nfunction renderDefaultPane(")]
    lst = body[body.index('<div class="prlist">'):body.index("for(const p of _prProducts)")]
    assert "<b>Default</b>" in lst and "prdefrow" in lst, "the default row must come first"

    pane = SRC[SRC.index("function renderDefaultPane("):SRC.index("\nfunction renderPromptPane(")]
    for forbidden in ("prtext", "prsave", "prrevert", "prseed", "textarea"):
        assert forbidden not in pane, f"the read-only pane must not render {forbidden}"
    assert 'data-prview="diff"' not in pane, "there is nothing to diff the default against"
    assert "Read-only" in pane


def test_the_read_only_pane_is_chosen_by_the_servers_flag():
    """Not by comparing the selection to a string in two places: the pane and the fetch must
    agree, or a product could be painted with the uneditable pane (or the reverse)."""
    pane = SRC[SRC.index("function renderPromptPane("):SRC.index("let _exRun")]
    assert "if(_prData.read_only) return renderDefaultPane();" in pane
    i = SRC.index("async function loadOnePrompt(")
    load = SRC[i:SRC.index("\nasync function savePrompt(")]
    assert "/api/prompts/_default?mode=" in load, "the default has no product to ask through"
    assert "_prDraft=_prData.read_only ? null" in load, \
        "a null draft is what makes prDirty() and savePrompt() inert on this row"


def test_the_default_row_cannot_be_saved_even_if_a_button_appeared():
    """Belt and braces: both write paths key on the draft, which stays null on this row."""
    assert "_prDraft !== null" in SRC[SRC.index("function prDirty("):][:120]
    i = SRC.index("async function savePrompt(")
    assert "_prDraft===null" in SRC[i:i + 200]


def test_landing_on_the_default_leaves_the_diff_view():
    """The diff view compares the draft to the default. Arriving from a product with the diff
    open would otherwise paint an empty pane, because renderDefaultPane offers no diff."""
    i = SRC.index("async function loadOnePrompt(")
    load = SRC[i:SRC.index("\nasync function savePrompt(")]
    assert 'if(product===PRDEF && _prView==="diff") _prView="edit";' in load


def test_the_default_row_survives_a_reload_of_the_list():
    """A save elsewhere reloads the list. Resetting the selection to the first product would
    throw the reader off the default prompt they were reading."""
    i = SRC.index("async function loadPrompts(")
    body = SRC[i:SRC.index("\nasync function loadOnePrompt(")]
    assert "if(!prIsDefault() &&" in body


def test_the_default_pane_shows_the_prompt_for_the_MODE_being_viewed():
    """Summary and Explain have DIFFERENT defaults. One shown for both would be a lie about
    what answers a question, and this row is the only place the difference is visible."""
    pane = SRC[SRC.index("function renderDefaultPane("):SRC.index("\nfunction renderPromptPane(")]
    assert "_prMode" in pane and "default_prompt" in pane
    i = SRC.index("async function loadOnePrompt(")
    assert "_default?mode=\"+enc(_prMode)" in SRC[i:SRC.index("\nasync function savePrompt(")]


def test_the_effective_prompt_is_shown_and_the_uneditable_parts_are_named():
    """The box holds only the product half. Saying so prevents an SME wondering why their
    formatting instructions are ignored."""
    i = SRC.index("function renderPromptPane(")
    body = SRC[i:SRC.index("let _exRun")]
    assert "prompt actually in effect" in body
    assert "not editable here" in body
    assert "_prData.effective" in body


def test_audit_information_is_surfaced_when_present():
    i = SRC.index("function renderPromptPane(")
    body = SRC[i:SRC.index("let _exRun")]
    assert "updated_by" in body and "updated_at" in body


def test_the_prompts_mode_is_wired_into_the_shell():
    assert 'main.classList.toggle("promptmode", pr)' in SRC
    assert "loadPrompts();" in SRC
    assert "main.promptmode" in RAW, "the mode needs its full-width layout rule"


# --------------------------------------------------------- preview and diff (source-level)

def _pane_src() -> str:
    i = SRC.index("function setPrView(")
    return SRC[i:SRC.index("let _exRun")]


def test_the_textarea_stays_the_only_source_of_truth():
    """The stored text goes VERBATIM into the system prompt, and agent.py records a measurement
    showing that WHERE an instruction sits changes whether it holds. So the derived views are
    read-only: a preview that round-tripped the draft through HTML could normalise whitespace
    and alter the prompt without the author ever seeing it."""
    body = _pane_src()
    for fn in ("prRenderPreview", "prRenderDiff", "prLineDiff"):
        i = SRC.index(f"function {fn}(")
        src = SRC[i:SRC.index("\n}\n", i)]
        assert "_prDraft=" not in src.replace(" ", ""), f"{fn} must not write back to the draft"
    assert 'ew.hidden = v!=="edit"' in body, "the editor must stay mounted, only hidden"


def test_switching_view_repaints_the_pane_and_not_the_whole_screen():
    """A full re-render would drop the caret and the scroll position mid-prompt — the SME is
    editing 4,000 characters, and looking at the diff must not cost them their place."""
    i = SRC.index("function setPrView(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "renderPrompts()" not in body, "switching view must not re-render the screen"
    assert "#prtext" in body and "#prview" in body


def test_the_diff_is_against_the_product_default_not_the_assembled_prompt():
    """`effective` includes the answer-format contract, which is NOT editable here. Diffing
    against it would show every override as having deleted a block it never had."""
    i = SRC.index("function prRenderDiff(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "_prData.default_prompt" in body
    assert "effective" not in body, "the diff base must be the product half of the default"


def test_a_product_on_the_default_shows_no_phantom_diff():
    """With no override the box is empty; a naive diff would render the entire default as
    'deleted', which reads as a catastrophic change that has not happened."""
    i = SRC.index("function prRenderDiff(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert 'draft===""' in body and "No override" in body


def test_the_preview_escapes_the_authors_text():
    """An admin is trusted with the prompt, not with the page: a prompt containing markup must
    render as text. mdToHtml escapes before it renders, which is why it is reused here rather
    than a second renderer being written."""
    assert "mdToHtml(t)" in _pane_src()
    i = SRC.index("function mdToHtml(")
    assert 't=esc(t||"")' in SRC[i:i + 120], "mdToHtml must escape first; the preview relies on it"


def test_the_diff_bounds_its_own_cost():
    """O(n*m) over lines. The API caps an override at 20,000 characters, so a real diff is
    sub-millisecond; the guard exists so that a pasted document cannot hang the tab before the
    API ever sees it."""
    i = SRC.index("function prLineDiff(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "A.length*B.length > 400000" in body
    assert "return [[" in body, "over the cap it must return a whole-text replacement, not throw"


def test_starting_from_the_default_does_not_save_and_will_not_overwrite():
    """Writing an override in an empty box means discarding a prompt that is known to work,
    which is the likeliest way this feature makes answers worse. Seeding is an edit, not a
    save — and it must never clobber wording already in the box."""
    i = SRC.index('if(sd) sd.addEventListener("click"')
    body = SRC[i:i + 400]
    assert "savePrompt" not in body, "seeding must not save; the SME reviews it first"
    assert 'if((_prDraft||"").trim()) return;' in body
    assert "default_prompt" in body


# --------------------------------------------------------- the markdown editor (EasyMDE, MIT)

def _mount_src() -> str:
    i = SRC.index("async function prMountEditor(")
    return SRC[i:SRC.index("\n}\n", i)]


def test_the_editor_is_pinned_and_integrity_checked():
    """This script can rewrite the prompt that decides what the assistant asserts about
    regulated content. A floating version from a CDN would mean that text is edited by whatever
    the CDN served today."""
    assert "easymde@2.20.0/dist/easymde.min.js" in SRC, "the version must be exact, not a range"
    assert "@latest" not in SRC and "easymde@2/" not in SRC
    for c in ("MDE_JS_SRI", "MDE_CSS_SRI"):
        assert re.search(rf'{c}="sha384-[A-Za-z0-9+/=]{{60,}}"', SRC), f"{c} must be a real hash"
    i = SRC.index("function prLoadEditor(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "sc.integrity=MDE_JS_SRI" in body and 'sc.crossOrigin="anonymous"' in body, \
        "SRI without crossOrigin is not enforced"
    assert "l.integrity=MDE_CSS_SRI" in body


def test_a_blocked_or_failed_cdn_leaves_a_working_textarea():
    """The editor is an affordance; the textarea is the mechanism. This screen is admin-only
    behind a WAF, and an SME who cannot edit the prompt because a CDN is unreachable is a
    worse outcome than a plain box."""
    i = SRC.index("function prLoadEditor(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "sc.onerror=()=>res(false)" in body, "a load failure must resolve, not reject or hang"
    assert "if(!await prLoadEditor()) return;" in _mount_src(), \
        "a failed load must leave the textarea untouched"


def test_the_editor_is_loaded_lazily_and_not_in_the_page_head():
    """319KB of JavaScript for a tab most users never see, and never will see — the tab itself
    is hidden until the server permits it."""
    head = RAW[:RAW.index("<style>")]
    assert "easymde" not in head, "the editor must not be in the document head"
    assert "document.head.appendChild(sc)" in SRC


def test_autosave_is_off():
    """THE one that would bite. EasyMDE's autosave is keyed by a single uniqueId in
    localStorage, so with three products on one screen it restores the WRONG product's draft —
    and a stale draft of a regulated-content prompt reappearing on its own, possibly under
    another product's name, is exactly what this screen must never do."""
    body = _mount_src()
    assert "autosave: {enabled:false}" in body
    assert "uniqueId" not in SRC, "no autosave key may be configured at all"


def test_the_toolbar_offers_only_the_four_agreed_tools():
    """A prompt is not a document. Link, image, code-block, quote and fullscreen would only
    let an author paste syntax that means nothing downstream."""
    i = SRC.index("function prToolbar(")
    body = SRC[i:SRC.index("\n}\n", i)]
    names = re.findall(r'name:"([a-z-]+)"', body)
    assert names == ["heading", "bold", "unordered-list", "ordered-list", "table"]
    for absent in ("link", "image", "code", "quote", "fullscreen", "side-by-side", "guide"):
        assert f'name:"{absent}"' not in body, f"{absent} is not one of the agreed tools"


def test_the_toolbar_needs_no_icon_font():
    """EasyMDE emits Font Awesome class names and ships no icon font, so the stock toolbar is a
    row of blank buttons. Text labels avoid pulling in a whole icon pack — and nothing on this
    page loads one."""
    i = SRC.index("function prToolbar(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert body.count("text:") == 5, "every button needs its own label"
    assert "className" not in body, "a className would append an <i> icon that cannot render"
    assert "font-awesome" not in RAW and "fontawesome" not in RAW.lower()


def test_the_toolbar_inserts_only_syntax_this_pages_renderer_understands():
    """The Preview tab renders with mdToHtml, not with EasyMDE's own parser. A toolbar that
    inserted `__bold__` or a table without a separator row would produce text the preview shows
    as literal punctuation — and the author would trust the wrong picture."""
    body = _mount_src()
    assert 'blockStyles: {bold:"**", italic:"*"}' in body, "mdToHtml reads ** for bold, not __"
    assert 'unorderedListStyle: "-"' in body
    assert "| --- | --- |" in body, "mdToHtml only builds a table when a separator row follows"
    assert "previewRender: (t)=>mdToHtml(t)" in body, "one renderer, one escaping path"


def test_the_draft_is_tracked_from_the_editor_not_only_the_textarea():
    """forceSync writes to the textarea programmatically, which fires NO input event. Relying
    on the input listener alone would mean every keystroke in the editor was invisible to the
    dirty state — the Save button would stay disabled and the work would be lost on switch."""
    body = _mount_src()
    assert 'codemirror.on("change"' in body and "_prDraft=_prMde.value()" in body
    assert "forceSync: true" in body, "the textarea must stay truthful for the fallback path"


def test_the_editor_is_detached_before_the_pane_is_repainted():
    """Every repaint replaces #detail. Without an explicit detach, each one would leak a
    CodeMirror instance and its document-level key handlers."""
    i = SRC.index("function renderPrompts(")
    assert "prDestroyEditor();" in SRC[i:i + 300], "detach must happen before innerHTML is set"
    i = SRC.index("function prDestroyEditor(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "toTextArea()" in body and "catch" in body, "detaching must not be able to throw"


def test_returning_to_the_editor_refreshes_its_measurements():
    """CodeMirror measures itself on mount. Mounted or hidden inside a display:none container
    those measurements are zero, and it paints an empty gutter — the classic symptom being an
    editor that looks broken after switching tabs."""
    i = SRC.index("function setPrView(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "_prMde.codemirror.refresh()" in body


def test_a_race_between_the_lazy_load_and_a_repaint_cannot_double_mount():
    """The load is 319KB over a network; a product click or a tab change mid-load is likely,
    not hypothetical. Two editors on one textarea would double every keystroke."""
    body = _mount_src()
    assert "if(!ta || _prMde) return;" in body, "an existing editor must not be replaced"
    assert "still!==ta" in body, "the textarea it was asked to upgrade must still be the live one"


# --------------------------------------------------------- the two answer modes

def test_the_mode_is_sent_on_every_prompt_request():
    """Two prompts per product, chosen by mode. A request without it would read or write the
    wrong one — and a save landing on the wrong mode would replace a prompt the SME did not
    open."""
    for fn, nxt in [("async function loadOnePrompt(", "\nasync function setPrMode("),
                    ("async function savePrompt(", "\nfunction renderPrompts(")]:
        i = SRC.index(fn)
        body = SRC[i:SRC.index(nxt)]
        assert 'enc(_prMode)' in body, f"{fn} does not send the mode"


def test_switching_mode_warns_about_unsaved_work():
    """The two modes are separate prompts, so moving between them discards the box. Losing an
    SME's wording silently is how this screen becomes untrustworthy."""
    i = SRC.index("async function setPrMode(")
    body = SRC[i:SRC.index("\nasync function savePrompt(")]
    assert "prDirty()" in body and "confirm(" in body
    assert "loadOnePrompt(_prSel, true)" in body, "the reload must not ask a second time"


def test_the_list_badge_reflects_the_mode_being_edited():
    """THE one that matters here. A single 'override' badge would mark a product as
    customised when only its OTHER answer was, which is the likeliest way to edit the wrong
    prompt — and overwrite a good one."""
    i = SRC.index("function renderPrompts(")
    body = SRC[i:SRC.index("\nfunction renderPromptPane(")]
    assert "(p.modes||[]).indexOf(_prMode)>=0" in body
    assert "p.has_override" not in body, "a mode-blind flag must not drive the badge"
    assert "prother" in body, "the other mode's override should still be visible"


def test_the_default_mode_matches_the_explain_toggle_being_off():
    """The screen opens on the prompt that answers most questions."""
    assert "let _prMode='summary';" in SRC


def test_the_modes_are_labelled_by_what_they_do_not_by_their_key():
    """An SME does not know 'summary' and 'explain' are storage keys; they know the Explain
    checkbox."""
    i = SRC.index("function renderPrompts(")
    body = SRC[i:SRC.index("\nfunction renderPromptPane(")]
    assert "Summary answers" in body and "Explain answers" in body
    assert "Explain is on" in body and "Explain is off" in body
