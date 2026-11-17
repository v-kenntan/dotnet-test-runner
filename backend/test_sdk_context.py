"""SdkContext: global.json shape and DOTNET_* rules in one place. Run: python test_sdk_context.py"""
import json
import os
import tempfile

from sdk_source import SdkContext

ROOT = r"C:\sdks\11"


def test_env():
    assert SdkContext(ROOT).env({"PATH": "P"}) == {
        "DOTNET_ROOT": ROOT, "DOTNET_MULTILEVEL_LOOKUP": "0", "PATH": ROOT + ";P"}
    assert SdkContext().env({"PATH": "P"}) == {"PATH": "P"}


def test_bat_prelude():
    pinned = SdkContext(ROOT).bat_prelude()
    assert f'set "DOTNET_ROOT={ROOT}"' in pinned and f'set "PATH={ROOT};%PATH%"' in pinned
    # Unpinned clears what an earlier pinned test set in the same console.
    assert SdkContext().bat_prelude() == 'set "DOTNET_ROOT="\nset "DOTNET_MULTILEVEL_LOOKUP="\n'


def test_tfm():
    assert SdkContext(version="11.0.100-preview.7.26381.103").tfm == "net11.0"
    assert SdkContext().tfm == "net10.0"


def test_global_json():
    with tempfile.TemporaryDirectory() as d:
        SdkContext().write_global_json(d)
        assert not os.listdir(d)
        SdkContext(version="10.0.100").write_global_json(d)
        with open(os.path.join(d, "global.json")) as f:
            assert json.load(f) == {"sdk": {"version": "10.0.100", "rollForward": "disable"}}


def test_dotnet_exe_falls_back_to_path():
    assert SdkContext(r"C:\no\such\sdk").dotnet_exe() == "dotnet"


if __name__ == "__main__":
    test_env()
    test_bat_prelude()
    test_tfm()
    test_global_json()
    test_dotnet_exe_falls_back_to_path()
    print("ok")
