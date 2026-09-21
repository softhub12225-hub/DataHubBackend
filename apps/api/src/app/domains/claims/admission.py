"""Admission-requirement and programme candidates (Step 5C.2 sections 6-10).

ADMISSION REQUIREMENTS ARE THE HIGHEST-RISK FIELD
=================================================
A wrong entry requirement sends a student to apply for something they cannot get, or
stops one applying for something they can. So this extractor does the least it can get
away with: it finds requirement *blocks*, preserves their wording, and records whatever
scope and qualification the page stated.

It does not decide what the requirement means.

SCOPE IS NEVER DEFAULTED
========================
The dangerous move is reading a requirement with no country wording as applying to
everybody. `UNIVERSAL` would then be asserted from silence, and a rule written for UK
A-level applicants would be shown to a Chinese applicant as their requirement.

So: explicit scope wording is preserved raw, mapped only where a deterministic marker
exists, and otherwise the claim carries `SCOPE_MAPPING_REQUIRED` and stays unresolved.

DEGREE LEVEL IS NOT GUESSED
===========================
"Graduate" does not mean PhD. "Advanced" means nothing. Only unambiguous award names
map, and an ambiguous one keeps its label with a null level (section 7).
"""

# ruff: noqa: RUF001 -- the non-ASCII characters in the patterns below are
# deliberate. Real university pages write fee ranges with EN DASH and possessives
# with RIGHT SINGLE QUOTATION MARK; a pattern matching only the ASCII hyphen and
# apostrophe would silently miss those pages, which is the bug this rule would
# otherwise talk us into.

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.domains.claims.locator import Locator, heading_path_for
from app.domains.claims.model import Candidate, Confidence, FieldKind, is_chrome
from app.domains.extraction.html_document import BODY_CONTAINERS

EXTRACTOR = "admission-rule-extractor"
#: Version 4. Every bump came from auditing real pages:
#:
#: * v1 -> v2: site navigation was read as requirement prose, so Imperial's menu became
#:   a 2,000-character admission requirement.
#: * v2 -> v3: an ancestor heading alone earned MEDIUM, which on an admissions page is
#:   every paragraph -- a dean's byline, a news blurb, "students from all walks of life
#:   come together". They are still candidates; they are no longer indistinguishable
#:   from "applicants must hold a bachelor's degree".
#: * v3 -> v4: two scope markers were wrong in opposite directions -- one asserted a
#:   United States scope from the pronoun "us", the other could not match "Chinese".
#:   See `_SCOPE_MARKERS`.
#: * v4 -> v5 (Step 5C.4 section 7): a unit whose entire text is one anchor's text is
#:   not a requirement -- "International entry requirements" is a link *to* the
#:   requirements. Chrome exclusion moves to the sticky `in_chrome`, because document v2
#:   added `section` to the container list and a `<section>` inside a `<nav>` would
#:   otherwise report `section` and pass. And the degree level is read from the wording
#:   plus the innermost heading rather than the whole path, because a page titled
#:   "Graduate Admissions" was stamping `degree_level_raw='Graduate'` on every
#:   paragraph on it.
#: * v5 -> v6 (Step 5C.5 sections 1 and 2): a country token is not an applicant
#:   jurisdiction. Of twelve jurisdiction scopes on the real corpus, three were right
#:   and the rest were a subject ("Chinese medicine"), a campus location ("Beijing,
#:   China"), a city list, an institution's own name, a postal address and navigation
#:   text -- every one of them reading as *resolved*. A jurisdiction now requires one of
#:   five named applicant/qualification constructions. The two scope dimensions are also
#:   separated: `applicant_jurisdictions` and `qualification_systems` are carried beside
#:   the combined list, because where an applicant is from and what they hold are
#:   different questions.
VERSION = "6"

PROGRAM_EXTRACTOR = "program-rule-extractor"
#: The programme rule carries its own version, because it is its own extractor. A
#: shared constant would mean a correction to one relabelled every claim from the
#: other as a new version, which is the opposite of what versioning is for.
#:
#: It starts at 2: version 1 recorded a link-derived claim with `json_ld_path` set to
#: `"links.192"`, which `resolve` could not follow, so those locators pointed nowhere.
#: Those rows are retained as superseded history. Version 3 additionally stops
#: reading award-shaped headings out of site navigation. Version 4 carries the
#: `heading_path_for` correction: a programme heading's nearest preceding heading is
#: usually its SIBLING, and it was being recorded as its parent section, so
#: "Archaeology, BA (Hons)" sat under "Anglo-Saxon, Norse, and Celtic, BA (Hons)".
#:
#: Version 5 (Step 5C.4 sections 6 and 9): a link may become a programme name only from
#: **body** content. Document v2 records a link's container and whether its ancestry was
#: chrome, which v4 had no way of knowing -- 60% of the fleet's links are inside
#: navigation, and v4 read all of them. See `_body_origin`.
PROGRAM_VERSION = "5"

# --- degree level ------------------------------------------------------------------
#: Only unambiguous awards. The reference table holds BACHELOR / MASTER / DOCTORATE.
_DEGREE_LEVELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "DOCTORATE",
        re.compile(
            r"\bPh\.?D\.?\b|\bD\.?Phil\.?\b|\bDoctor\s+of\s+Philosophy\b|\bdoctoral\b"
            r"|\bdoctorate\b",
            re.I,
        ),
    ),
    (
        "MASTER",
        re.compile(
            r"\bM\.?Sc\.?\b|\bM\.?A\.?\b(?!\w)|\bM\.?Eng\.?\b|\bM\.?Phil\.?\b|\bMBA\b"
            r"|\bM\.?Res\.?\b|\bLL\.?M\.?\b|\bM\.?Arch\.?\b|\bM\.?Fin\.?\b"
            r"|\bmaster(?:'|’)?s?\s+(?:degree|programme|program|course)\b"
            r"|\bMaster\s+of\s+\w+",
            re.I,
        ),
    ),
    (
        "BACHELOR",
        re.compile(
            r"\bB\.?Sc\.?\b|\bB\.?A\.?\b(?!\w)|\bB\.?Eng\.?\b|\bLL\.?B\.?\b"
            r"|\bbachelor(?:'|’)?s?\s+(?:degree|programme|program|course)\b"
            r"|\bBachelor\s+of\s+\w+|\bundergraduate\s+degree\b",
            re.I,
        ),
    ),
)

#: Words that look like a level and are not one. Present so the refusal is testable.
AMBIGUOUS_LEVEL_WORDS = re.compile(
    r"\bgraduate\b|\bpostgraduate\b|\badvanced\b|\bhigher\s+degree\b|\btaught\s+programme\b",
    re.I,
)

# --- discipline (the three pilot top-levels only) ----------------------------------
_DISCIPLINES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "COMPUTER_AND_DATA",
        re.compile(
            r"\bcomputer\s+science\b|\bcomputing\b|\bdata\s+science\b|\bartificial\s+"
            r"intelligence\b|\bmachine\s+learning\b|\binformatics\b|\bsoftware\s+"
            r"engineering\b|\bdata\s+analytics\b",
            re.I,
        ),
    ),
    (
        "ENGINEERING",
        re.compile(
            r"\b(?:mechanical|electrical|civil|chemical|aerospace|biomedical|"
            r"electronic)\s+engineering\b|\bengineering\s+(?:science|degree)\b",
            re.I,
        ),
    ),
    (
        "BUSINESS",
        re.compile(
            r"\bbusiness\s+(?:administration|management|analytics|school)\b|\bMBA\b"
            r"|\bfinance\b|\baccounting\b|\bmanagement\s+(?:studies|science)\b"
            r"|\bmarketing\b|\beconomics\b",
            re.I,
        ),
    ),
)

# --- applicant scope --------------------------------------------------------------


class ScopeDimension(StrEnum):
    """Which question a scope marker answers.

    Two dimensions, never collapsed into one taxonomy (Step 5C.5 section 2). They were
    previously distinguishable only by `applicant_country_code` being null, which is an
    accident of the data rather than a statement about it.
    """

    JURISDICTION = "JURISDICTION"
    """Where the applicant is from, or where their qualification was awarded."""

    QUALIFICATION_SYSTEM = "QUALIFICATION_SYSTEM"
    """What the applicant holds. Says nothing about where they are from."""


@dataclass(frozen=True, slots=True)
class _Jurisdiction:
    """One place, with the two word-forms a page uses to refer to it.

    The name and the demonym are separate because the constructions differ: a demonym
    qualifies a noun ("Chinese applicants") and a name follows a preposition
    ("applicants from China"). Merging them into one pattern is what allowed a bare
    token to assert a scope.
    """

    label: str
    country_code: str
    #: Regex fragment for the place name. Scoped `(?i:...)` flags, never a global
    #: `re.I`: an acronym must stay case-sensitive or `\bUS\b` matches the pronoun.
    name: str
    #: Regex fragment for the demonym.
    adjective: str


_JURISDICTIONS: tuple[_Jurisdiction, ...] = (
    # `chin(?:a|ese)`, not `china(?:ese)?`. The latter is "chin" + "a" + an optional
    # "ese", so it matched "china" and the non-existent "chinaese" and could never
    # match "Chinese" -- which is the form that matters most here.
    _Jurisdiction("China", "CN", r"(?:(?i:mainland)\s+)?(?i:china)", r"(?i:chinese)"),
    _Jurisdiction("Hong Kong", "HK", r"(?i:hong\s*kong)", r"(?i:hong\s*kong)"),
    _Jurisdiction("India", "IN", r"(?i:india)", r"(?i:indian)"),
    _Jurisdiction("Singapore", "SG", r"(?i:singapore)", r"(?i:singaporean)"),
    _Jurisdiction("Malaysia", "MY", r"(?i:malaysia)", r"(?i:malaysian)"),
    # An ACRONYM is matched case-sensitively; a NAME is not. `\bUSA?\b` under `re.I`
    # matches the pronoun "us", and it had asserted a United States scope -- with
    # `unresolved_reason` NULL, so it read as *resolved* -- on pages whose wording was
    # "contact us" and "study with us". `USA` precedes `US` in the alternation so the
    # longer form wins.
    _Jurisdiction(
        "United States", "US", r"(?:(?i:united\s+states)|USA|US)", r"(?:(?i:american)|USA|US)"
    ),
    _Jurisdiction("EU", "EU", r"(?:(?i:european\s+union)|EU)", r"(?:(?i:european\s+union)|EU)"),
)

#: Nouns a demonym may qualify for the phrase to be about applicants. Deliberately
#: short: "Chinese medicine", "Chinese language" and "Chinese history" are subjects, and
#: every noun added here is a chance to let one back in.
_APPLICANT_ATTRIBUTE = (
    r"(?i:applicants?|students?|candidates?|nationals?|citizens?|residents?"
    r"|passport\s+holders?|school\s+leavers?|qualifications?|awards?"
    r"|high\s+schools?|secondary\s+schools?|curricul(?:um|a)|education\s+system)"
)

#: Who the sentence is about, for the prepositional forms.
_APPLICANT_NOUN = r"(?i:applicants?|students?|candidates?|pupils?|learners?)"

#: The relation that makes a place an applicant's origin rather than a location. Note
#: what is absent: a bare "in", which is what "a campus in Beijing, China" is made of.
_ORIGIN_RELATION = (
    r"(?i:from|educated\s+in|schooled\s+in|studying\s+in|studied\s+in"
    r"|based\s+in|resident\s+in|living\s+in|applying\s+from)"
)

#: How a qualification acquires a place.
_OBTAINED = r"(?i:obtained|taken|awarded|completed|studied|gained|sat|earned|issued)"

#: Words that make a preceding place name part of a qualification's title. This is the
#: construction that carries all three of the real true positives.
_QUALIFICATION_WORD = (
    r"(?:(?i:certificates?|diplomas?|matriculation|sijil|senior\s+secondary"
    r"|school\s+leaving|unified\s+examination|baccalaur\w*"
    r"|national\s+(?:college|higher|senior)\w*)"
    r"|GCE|GCSE|HKDSE|HKCEE|STPM|SPM|CBSE|ISC)"
)


def _jurisdiction_patterns(place: _Jurisdiction) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The constructions that let this place assert an applicant jurisdiction.

    Each is named, because section 12's rule for review evidence applies here too: an
    explicit reason beats an opaque score, and "NATIONAL_QUALIFICATION" tells a reviewer
    what to check in a way that a confidence number does not.
    """
    name, adjective = place.name, place.adjective
    return (
        ("DEMONYM", re.compile(rf"\b{adjective}\s+{_APPLICANT_ATTRIBUTE}\b")),
        (
            "APPLICANT_ORIGIN",
            re.compile(
                rf"\b{_APPLICANT_NOUN}\s+(?:(?i:who)\s+(?i:are|were|have\s+been)\s+)?"
                rf"{_ORIGIN_RELATION}\s+(?:(?i:the)\s+)?{name}\b"
            ),
        ),
        (
            "QUALIFICATION_ORIGIN",
            re.compile(
                rf"\b(?i:qualifications?|grades?|results?|awards?|degrees?)\s+"
                rf"(?:{_OBTAINED}\s+)?(?i:in|from)\s+(?:(?i:the)\s+)?{name}\b"
            ),
        ),
        (
            "CONDITIONAL_ORIGIN",
            re.compile(
                rf"\b(?i:if\s+you)\s+(?i:are|were|have\s+been)\s+"
                rf"{_ORIGIN_RELATION}\s+(?:(?i:the)\s+)?{name}\b"
            ),
        ),
        (
            "NATIONAL_QUALIFICATION",
            re.compile(rf"\b{name}[\s/\u2013\u2014-]+(?:\w+[\s/]+){{0,2}}?{_QUALIFICATION_WORD}\b"),
        ),
    )


#: place -> its constructions, compiled once.
_JURISDICTION_RULES: tuple[tuple[_Jurisdiction, tuple[tuple[str, re.Pattern[str]], ...]], ...] = (
    tuple((place, _jurisdiction_patterns(place)) for place in _JURISDICTIONS)
)

#: Qualification systems. These need no surrounding construction: an A-level *is* a
#: thing an applicant holds, and the marker is the semantics. They carry no country
#: code -- a Gaokao strongly suggests a Chinese applicant and suggesting is not
#: extracting (section 2).
_QUALIFICATION_SYSTEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("A-level", re.compile(r"\bA[-\s]?levels?\b", re.I)),
    ("IB", re.compile(r"(?i:\bInternational\s+Baccalaureate\b)|\bIB\s+Diploma\b")),
    ("Gaokao", re.compile(r"\bgaokao\b|\bNational\s+College\s+Entrance", re.I)),
    ("AP", re.compile(r"\bAdvanced\s+Placement\b", re.I)),
)


#: A construction immediately preceded by one of these is about who the rule does NOT
#: apply to. "Students who are non-US citizens attending secondary schools outside the
#: US are not typically eligible for fee waivers" is a real sentence from the corpus,
#: and reading a United States scope off it -- with `unresolved_reason` NULL, so
#: *resolved* -- is the same defect as reading one off a postal address.
#:
#: Negation is detected, not interpreted: the marker is dropped rather than inverted,
#: because "not China" is not a scope and guessing which jurisdictions it leaves would
#: be an inference the page never made.
_NEGATED_BEFORE = re.compile(
    r"(?:\bnon|\bnot|\bother\s+than|\bexcluding|\bexcept|\boutside|\bapart\s+from)[\s-]*$",
    re.I,
)

#: How far back to look for a negator. Long enough for "other than " and short enough
#: that an unrelated "not" earlier in the sentence cannot reach.
_NEGATION_WINDOW = 14


def _negated(text: str, start: int) -> bool:
    """Is the construction starting at `start` inside a negation?"""
    return bool(_NEGATED_BEFORE.search(text[max(0, start - _NEGATION_WINDOW) : start]))


def jurisdictions_in(text: str) -> list[dict[str, object]]:
    """Applicant jurisdictions the wording actually states (section 1).

    A country token alone asserts nothing. See the module docstring for the twelve real
    candidates that decided this, three of which were right.
    """
    found: list[dict[str, object]] = []
    for place, patterns in _JURISDICTION_RULES:
        entry: dict[str, object] | None = None
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                if _negated(text, match.start()):
                    continue
                entry = {
                    "scope_raw": match.group(0),
                    "scope_label": place.label,
                    "applicant_country_code": place.country_code,
                    "dimension": ScopeDimension.JURISDICTION.value,
                    "match_kind": kind,
                }
                break
            if entry is not None:
                break
        if entry is not None:
            found.append(entry)
    return found


def qualification_systems_in(text: str) -> list[dict[str, object]]:
    """Qualification systems the wording names. Says nothing about jurisdiction."""
    found: list[dict[str, object]] = []
    for label, pattern in _QUALIFICATION_SYSTEMS:
        match = pattern.search(text)
        if match:
            found.append(
                {
                    "scope_raw": match.group(0),
                    "scope_label": label,
                    "applicant_country_code": None,
                    "dimension": ScopeDimension.QUALIFICATION_SYSTEM.value,
                    "match_kind": "QUALIFICATION_SYSTEM",
                }
            )
    return found


def scopes_in(text: str) -> list[dict[str, object]]:
    """Every scope marker, both dimensions, raw wording preserved (section 10).

    Kept as the combined list because the grouping key, the quality cascade and the
    review-time scope proposal all read it. The dimensions are on each entry and are
    also surfaced separately on the candidate, which is what section 2 requires -- the
    combined list is a convenience, not the taxonomy.
    """
    return jurisdictions_in(text) + qualification_systems_in(text)


_QUALIFICATION_HINTS = re.compile(
    r"\b(?:bachelor(?:'|’)?s?\s+degree|first\s+degree|honours\s+degree|"
    r"2:1|2\.1|upper\s+second|second\s+class|GPA\s*(?:of\s*)?\d(?:\.\d+)?|"
    r"A[-\s]?levels?|IB\s+Diploma|high\s+school\s+diploma)\b",
    re.I,
)

_REQUIREMENT_WORD = re.compile(
    r"\brequire(?:s|d|ment)?\b|\bmust\s+(?:have|hold|demonstrate)\b|\bapplicants?\s+"
    r"(?:should|must|need)\b|\bentry\s+requirement|\badmission\s+requirement"
    r"|\bminimum\s+(?:qualification|grade|GPA)\b|\bwe\s+expect\b|\beligib",
    re.I,
)
_REQUIREMENT_HEADING = re.compile(
    r"requirement|entry|admission|eligib|qualification|academic\s+background", re.I
)

_MIN_REQUIREMENT_CHARS = 25


def states_requirement(text: str) -> bool:
    """Does this wording state a requirement, rather than merely sit near one?

    Public because Step 5C.3's quality classification needs exactly the signal the rule
    itself used, and re-implementing it there would let the two drift: a classification
    that disagreed with the extractor about what a requirement sentence is would sort
    candidates into buckets the bands contradict.
    """
    return bool(_REQUIREMENT_WORD.search(text))


def degree_level_of(text: str) -> tuple[str | None, str | None, str]:
    """(level, matched wording, reason). A null level is a valid answer.

    Ordered doctorate → master → bachelor because a PhD page often mentions the
    master's it requires, and the page's own subject is the more specific award.
    """
    for level, pattern in _DEGREE_LEVELS:
        match = pattern.search(text)
        if match:
            return level, match.group(0), f"unambiguous award name {match.group(0)!r}"
    ambiguous = AMBIGUOUS_LEVEL_WORDS.search(text)
    if ambiguous:
        return (
            None,
            ambiguous.group(0),
            f"{ambiguous.group(0)!r} does not identify a level; "
            "'graduate' is not a doctorate and 'advanced' is not a level",
        )
    return None, None, "no award name present"


def discipline_of(text: str) -> tuple[str | None, str | None]:
    for code, pattern in _DISCIPLINES:
        match = pattern.search(text)
        if match:
            return code, match.group(0)
    return None, None


def extract(document: object, *, responsibility: str) -> list[Candidate]:
    """Admission-requirement candidates."""
    from app.domains.extraction.document import BlockKind

    blocks = getattr(document, "blocks", [])
    out: list[Candidate] = []

    #: Per block, the texts of anchors that are the whole of their own list item. Keyed
    #: by block so a label only suppresses the item it actually sits in -- a document-
    #: wide set of link texts would suppress any paragraph that happened to read the
    #: same as some link elsewhere on the page.
    link_only_items: dict[int, set[str]] = {}
    for link in getattr(document, "links", []):
        if not link.is_link_only_item or link.block_index is None:
            continue
        text = (link.text or "").strip()
        if text:
            link_only_items.setdefault(link.block_index, set()).add(text)

    for index, block in enumerate(blocks):
        if is_chrome(block) or getattr(block, "in_chrome", False):
            # Site furniture, not the page's answer. `in_chrome` is the sticky form and
            # `is_chrome` alone is no longer sufficient: document v2 added `section` to
            # the container list, so a `<section>` inside a `<nav>` now reports
            # `section` and a container test would wave it through.
            continue
        heading_path = heading_path_for(blocks, index)
        under_heading = any(_REQUIREMENT_HEADING.search(part) for part in heading_path)
        profile = getattr(block, "link_profile", None)
        item_labels = link_only_items.get(index, set[str]())

        units: list[tuple[str, Locator, bool]] = []
        if block.kind is BlockKind.LIST:
            for item_index, item in enumerate(block.items):
                units.append(
                    (
                        item,
                        Locator(
                            kind="list_item",
                            block_index=index,
                            list_item_index=item_index,
                            heading_path=heading_path,
                        ),
                        item.strip() in item_labels,
                    )
                )
        elif block.kind in (BlockKind.PARAGRAPH, BlockKind.QUOTE) and block.text:
            units.append(
                (
                    block.text,
                    Locator(kind="block", block_index=index, heading_path=heading_path),
                    bool(profile and profile.is_link_only),
                )
            )

        for text, locator, link_only in units:
            if len(text) < _MIN_REQUIREMENT_CHARS:
                continue
            if not (_REQUIREMENT_WORD.search(text) or under_heading):
                continue
            if link_only:
                # The whole unit is one anchor's text. "International entry
                # requirements" is a link *to* the requirements, not a requirement --
                # 144 LOW rows and 2 MEDIUM ones were exactly this. A catalogue's
                # programme link is the legitimate mirror image, which is why the test
                # lives in this rule and not in the parser (section 3).
                continue

            states_requirement = bool(_REQUIREMENT_WORD.search(text))
            jurisdictions = jurisdictions_in(text)
            qualifications = qualification_systems_in(text)
            scopes = jurisdictions + qualifications
            qualification = _QUALIFICATION_HINTS.search(text)
            # The wording plus the INNERMOST heading only. Reading the whole path let a
            # page titled "Graduate Admissions" stamp `degree_level_raw='Graduate'` on
            # every paragraph beneath it, testimonials included -- a wrong value on the
            # row rather than a recall trade-off.
            nearest = heading_path[-1] if heading_path else ""
            level, level_raw, _ = degree_level_of(f"{nearest} {text}")

            value: dict[str, object] = {
                "requirement_text": text[:1000],
                "applicant_scopes": scopes or None,
                # Section 2: the two dimensions stay separate, so the taxonomy can be
                # decided later without either having been flattened into the other.
                "applicant_jurisdictions": jurisdictions or None,
                "qualification_systems": qualifications or None,
                "qualification_hint": qualification.group(0) if qualification else None,
                "degree_level_hint": level,
                "degree_level_raw": level_raw,
            }
            # Never UNIVERSAL (section 1). No marker at all is one situation; a
            # qualification hint with no jurisdiction is a different and more specific
            # one, and a single flag covering both would hide which.
            if jurisdictions:
                unresolved = None
            elif qualifications:
                unresolved = "APPLICANT_JURISDICTION_UNRESOLVED"
            else:
                unresolved = "SCOPE_MAPPING_REQUIRED"
            out.append(
                Candidate(
                    field_kind=FieldKind.ADMISSION_REQUIREMENT,
                    value=value,
                    value_raw_text=text[:300],
                    evidence_text=text,
                    locator=locator,
                    # MEDIUM needs the wording itself to state a requirement. An
                    # ancestor heading alone admits every paragraph on an admissions
                    # page -- a dean's byline, a news blurb about a journalism trip,
                    # "students from all walks of life come together" -- all of which
                    # the audit found at MEDIUM. They remain candidates, because a
                    # reviewer with the sentence in front of them can dismiss one in a
                    # second; what they must not be is indistinguishable from
                    # "applicants must hold a bachelor's degree".
                    confidence=(
                        Confidence.MEDIUM
                        if states_requirement and under_heading
                        else Confidence.LOW
                    ),
                    confidence_reason=(
                        (
                            "requirement wording under a requirements heading"
                            if states_requirement and under_heading
                            else "requirement wording with no matching heading above it"
                            if states_requirement
                            else "prose under a requirements heading that does not "
                            "itself state a requirement -- the heading is the only "
                            "thing making this a requirement candidate"
                        )
                        + (
                            ""
                            if scopes
                            else "; no explicit applicant scope, so scope is UNRESOLVED "
                            "rather than universal"
                        )
                    ),
                    unresolved_reason=unresolved,
                    context={"responsibility": responsibility},
                )
            )
    return out


#: Responsibilities that authorise a programme name to be read from a link. Routing
#: already restricts the `program` extractor to these, and section 6 asks for the check
#: to be explicit on the rule rather than implicit in the routing table.
PROGRAM_LINK_RESPONSIBILITIES = frozenset({"PROGRAM_CATALOG", "PROGRAM_PAGE"})


def _body_origin(container: str | None, in_chrome: bool) -> tuple[bool, str]:
    """Is this structural origin the page's own content? (verdict, reason.)

    Section 6 lists header, footer, nav, site-wide A-Z menus, generic course pickers and
    unresolved structural origin as things to reject or quarantine. The first three are
    `in_chrome`; the rest are "the parser could not place this in a body container",
    which covers the 6,223 links in no semantic container at all.
    """
    if in_chrome:
        return False, f"inside site chrome ({container or 'unlabelled'})"
    if container in BODY_CONTAINERS:
        return True, f"body content ({container})"
    if container is None:
        return False, "structural origin unresolved: no semantic container"
    return False, f"not body content ({container})"


def extract_program(document: object, *, responsibility: str) -> list[Candidate]:
    """Programme candidates from a catalogue or programme page (sections 6, 9).

    Conservative by design: a catalogue's programme list is usually links or headings,
    and a heading is only a programme name when it carries an award. "Our courses" is
    a heading; "MSc Computer Science" is a programme.

    **A candidate is created only from body content.** See `_body_origin`.
    """
    from app.domains.extraction.document import BlockKind

    blocks = getattr(document, "blocks", [])
    out: list[Candidate] = []
    seen: set[str] = set()

    # Headings that name an award.
    for index, block in enumerate(blocks):
        if block.kind is not BlockKind.HEADING or not block.text or is_chrome(block):
            continue
        is_body, origin = _body_origin(
            getattr(block, "container", None), getattr(block, "in_chrome", False)
        )
        if not is_body:
            # Cambridge's A-Z picker headings live here: "Courses for 2027 entry >>
            # Vertical menu >> A" sits inside a `<nav>`, and v4 read all 30 of them.
            continue
        level, level_raw, level_reason = degree_level_of(block.text)
        if level is None and level_raw is None:
            continue
        name = block.text.strip()
        if name.lower() in seen or len(name) > 200:
            continue
        seen.add(name.lower())
        locator = Locator(
            kind="block", block_index=index, heading_path=heading_path_for(blocks, index)
        )
        out.extend(
            _program_candidates(
                name,
                locator,
                level,
                level_raw,
                level_reason,
                responsibility,
                Confidence.MEDIUM,
                f"a heading naming an award, in {origin}",
            )
        )

    # Links whose text names an award: how most catalogues actually list programmes.
    if responsibility not in PROGRAM_LINK_RESPONSIBILITIES:
        return out

    for position, link in enumerate(getattr(document, "links", [])):
        text = (link.text or "").strip()
        if not text or len(text) > 200 or text.lower() in seen:
            continue
        is_body, origin = _body_origin(
            getattr(link, "container", None), getattr(link, "in_chrome", False)
        )
        if not is_body:
            continue
        level, level_raw, level_reason = degree_level_of(text)
        if level is None and level_raw is None:
            continue
        seen.add(text.lower())
        out.extend(
            _program_candidates(
                text,
                Locator(kind="link", link_index=position, block_index=link.block_index),
                level,
                level_raw,
                level_reason,
                responsibility,
                Confidence.LOW,
                f"link text naming an award in {origin}",
            )
        )
    return out


def _program_candidates(
    name: str,
    locator: Locator,
    level: str | None,
    level_raw: str | None,
    level_reason: str,
    responsibility: str,
    band: Confidence,
    reason: str,
) -> list[Candidate]:
    discipline, discipline_raw = discipline_of(name)
    out = [
        Candidate(
            field_kind=FieldKind.PROGRAM_NAME,
            value={"program_name": name},
            value_raw_text=name,
            evidence_text=name,
            locator=locator,
            confidence=band,
            confidence_reason=reason,
            context={"responsibility": responsibility},
        ),
        Candidate(
            field_kind=FieldKind.DEGREE_LEVEL,
            # A null level with its raw label is the honest answer for an ambiguous
            # award, and the claim stays available for review (section 7).
            value={"degree_level": level, "raw_label": level_raw} if level else None,
            value_raw_text=level_raw or name,
            evidence_text=name,
            locator=locator,
            confidence=band if level else Confidence.LOW,
            confidence_reason=level_reason,
            unresolved_reason=None if level else "DEGREE_LEVEL_AMBIGUOUS",
            context={"responsibility": responsibility, "program_name": name},
        ),
    ]
    if discipline:
        out.append(
            Candidate(
                field_kind=FieldKind.DISCIPLINE_HINT,
                value={"discipline": discipline, "matched": discipline_raw},
                value_raw_text=discipline_raw or name,
                evidence_text=name,
                locator=locator,
                confidence=Confidence.LOW,
                confidence_reason=(
                    f"the programme name contains {discipline_raw!r}; a name is a hint "
                    "about discipline, not a classification"
                ),
                context={"responsibility": responsibility, "program_name": name},
            )
        )
    duration = re.search(
        r"\b(?P<count>\d+(?:\.\d+)?|one|two|three|four|five|six)\s*[-\s]?"
        r"(?P<unit>year|years|month|months|semester|semesters)\b",
        name,
        re.I,
    )
    if duration:
        out.append(
            Candidate(
                field_kind=FieldKind.DURATION,
                value={"duration_text": duration.group(0)},
                value_raw_text=duration.group(0),
                evidence_text=name,
                locator=locator,
                confidence=Confidence.LOW,
                confidence_reason="a duration stated inside the programme name",
                context={"responsibility": responsibility, "program_name": name},
            )
        )
    mode = re.search(r"\b(full[-\s]?time|part[-\s]?time|online|distance\s+learning)\b", name, re.I)
    if mode:
        out.append(
            Candidate(
                field_kind=FieldKind.STUDY_MODE,
                value={"study_mode_raw": mode.group(0)},
                value_raw_text=mode.group(0),
                evidence_text=name,
                locator=locator,
                confidence=Confidence.LOW,
                confidence_reason="a study mode stated inside the programme name",
                context={"responsibility": responsibility, "program_name": name},
            )
        )
    return out


__all__ = [
    "AMBIGUOUS_LEVEL_WORDS",
    "EXTRACTOR",
    "PROGRAM_EXTRACTOR",
    "PROGRAM_LINK_RESPONSIBILITIES",
    "PROGRAM_VERSION",
    "VERSION",
    "ScopeDimension",
    "degree_level_of",
    "discipline_of",
    "extract",
    "extract_program",
    "jurisdictions_in",
    "qualification_systems_in",
    "scopes_in",
    "states_requirement",
]
