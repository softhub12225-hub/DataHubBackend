"""Mapping the client's region labels onto the platform's `destination` vocabulary.

This is a *translation table*, not a source of facts. The client writes 英国; the
database keys destinations by ISO 3166 alpha-2. Both name the same place, and
recording the correspondence explicitly is what lets an unrecognised label fail
loudly instead of being guessed.

Two independent label columns are mapped, because the QS workbook supplies both a
Chinese region and a QS "Country/Territory" string, and either may be the one a
future file uses. When both are present and disagree, the importer reports it rather
than choosing -- a disagreement means the file is inconsistent, which is the
operator's problem to resolve, not ours to paper over.

An unmapped label is never silently dropped and never approximated to a neighbour.
The affected target is imported with `destination_code = NULL` and moved to
`NEEDS_MANUAL_REVIEW`, and the import report names the label. Adding a destination
is a seed change; adding a synonym is a change here.
"""

from __future__ import annotations

#: Chinese region label -> destination code. Keys are exactly the strings the
#: client's spreadsheets use, including the politically specific 中国香港/中国澳门
#: forms, which is how the supplied QS 2027 file writes them.
CHINESE_REGION_TO_DESTINATION: dict[str, str] = {
    "英国": "GB",
    "美国": "US",
    "澳大利亚": "AU",
    "加拿大": "CA",
    "新加坡": "SG",
    "新西兰": "NZ",
    "中国香港": "HK",
    "香港": "HK",
    "中国澳门": "MO",
    "澳门": "MO",
    "爱尔兰": "IE",
    "马来西亚": "MY",
}

#: QS "Country/Territory" label -> destination code. QS uses its own forms
#: ("United States of America", "Hong Kong SAR, China"); they are recorded verbatim
#: as keys rather than guessed at by prefix matching.
QS_TERRITORY_TO_DESTINATION: dict[str, str] = {
    "United Kingdom": "GB",
    "United States of America": "US",
    "United States": "US",
    "Australia": "AU",
    "Canada": "CA",
    "Singapore": "SG",
    "New Zealand": "NZ",
    "Hong Kong SAR, China": "HK",
    "Hong Kong SAR": "HK",
    "Macao SAR, China": "MO",
    "Macau SAR, China": "MO",
    "Ireland": "IE",
    "Malaysia": "MY",
}


def resolve_destination(
    region_label: str | None, country_territory: str | None
) -> tuple[str | None, str | None]:
    """Resolve a destination code from either label.

    Returns ``(destination_code, problem)``. Exactly one is non-None:

    * a code, when the labels agree or only one is recognised;
    * a problem description, when neither is recognised or the two disagree.

    Disagreement is reported rather than resolved by precedence. If a file says
    ``英国`` and ``Australia`` on the same row, one of them is wrong and the
    importer must not decide which.
    """
    from_region = CHINESE_REGION_TO_DESTINATION.get((region_label or "").strip())
    from_territory = QS_TERRITORY_TO_DESTINATION.get((country_territory or "").strip())

    if from_region and from_territory and from_region != from_territory:
        return None, (
            f"region {region_label!r} maps to {from_region} but "
            f"country/territory {country_territory!r} maps to {from_territory}"
        )
    resolved = from_region or from_territory
    if resolved is None:
        unknown = [
            repr(label) for label in (region_label, country_territory) if (label or "").strip()
        ]
        detail = " / ".join(unknown) if unknown else "no region given"
        return None, f"unrecognised region: {detail}"
    return resolved, None


__all__ = [
    "CHINESE_REGION_TO_DESTINATION",
    "QS_TERRITORY_TO_DESTINATION",
    "resolve_destination",
]
