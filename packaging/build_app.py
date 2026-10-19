#!/usr/bin/env python3
"""``SesametokBackend.app``을 만든다.

이 앱은 얇은 스텁이다: 안에 파이썬을 번들하지 않고, ``Contents/Resources/target``에
적힌 인터프리터를 자식으로 spawn한다(exec하지 않는다). 그래야 macOS TCC가 앱에 준
전체 디스크 접근·손쉬운 사용 권한이 자식에게 귀속되고, 같은 인터프리터를 쓰는 다른
프로젝트는 아무 권한도 얻지 않는다. 이 방식은 실측으로 검증되었다.

사용:
    ./.venv/bin/python packaging/build_app.py                 # /Applications (기본)
    ./.venv/bin/python packaging/build_app.py --dest ~/Applications
    ./.venv/bin/python packaging/build_app.py --no-sign       # 서명 없이(실험용)

빌드 후:
    sesametok-backend register      # 로그인 시 자동시작 등록(plist가 이 앱을 가리킨다)
    (앱을 /Applications에서 더블클릭해도 `sesametok-backend start`와 같다)
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUB_SOURCE = HERE / "stub.c"
INFO_TEMPLATE = HERE / "Info.plist.tmpl"
DEFAULT_NAME = "SesametokBackend"
DEFAULT_DEST = Path("/Applications")


def _build_info_plist(
    *,
    bundle_id: str,
    name: str,
    display_name: str,
    executable: str,
    version: str,
    copyright_text: str,
) -> bytes:
    template = INFO_TEMPLATE.read_text(encoding="utf-8")
    rendered = (
        template.replace("{bundle_id}", bundle_id)
        .replace("{name}", name)
        .replace("{display_name}", display_name)
        .replace("{executable}", executable)
        .replace("{version}", version)
        .replace("{copyright}", copyright_text)
    )
    # 렌더링 결과가 실제로 유효한 plist인지 확인한 뒤 쓴다.
    plistlib.loads(rendered.encode("utf-8"))
    return rendered.encode("utf-8")


def build(
    *,
    dest: Path,
    name: str = DEFAULT_NAME,
    python: Path,
    module: str,
    bundle_id: str,
    version: str,
    display_name: str,
    copyright_text: str,
    sign: bool = True,
) -> Path:
    if not STUB_SOURCE.is_file():
        raise SystemExit(f"스텁 소스를 찾을 수 없습니다: {STUB_SOURCE}")
    if not python.is_file():
        raise SystemExit(f"인터프리터를 찾을 수 없습니다: {python}")

    app = dest / f"{name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"

    if app.exists():
        shutil.rmtree(app)
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1) Info.plist
    (contents / "Info.plist").write_bytes(
        _build_info_plist(
            bundle_id=bundle_id,
            name=name,
            display_name=display_name,
            executable=name,
            version=version,
            copyright_text=copyright_text,
        )
    )

    # 2) 자식이 실행할 인터프리터와 모듈 이름
    (resources / "target").write_text(f"{python}\n{module}\n", encoding="utf-8")

    # 3) 스텁 컴파일
    executable = macos / name
    completed = subprocess.run(
        ["cc", "-O2", "-Wall", "-o", str(executable), str(STUB_SOURCE)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"스텁 컴파일 실패:\n{completed.stderr.strip()}")

    # 4) ad-hoc 서명 — TCC가 앱 신원을 기록하려면 서명이 있어야 한다.
    if sign:
        signed = subprocess.run(
            ["codesign", "--force", "--sign", "-", "--identifier", bundle_id, str(app)],
            capture_output=True,
            text=True,
        )
        if signed.returncode != 0:
            raise SystemExit(f"codesign 실패:\n{signed.stderr.strip()}")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_app.py",
        description="SesametokBackend.app(얇은 스텁)을 빌드한다.",
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="앱을 둘 디렉터리")
    parser.add_argument("--name", default=DEFAULT_NAME, help="앱 번들 이름")
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="앱이 spawn할 인터프리터(기본: 이 스크립트를 실행한 python)",
    )
    parser.add_argument("--module", default="sesametok_backend", help="실행할 모듈")
    parser.add_argument("--no-sign", action="store_true", help="codesign을 건너뛴다")
    args = parser.parse_args(argv)

    from sesametok_backend import __version__
    from sesametok_backend.config import BUNDLE_ID

    # resolve하지 않는다: venv 컨텍스트(pyvenv.cfg)는 호출 경로를 기준으로 잡히므로,
    # 심볼릭 링크를 실제 pyenv 경로로 바꾸면 site-packages를 찾지 못한다.
    python = args.python.expanduser()
    app = build(
        dest=args.dest.expanduser().resolve(),
        name=args.name,
        python=python,
        module=args.module,
        bundle_id=BUNDLE_ID,
        version=__version__,
        display_name="Sesametok 백엔드",
        copyright_text="sesametok-backend",
        sign=not args.no_sign,
    )

    print(f"빌드 완료: {app}")
    print(f"  번들 ID     : {BUNDLE_ID}")
    print(f"  실행 대상   : {python}")
    print(f"  모듈        : {args.module}")
    print()
    print("다음 단계")
    print("  1) 권한 부여 — 아래 실행 파일에 전체 디스크 접근 권한과 손쉬운 사용을 줍니다:")
    print(f"       {app}")
    print("     시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한 / 손쉬운 사용")
    print("  2) 로그인 시 자동시작 등록: sesametok-backend register")
    print("  3) 앱을 /Applications에서 더블클릭하면 `sesametok-backend start`와 같습니다.")
    print()
    print("주의: 앱을 다시 빌드하면 서명 해시가 바뀌어 기존 권한이 무효가 됩니다.")
    print("      경로를 고정하고, 재빌드 후에는 권한을 다시 부여하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
