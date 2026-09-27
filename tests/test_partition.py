from kbimporter.partition import (
    ACADEMIC_PARTITIONS,
    academic_partition_name,
    normalize_lang,
    partitions_for_year_range,
    year_bucket,
)


def test_year_bucket_edges():
    assert year_bucket(0) == "unknown"
    assert year_bucket(-1) == "unknown"
    assert year_bucket(1979) == "pre1980"
    assert year_bucket(1980) == "1980s"
    assert year_bucket(1995) == "1990s"
    assert year_bucket(2005) == "2000s"
    assert year_bucket(2015) == "2010s"
    assert year_bucket(2024) == "2020s"
    assert year_bucket(2030) == "2020s"
    assert year_bucket(2099) == "2020s"


def test_normalize_lang():
    assert normalize_lang("zh") == "zh"
    assert normalize_lang("EN") == "en"
    assert normalize_lang("") == "zh"
    assert normalize_lang("fr") == "zh"


def test_academic_partition_name():
    assert academic_partition_name("zh", 2015) == "zh_2010s"
    assert academic_partition_name("en", 0) == "en_unknown"
    assert academic_partition_name("EN", 1991) == "en_1990s"


def test_all_partitions_unique_and_cover_languages():
    assert len(ACADEMIC_PARTITIONS) == 14
    assert "zh_2010s" in ACADEMIC_PARTITIONS
    assert "en_unknown" in ACADEMIC_PARTITIONS


def test_partitions_for_year_range():
    assert partitions_for_year_range(2015, 2020) == [
        "en_2010s", "en_2020s", "zh_2010s", "zh_2020s",
    ]
    assert partitions_for_year_range(2015, 2020, language="zh") == [
        "zh_2010s", "zh_2020s",
    ]
    assert partitions_for_year_range(None, None) == ACADEMIC_PARTITIONS


def test_partitions_for_year_range_includes_open_ended_2020s():
    assert partitions_for_year_range(2030, 2040) == ["en_2020s", "zh_2020s"]
    assert "zh_2020s" in partitions_for_year_range(2025, 2035)


def test_year_bucket_stays_in_closed_set():
    from kbimporter.partition import _BUCKETS
    for y in range(-5, 5001):
        assert year_bucket(y) in _BUCKETS
        name = academic_partition_name("zh", y)
        assert name in ACADEMIC_PARTITIONS
        assert academic_partition_name("en", y) in ACADEMIC_PARTITIONS
