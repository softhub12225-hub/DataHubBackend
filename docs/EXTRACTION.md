Written for: whoever builds Step 5C.2's field-level extraction, and whoever has to
explain later where a published number came from.

# Document normalisation

Raw immutable evidence → deterministic document extraction → a versioned normalised
document representation.

**Not** raw page → university fact. Nothing here creates a `field_claim`, a tuition, a
deadline or a requirement; that is Step 5C.2, and doing it here would skip the review the
whole system exists to enforce.

---

## 1. The trust boundary is unchanged

Extraction reads evidence and writes a derived document. It writes nothing to
`publication_eligibility`, `official_domain`, `source_mapping` or any pilot verification
state.

> A document parsed perfectly from a `NOT_ELIGIBLE` source is a perfectly parsed
> document from a source nobody may publish. **Successful parsing earns nothing.**

Asserted over the tables rather than by reading the code, because the code is what would
change: after extracting all 175 real documents, `publication_eligibility` moved for zero
sources, no domain became verified, no mapping was promoted, and eleven canonical and
governance tables are still empty.

---

## 2. Where it lives

No new table. `extraction` already existed with `snapshot_id`, `extractor_name`,
`extractor_version`, `status`, `recorded_at`, an append-only trigger, and
`field_claim.extraction_id` pointing at it — which is exactly the lineage Step 5C.2
needs. Re-modelling it would have broken the one invariant this step had to preserve.

So revision `c8d9e0f1a2b3` is five columns and three constraints:

| Column | Why |
|---|---|
| `input_content_hash` | the bytes parsed, denormalised from the snapshot so "same bytes, same version" needs no join |
| `document_hash` | sha256 of the canonical derived document |
| `document_storage_key` | where the payload lives |
| `document_byte_size` | how big it is |
| `warnings` | why a `PARTIAL` is partial — distinct from `error_detail`, which explains a `FAILED` |

Plus `UNIQUE (snapshot_id, extractor_name, extractor_version)`, a sha256-shape CHECK on
both hashes, and a CHECK that a stored document is *completely* described — hash, key and
size together or none of them. That last one is C12's dangling-reference failure, one
plane further down.

---

## 3. The storage boundary

**Metadata in PostgreSQL, payload in the object store.**

`extraction.output` is `jsonb` and could have held the documents. For 174 real pages
they come to 11 MB of data that is *reproducible from bytes we already store*, and
putting it in PostgreSQL means every backup, every replica and every `pg_dump` carries
it. `output` keeps a summary — title, language, statistics, `@type` list, charset
decision — which is what it is good at.

Derived artifacts live under a `derived/` key prefix; raw evidence is under `evidence/`.
Separate prefixes make "extraction never overwrites raw evidence" a structural property
rather than a convention: the two could only collide by sharing a prefix, and a test puts
both stores in one directory to prove they do not.

**Object first, then the transaction** (D36). An orphaned artifact is wasted bytes; a row
pointing at an artifact that was never written is a document we claim to have and cannot
produce.

---

## 4. Determinism

`canonical_bytes()` serialises a document to exactly one byte sequence: sorted keys,
tight separators, UTF-8. Its sha256 is the artifact hash.

The artifact therefore contains **no ids and no timestamps**. Including them would make
the hash a function of *when and where* extraction ran rather than *what was parsed*, and
the determinism test would be untestable. A test asserts both halves: the same bytes
produce the same hash, and the artifact is over 500 bytes and contains no `snapshot_id`,
no `recorded_at` and no year — so determinism is not being achieved by emptiness.

Lineage is not lost. It lives on the row, which is where a join can follow it.

One consequence, stated rather than discovered: two sources serving byte-identical pages
produce **one** artifact. In the real fleet exactly that happened — 175 extractions,
174 distinct artifacts.

---

## 5. Idempotency

One result per `(snapshot, extractor, version)`. A second pass over the same snapshot at
the same version does nothing: the result is a pure function of its inputs, so another
row could only be a duplicate. There is no separate "run record" — an extraction *is* the
result, and re-deriving it is not an event worth storing.

Proved on the real fleet: the second pass reported `175 already extracted at this
version, 0 attempted`.

Changed logic gets a **new version** and a new row beside the old one, which is retained
because the table is append-only. Comparing two versions over one snapshot is how
extractor drift becomes visible instead of being mistaken for a source change (D4).

The cost, named: a `FAILED` extraction caused by something environmental cannot be
retried under the same version. The fix is a version bump, not an `UPDATE`.

---

## 6. What is extracted

Only body-bearing evidence. A `BLOCKED`, `404`, TLS-failed, timed-out or unresolved page
has no stored bytes, so there is nothing to parse and no row is created — an extraction
over nothing would assert we read a document that does not exist. A `304` resolves to the
snapshot that actually carried bytes, so an unchanged page extracts the body it last
served rather than a manufactured one.

---

## 7. HTML normalisation

`lxml` with `recover=True`, because real university HTML is unclosed and mis-nested and
refusing it would lose the fleet.

The document is **blocks in document order**, not one flattened string: "the fee table's
third row" and "the paragraph after the Entry Requirements heading" are both things Step
5C.2 needs to be able to say, and a text blob can say neither. Headings carry their
level, lists their items, tables their rows and cells and spans, links their anchor text
and resolved target.

### Conservative about navigation

`script` and `style` are dropped, because executable code is not content. `nav`,
`header`, `footer` and `aside` are **kept** — universities put fee tables in asides and
application deadlines in footers, and a parser that deleted them because a CSS class
looked like navigation would silently lose the thing we came for.

Instead of deleting, every block records the `container` it came from. A later extractor
that wants to prefer the main column can; the decision belongs to the step that knows
what it is looking for.

### Text

Whitespace collapsed, Unicode NFC-normalised, nothing else. No translation, no summary,
no rewriting, no number turned into a value. Official wording survives to the character,
because a reviewer comparing our claim against the page has to be able to find the
sentence.

---

## 8. Character encoding

A fixed ladder, not a detection library — `charset-normalizer` and `chardet` guess, and
they guess differently between versions, which would make the artifact hash depend on a
dependency's heuristics.

1. the HTTP `Content-Type` charset, because the server stated it about *these* bytes;
2. the document's own declaration — BOM, then `<meta charset>`, then `http-equiv`;
3. UTF-8;
4. `cp1252`, which decodes every byte value.

Step 4 always succeeds, so there is no undecodable outcome — but arriving there is
recorded as a fallback, and replacement characters are counted. A page that decoded badly
is visible rather than silently mangled, and a fallback makes the extraction `PARTIAL`.

**Raw bytes are never rewritten.** Any decoding decision can be revisited by a new
extractor version over the same bytes.

Across the real fleet: 172 documents decoded as declared UTF-8, one as `ISO-8859-15`, one
needed the fallback, and **zero** carried an undecodable character.

---

## 9. Structured data

JSON-LD is parsed in all three legal shapes — a single object, an array, and `@graph`.
A malformed block is a **warning**, never a failure: refusing a page's 40,000 characters
of real text over a trailing comma would be the wrong trade, and that is precisely what
`PARTIAL` is for.

> Present is not the same as true. JSON-LD is a claim the page makes in a convenient
> format, not a more authoritative version of the page. Nothing here becomes a
> `field_claim`.

Embedded JSON is handled only where it is *safely identifiable*: `application/json`
script blocks and the exact ids `__NEXT_DATA__` / `__NUXT_DATA__`. Matching on an exact
id rather than a pattern is deliberate — a heuristic over arbitrary script bodies is how
a parser turns into a JavaScript interpreter. Everything else is inventoried by type and
left alone.

---

## 10. PDF

`pypdf`, not PyMuPDF, which the instruction suggested. PyMuPDF gives better block and
reading-order data and is **AGPL-3.0**; a licence decision on a commercial internal
product belongs to the client, not to a silent import. The whole fleet contains one
registered PDF, so pypdf (BSD-3) is proportionate: text layer, info dictionary, link
annotations. Swapping it later is a contained change, because everything downstream
consumes `NormalizedDocument` rather than pypdf.

**No OCR.** A PDF with no text layer is marked `OCR_REQUIRED` and left alone — OCR
guesses, and a guessed tuition figure is worse than a missing one.

One block per page, so "page 3 of the academic calendar" stays sayable.

---

## 11. Security

Parsing stored evidence executes nothing and fetches nothing: no JavaScript, no external
resource, no iframe followed, no PDF action run, no LLM.

`test_the_parser_makes_no_network_call` holds a socket-level guard — `connect`,
`connect_ex`, `create_connection` and `getaddrinfo` all raise — over a document
containing an external DTD, a remote stylesheet, an iframe and an image. Proved by
removing the ability, not by reading the code.

---

## 12. Commands

```bash
make extract-documents              # extract every body-bearing source (offline)
make extract-report                 # totals, coverage, charsets, the PDF
make extract-jsonld                 # the @type inventory
make extract-thin                   # low-text pages: is a browser needed?
```

Manual commands rather than CI jobs: the automated suite is fixture-based, and turning
174 universities' markup into a test dependency would make it fail when they redesign
their sites.
