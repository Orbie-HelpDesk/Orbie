import pytest

from app import ingest
from app.config import settings
from fake_kb_db import FakeEmbedder, FakeKbDb

ARTICLE = "# VPN troubleshooting\n\nRestart the VPN client.\n\nCheck your network."


@pytest.fixture
def env(monkeypatch, tmp_path):
    db, embedder = FakeKbDb(), FakeEmbedder()
    monkeypatch.setattr(ingest, "engine", db)
    monkeypatch.setattr(ingest, "client", embedder)
    monkeypatch.setattr(settings, "kb_dir", str(tmp_path))
    monkeypatch.setattr(settings, "embed_dimensions", 4)
    return db, embedder, tmp_path


# ---------- pieces ----------
def test_chunking_keeps_paragraphs_together_and_is_unchanged():
    assert ingest.chunk_text("a\n\nb\n\nc") == ["a\n\nb\n\nc"]
    big = "\n\n".join(["x" * 500, "y" * 500, "z" * 100])
    assert ingest.chunk_text(big) == ["x" * 500, "y" * 500 + "\n\n" + "z" * 100]
    assert ingest.chunk_text("   \n\n  ") == []


@pytest.mark.parametrize(
    "content, fallback, expected",
    [("# VPN troubleshooting\nbody", "vpn", "VPN troubleshooting"), ("## Sub\nbody", "x", "Sub"), ("\nbody", "fallback", "fallback"), ("", "fallback", "fallback"), ("#   \nbody", "fb", "fb")],
)
def test_the_title_is_the_first_line_or_the_file_name(content, fallback, expected):
    assert ingest.title_of(content, fallback) == expected


def test_embedding_is_sent_in_batches_and_keeps_the_order(env):
    _, embedder, _ = env
    texts = [f"text {i}" for i in range(120)]
    vectors = ingest.embed(texts)
    assert [len(c["input"]) for c in embedder.calls] == [50, 50, 20]
    assert len(vectors) == 120 and all(c["dimensions"] == 4 for c in embedder.calls)
    assert vectors[0] == [float(len("text 0") % 7)] * 4 and vectors[119] == [float(len("text 119") % 7)] * 4


def test_an_embedding_failure_surfaces_as_the_provider_error(env):
    from app import llm
    from test_llm import unavailable

    _, embedder, _ = env
    embedder.error = unavailable()
    with pytest.raises(llm.ProviderUnavailable):
        ingest.embed(["x"])


def test_prepare_article_chunks_and_embeds_without_touching_the_database(env):
    db, embedder, _ = env
    prepared = ingest.prepare_article("vpn.md", ARTICLE)
    assert (prepared.source, prepared.title) == ("vpn.md", "VPN troubleshooting")
    assert len(prepared.chunks) == len(prepared.vectors) == 1
    assert embedder.calls[0]["input"] == ["VPN troubleshooting\n" + prepared.chunks[0]]  # the title is part of what is embedded
    assert db.docs == {} and db.commits == 0


def test_store_article_replaces_the_older_version(env):
    db, _, _ = env
    with db.begin() as conn:
        ingest.store_article(conn, ingest.prepare_article("vpn.md", ARTICLE))
    with db.begin() as conn:
        ingest.store_article(conn, ingest.prepare_article("vpn.md", "# New title\n\nOnly this."))
    assert db.sources() == ["vpn.md"]
    (doc,) = db.docs.values()
    assert doc["title"] == "New title" and doc["chunk_count"] == 1
    assert [c["content"] for c in db.chunks_of("vpn.md")] == ["# New title\n\nOnly this."]
    assert db.chunks_of("vpn.md")[0]["metadata"] == {"title": "New title", "source": "vpn.md"}


def test_remove_article_deletes_its_chunks_and_counts(env):
    db, _, _ = env
    db.add_doc("a.md")
    db.add_doc("b.md")
    with db.begin() as conn:
        assert ingest.remove_article(conn, "a.md") == 1
        assert ingest.remove_article(conn, "missing.md") == 0
    assert db.sources() == ["b.md"] and db.chunks_of("a.md") == []


# ---------- the script ----------
def test_the_script_indexes_every_article_with_the_shared_functions(env, capsys):
    db, embedder, folder = env
    (folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    (folder / "wifi.md").write_text("# Wi-Fi\n\nCheck the router.", encoding="utf-8")
    (folder / "notes.txt").write_text("not markdown", encoding="utf-8")
    ingest.ingest()
    assert db.sources() == ["vpn.md", "wifi.md"]
    assert len(embedder.calls) == 2 and db.commits == 1  # one transaction for the whole run, as before
    out = capsys.readouterr().out
    assert "Indexed vpn.md: 1 chunks" in out and "Indexed wifi.md: 1 chunks" in out


def test_the_script_replaces_what_was_indexed_before(env):
    db, _, folder = env
    db.add_doc("vpn.md", title="Old", chunks=5)
    (folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    ingest.ingest()
    assert [d["title"] for d in db.docs.values()] == ["VPN troubleshooting"] and len(db.chunks_of("vpn.md")) == 1


def test_the_script_says_so_when_there_is_nothing_to_index(env, capsys):
    db, embedder, folder = env
    ingest.ingest()
    assert f"No .md files found in {folder}" in capsys.readouterr().out
    assert embedder.calls == [] and db.commits == 0


def test_a_failing_embedder_leaves_the_index_unchanged(env):
    from app import llm
    from test_llm import unavailable

    db, embedder, folder = env
    db.add_doc("old.md")
    (folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    embedder.error = unavailable()
    with pytest.raises(llm.ProviderUnavailable):
        ingest.ingest()
    assert db.sources() == ["old.md"]
