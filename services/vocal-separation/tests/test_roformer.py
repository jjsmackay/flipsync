import separator as sep


def test_bs_roformer_is_valid():
    assert "bs_roformer" in sep.VALID_MODELS


def test_family_detection():
    assert sep._is_roformer("bs_roformer") is True
    assert sep._is_roformer("htdemucs") is False
    assert sep._is_roformer("htdemucs_ft") is False


def test_roformer_has_a_checkpoint_mapping():
    assert sep._ROFORMER_CKPT["bs_roformer"].endswith(".ckpt")


def test_separate_routes_roformer_to_roformer_impl(monkeypatch, tmp_path):
    called = {}

    def fake_roformer(input_path, output_path, model_name):
        called["args"] = (input_path, output_path, model_name)

    monkeypatch.setattr(sep, "_separate_roformer", fake_roformer)
    out = str(tmp_path / "vocals.wav")
    sep.separate("in.wav", out, model_name="bs_roformer")
    assert called["args"] == ("in.wav", out, "bs_roformer")


def test_separate_still_uses_demucs_for_htdemucs(monkeypatch, tmp_path):
    # Demucs path must NOT be routed to the roformer impl.
    monkeypatch.setattr(sep, "_separate_roformer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrong path")))
    monkeypatch.setattr(sep, "_load_model", lambda name: (_ for _ in ()).throw(RuntimeError("stop-after-route")))
    input_path = tmp_path / "in.wav"
    input_path.write_bytes(b"fake-wav-bytes")
    import pytest
    with pytest.raises(RuntimeError, match="stop-after-route"):
        sep.separate(str(input_path), str(tmp_path / "v.wav"), model_name="htdemucs")


def test_roformer_participates_in_unload(monkeypatch):
    sep._model_cache["bs_roformer"] = object()  # stand-in engine
    assert sep.is_model_loaded() is True
    sep.unload_models()
    assert sep.is_model_loaded() is False
    assert "bs_roformer" not in sep._model_cache


def test_preload_roformer_uses_load_path(monkeypatch):
    loaded = {}
    monkeypatch.setattr(sep, "_load_model", lambda name: loaded.setdefault("name", name))
    sep.preload_models(["bs_roformer"])
    assert loaded["name"] == "bs_roformer"
