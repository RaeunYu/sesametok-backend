"""메뉴바 애플리케이션 계층.

``app.status``는 순수 로직이라 헤드리스 CI를 포함한 어디서든 임포트할 수 있다.
``app.menubar``는 PyObjC 뷰 계층이며 macOS에서 AppKit을 요구한다.
"""

from __future__ import annotations
