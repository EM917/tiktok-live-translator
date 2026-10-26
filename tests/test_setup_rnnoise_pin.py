"""setup.sh / setup.ps1 预先下载 RNNoise 降噪模型：固定到上游的一个提交、核对 sha256、先下到 .part，对上了才改名，
对不上不留文件（专家审查 I5、计划审查 Q9）。以前两个脚本都从上游 master 下载、不核对：上游的文件一变（或被人换掉），
新装机拿到的就是另一份，没人知道；curl 不带 -f 时，404 的网页也会被存成 models/bd.rnnn。

程序自己下载那一份（app/pipeline.py 的 RNNOISE_URL、RNNOISE_SHA256）要和这里是同一对。

不真的下载：地址和校验值静态比对；下载那一段原样从脚本里拿出来，换上假的 curl / Invoke-WebRequest 跑一遍。
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import pipeline as pipeline_mod

ROOT = Path(__file__).resolve().parent.parent
# 上游唯一动过这个文件的提交（2018-08-31）。它的文件和本机 2026-08 起一直在用的那份逐字节相同：299,693 字节，
# sha256 如下（2026-10-26 不在直播时从下面的地址实际下载核对过）
PINNED_COMMIT = "6c2c1301c30a6be5ddf93704a693f4956ce2aa55"
PINNED_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/{}/"
              "beguiling-drafter-2018-08-30/bd.rnnn").format(PINNED_COMMIT)
PINNED_SHA256 = "ae3f7411e1e6a884f839a4a145c394408398f09854dbc1216ee02faafc98a17b"
# app/pipeline.py 还没固定时的地址：只在程序那一侧还没改过来时允许（见 test_the_app_downloads_the_same_pair）
UNPINNED_APP_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
                    "beguiling-drafter-2018-08-30/bd.rnnn")


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _block(text, start, end):
    """脚本里「[4/5] 下载 RNNoise」那一段（到「[5/5]」之前）。"""
    i = text.index(start)
    return text[i:text.index(end, i)]


SH_BLOCK = ('echo "==> [4/5]', 'echo "==> [5/5]')
PS_BLOCK = ('Write-Host "==> [4/5]', 'Write-Host "==> [5/5]')


def test_setup_scripts_pin_the_commit_and_the_sha256():
    sh = _read("setup.sh")
    assert re.findall(r'^RNNOISE_URL="([^"]*)"$', sh, re.M) == [PINNED_URL]
    assert re.findall(r'^RNNOISE_SHA256="([^"]*)"$', sh, re.M) == [PINNED_SHA256]
    assert 'curl -sfL -o models/bd.rnnn.part "$RNNOISE_URL"' in sh   # -f：HTTP 出错不存网页
    assert "shasum -a 256" in sh and "sha256sum" in sh                # macOS 和 Linux 各有其一

    ps = _read("setup.ps1")
    assert re.findall(r'^\$RnnoiseUrl = "([^"]*)"$', ps, re.M) == [PINNED_URL]
    assert re.findall(r'^\$RnnoiseSha256 = "([^"]*)"$', ps, re.M) == [PINNED_SHA256]
    assert 'Invoke-WebRequest -Uri $RnnoiseUrl -OutFile "models\\bd.rnnn.part"' in ps
    assert "Get-FileHash -Algorithm SHA256" in ps

    for name, text in (("setup.sh", sh), ("setup.ps1", ps)):
        # 整个脚本里只有这一个降噪模型地址，不再有跟着 master 走的那条
        assert re.findall(r"https://raw\.githubusercontent\.com/GregorR/rnnoise-models/\S+?bd\.rnnn", text) \
            == [PINNED_URL], name
        assert "/master/" not in text, name


def test_the_app_downloads_the_same_pair():
    """程序自己下载时用的地址和校验值（app/pipeline.py）与两个安装脚本是同一对。

    程序那一侧的固定（RNNOISE_SHA256）和这两个脚本不在同一个 PR 里：那一侧合进来之前，app/pipeline.py 只能
    还是原来那条 master 地址；改了地址却没有校验值、或者两边对不上，都不行。"""
    app_sha = getattr(pipeline_mod, "RNNOISE_SHA256", None)
    if app_sha is None:
        assert pipeline_mod.RNNOISE_URL == UNPINNED_APP_URL
        return
    assert (pipeline_mod.RNNOISE_URL, app_sha) == (PINNED_URL, PINNED_SHA256)


# ---- 下载那一段原样跑一遍（假的下载命令，不连网） ----

FAKE_BYTES = b"rnnoise-fake-model\n"


def _cases(tmp_path):
    """(名字, 已有模型, 假下载怎么做, 用哪个校验值) -> 期望的结果。"""
    good_sha = hashlib.sha256(FAKE_BYTES).hexdigest()
    return [
        ("match", False, "ok", good_sha),            # 下载内容对得上（把期望值换成假内容的）：留下
        ("mismatch", False, "ok", PINNED_SHA256),     # 对不上：不留，.part 也删
        ("fail", False, "fail", PINNED_SHA256),       # 下载失败（写了半截才出错）：不留，.part 也删
        ("exists", True, "ok", PINNED_SHA256),        # 已有模型：不下载
    ]


def _check(case, workdir, output, calls, text=True):
    """text=False：只看文件和下载请求，不看打印的中文（Windows 上 PowerShell 按控制台代码页输出）。"""
    model = workdir / "models" / "bd.rnnn"
    part = workdir / "models" / "bd.rnnn.part"
    assert not part.exists(), (case, output)
    if case == "match":
        assert model.read_bytes() == FAKE_BYTES, output
        assert calls == [PINNED_URL], calls
    elif case == "mismatch":
        assert not model.exists(), output
        assert calls == [PINNED_URL] and (not text or "对不上" in output), (output, calls)
    elif case == "fail":
        assert not model.exists(), output
        assert calls == [PINNED_URL] and (not text or "下载失败" in output), (output, calls)
    else:
        assert model.read_bytes() == b"keep", output
        assert calls == [] and (not text or "已存在" in output), (output, calls)


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                    reason="setup.sh 只在 macOS / Linux 上用")
def test_setup_sh_keeps_only_a_download_that_matches(tmp_path):
    block = _block(_read("setup.sh"), *SH_BLOCK)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_curl = bindir / "curl"
    # 记下请求的地址；按 FAKE_MODE 写出假内容，fail 时写半截再以 22（curl -f 碰上 HTTP 出错的退出码）退出
    fake_curl.write_text(
        "#!/bin/sh\n"
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; -*) shift;; *) url="$1"; shift;; esac; done\n'
        'printf "%s\\n" "$url" >> "$FAKE_LOG"\n'
        'if [ "$FAKE_MODE" = fail ]; then printf half > "$out"; exit 22; fi\n'
        'printf "rnnoise-fake-model\\n" > "$out"\n', encoding="utf-8")
    fake_curl.chmod(0o755)
    for case, exists, mode, sha in _cases(tmp_path):
        work = tmp_path / case
        work.mkdir()
        if exists:
            (work / "models").mkdir()
            (work / "models" / "bd.rnnn").write_bytes(b"keep")
        script = work / "step4.sh"
        script.write_text("set -e\n" + block.replace(PINNED_SHA256, sha) + "echo END\n", encoding="utf-8")
        log = work / "curl.log"
        env = dict(os.environ, PATH="{}{}{}".format(bindir, os.pathsep, os.environ.get("PATH", "")),
                   FAKE_LOG=str(log), FAKE_MODE=mode)
        r = subprocess.run(["bash", str(script)], cwd=str(work), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0 and "END" in r.stdout, (case, r.stdout, r.stderr)   # set -e 下也走完
        calls = log.read_text(encoding="utf-8").split() if log.exists() else []
        _check(case, work, r.stdout, calls)


@pytest.mark.skipif(shutil.which("pwsh") is None and shutil.which("powershell") is None,
                    reason="没有 PowerShell")
def test_setup_ps1_keeps_only_a_download_that_matches(tmp_path):
    block = _block(_read("setup.ps1"), *PS_BLOCK)
    shell = shutil.which("pwsh") or shutil.which("powershell")
    # 函数比同名的 cmdlet 先被找到：这一段里的 Invoke-WebRequest 用的是这个假的
    fake = (
        '$ErrorActionPreference = "Stop"\n'
        "function Invoke-WebRequest { param($Uri, $OutFile)\n"
        "  Add-Content -Path $env:FAKE_LOG -Value $Uri\n"
        '  if ($env:FAKE_MODE -eq "fail") { Set-Content -Path $OutFile -Value "half" -NoNewline;'
        ' throw "fake download failed" }\n'
        '  Set-Content -Path $OutFile -Value "rnnoise-fake-model`n" -NoNewline }\n')
    for case, exists, mode, sha in _cases(tmp_path):
        work = tmp_path / case
        work.mkdir()
        if exists:
            (work / "models").mkdir()
            (work / "models" / "bd.rnnn").write_bytes(b"keep")
        script = work / "step4.ps1"
        # 带 BOM：Windows PowerShell 5.1 才按 UTF-8 读中文
        script.write_text(fake + block.replace(PINNED_SHA256, sha) + 'Write-Host "END"\n',
                          encoding="utf-8-sig")
        log = work / "iwr.log"
        env = dict(os.environ, FAKE_LOG=str(log), FAKE_MODE=mode)
        r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(script)], cwd=str(work), env=env, capture_output=True,
                           timeout=90)
        out = r.stdout.decode("utf-8", errors="replace")
        assert r.returncode == 0 and "END" in out, (case, out, r.stderr.decode("utf-8", errors="replace"))
        calls = log.read_text(encoding="utf-8").split() if log.exists() else []
        _check(case, work, out, calls, text=sys.platform != "win32")
