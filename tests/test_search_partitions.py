from __future__ import annotations

from kbimporter.cli import main


class _FakeClient:
    def __init__(self):
        self.loaded = []
        self.released = []
        self.searched = []
        self.queried = []

    def has_collection(self, collection_name):
        return True

    def load_partitions(self, collection_name, partition_names, **k):
        self.loaded.append(list(partition_names))

    def load_collection(self, collection_name, **k):
        self.loaded.append(["__ALL__"])

    def release_partitions(self, collection_name, partition_names, **k):
        self.released.append(list(partition_names))

    def release_collection(self, collection_name, **k):
        self.released.append(["__ALL__"])

    def search(self, collection_name, **kwargs):
        self.searched.append(kwargs)
        return [[]]

    def query(self, collection_name, **kwargs):
        self.queried.append(kwargs)
        return []

    def list_collections(self, **k):
        return ["academic_library"]


def test_search_year_range_loads_only_partitions(tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: fake)
    rc = main([
        "search", "--collection", "academic_library", "--kind", "dense",
        "村干部", "--year-from", "2015", "--year-to", "2020",
        "--release", "--config", "kb_config.toml",
    ])
    assert rc == 0
    assert fake.loaded == [["en_2010s", "en_2020s", "zh_2010s", "zh_2020s"]]
    assert fake.searched and fake.searched[0].get("partition_names") == [
        "en_2010s", "en_2020s", "zh_2010s", "zh_2020s",
    ]
    assert fake.released == [["en_2010s", "en_2020s", "zh_2010s", "zh_2020s"]]


def test_search_explicit_partitions(tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: fake)
    rc = main([
        "search", "--collection", "academic_library", "--kind", "dense",
        "村民自治", "--partitions", "zh_2010s,zh_2020s",
        "--config", "kb_config.toml",
    ])
    assert rc == 0
    assert fake.loaded == [["zh_2010s", "zh_2020s"]]
    assert fake.released == []  # 未加 --release


def test_search_without_year_loads_whole_collection(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: fake)
    rc = main([
        "search", "--collection", "academic_library", "--kind", "dense",
        "村干部", "--config", "kb_config.toml",
    ])
    assert rc == 0
    assert fake.loaded == [["__ALL__"]]


def test_release_partitions_only(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: fake)
    rc = main([
        "release", "academic_library", "--partitions", "zh_2010s",
        "--config", "kb_config.toml",
    ])
    assert rc == 0
    assert fake.released == [["zh_2010s"]]
