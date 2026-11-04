# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Arena.ai Desktop — native Windows app via pywebview + Flask.
Bundles static assets (HTML/CSS/JS) and the full arena package into a single .exe.
"""

import os
import platform

_HOST_ARCH = (os.environ.get("ARENA_TARGET_ARCH") or platform.machine() or "").lower()
_IS_ARM64 = _HOST_ARCH in ("arm64", "aarch64")
_EXE_NAME = "Arena-Desktop-arm64" if _IS_ARM64 else "Arena-Desktop"

a = Analysis(
    ['arena-cli.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('arena/web/static', 'arena/web/static'),
        ('assets/logo.svg', 'assets'),
        ('assets/logo.png', 'assets'),
    ],
    hiddenimports=[
        'arena',
        'arena.cli',
        'arena.api',
        'arena.auth',
        'arena.config',
        'arena.console',
        'arena.menu',
        'arena.models',
        'arena.desktop',
        'arena.deep_links',
        'arena.web',
        'arena.web.app',
        'arena.contracts',
        'arena.crypto',
        'arena.connectors',
        'arena.history_sync',
        'arena.permissions',
        'arena.agent',
        'arena.workspace',
        'arena.engine',
        'arena.tools',
        'arena.tools.base',
        'arena.tools.file_tools',
        'arena.tools.shell_tools',
        'arena.tools.browser_mcp',
        'arena.tools.mcp',
        'arena.ui',
        'arena.ui.banner',
        'arena.ui.chat_box',
        'arena.ui.markdown',
        'arena.ui.theme',
        'rich',
        'rich.panel',
        'rich.table',
        'rich.text',
        'rich.markdown',
        'rich.syntax',
        'rich.console',
        'requests',
        'websocket',
        'flask',
        'flask.json',
        'flask.templating',
        'jinja2',
        'markupsafe',
        'werkzeug',
        'webview',
        'webview.platforms.edgechromium',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX does not provide a reliable Windows ARM64 PE path; leave the
    # native ARM64 bootloader unpacked instead of risking a broken EXE.
    upx=not _IS_ARM64,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/arena-cli.ico',
)
