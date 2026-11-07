import os
import zipfile

from core.uir import toolchain


def test_zip_dir_relative_dest(tmp_path, monkeypatch):
    """相对路径的 dest 应相对调用方 cwd, 而不是 src 的父目录(macOS CI 曾因此报 zip 退出码 15)。"""
    build = tmp_path / "build" / "App-darwin-arm64"
    (build / "Contents").mkdir(parents=True)
    (build / "Contents" / "f.txt").write_text("x")
    os.symlink("Contents", build / "Current")
    monkeypatch.chdir(tmp_path)
    out = toolchain.zip_dir(build, "dist/app.zip")
    assert out == (tmp_path / "dist" / "app.zip")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert "App-darwin-arm64/Contents/f.txt" in names
