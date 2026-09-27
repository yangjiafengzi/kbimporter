from __future__ import annotations

_LANGS = ("zh", "en")
_BUCKETS = ("pre1980", "1980s", "1990s", "2000s", "2010s", "2020s", "unknown")

ACADEMIC_PARTITIONS: list[str] = sorted(
    f"{lang}_{b}" for b in _BUCKETS for lang in _LANGS
)


def year_bucket(year: int) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "unknown"
    if y <= 0:
        return "unknown"
    if y < 1980:
        return "pre1980"
    return f"{(y // 10) * 10}s"


def normalize_lang(language: str | None) -> str:
    lang = (language or "").strip().lower()
    return lang if lang in _LANGS else "zh"


def academic_partition_name(language: str | None, year: int) -> str:
    return f"{normalize_lang(language)}_{year_bucket(year)}"


def partitions_for_year_range(
    year_from: int | None,
    year_to: int | None,
    language: str | None = None,
) -> list[str]:
    if year_from is None and year_to is None:
        names = list(ACADEMIC_PARTITIONS)
    else:
        lo = 0 if year_from is None else int(year_from)
        hi = 10 ** 9 if year_to is None else int(year_to)
        if lo > hi:
            lo, hi = hi, lo
        names = []
        for b in _BUCKETS:
            if b == "unknown":
                continue
            if b == "pre1980":
                start, end = 0, 1979
            else:
                start = int(b[:4])
                end = start + 9
            if end < lo or start > hi:
                continue
            names.extend(f"{lang}_{b}" for lang in _LANGS)
        names = sorted(set(names))
    if language:
        lang = normalize_lang(language)
        names = [n for n in names if n.startswith(f"{lang}_")]
    return names
