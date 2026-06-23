#!/usr/bin/env python3
"""Claude Code session monitor — macOS menu bar with animations."""

import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import AppKit
from PIL import Image, ImageDraw
import rumps


def _resource(filename: str) -> Path:
    """app bundle 内と開発時の両方でリソースを解決する。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent.parent / "Resources"
    else:
        base = Path(__file__).parent
    return base / filename


SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CRAB_SRC     = _resource("cc-menubar-icon.png")
FRAMES_DIR   = Path.home() / "Library" / "Caches" / "com.ksterx.MenubarCC" / "frames"
HOOKS_CONFIG_DEFAULT = Path.home() / ".claude" / "hooks" / "config" / "hooks-config.json"
HOOKS_CONFIG_LOCAL   = Path.home() / ".claude" / "hooks" / "config" / "hooks-config.local.json"
APP_SUPPORT_DIR      = Path.home() / "Library" / "Application Support" / "com.ksterx.MenubarCC"
APP_SETTINGS_PATH    = APP_SUPPORT_DIR / "settings.json"
STUCK_SECS   = 600
REFRESH_SECS = 10

# Animation speed presets (seconds per frame). Lower = faster.
SPEED_PRESETS: list[tuple[str, float]] = [
    ("とても遅い", 0.30),
    ("遅い",       0.20),
    ("普通",       0.12),
    ("速い",       0.08),
    ("とても速い", 0.04),
]
DEFAULT_ANIM_FPS = 0.12

# Hook events that the user can mute / customize per-event from the menu bar.
CONTROLLED_HOOK_EVENTS: list[tuple[str, str, str]] = [
    # (event_name, disable_flag_key, human_label)
    ("Stop",              "disableStopHook",              "Stop（応答終了）"),
    ("Notification",      "disableNotificationHook",      "Notification（通知）"),
    ("PermissionRequest", "disablePermissionRequestHook", "PermissionRequest（許可要求）"),
]


# ── フレーム生成 ──────────────────────────────────────────────────────────────

def build_frames(crab: Image.Image) -> dict:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    # 透明パディングを除いた実際のカニ領域だけ使う
    bbox = crab.getbbox()
    if bbox:
        crab = crab.crop(bbox)
    cw, ch = crab.size          # 実サイズ（例: 44 × 28）
    PAD     = 36                # 横移動の余白
    CW      = cw + PAD          # キャンバス幅 80
    CH      = 44                # キャンバス高（メニューバー基準）
    cy      = (CH - ch) // 2   # 縦中央

    def blank():
        return Image.new("RGBA", (CW, CH), (0, 0, 0, 0))

    # ── 歩き（右→左） ────────────────────────────────────────────────────
    WALK_N = 14
    walk = []
    for i in range(WALK_N):
        t = i / (WALK_N - 1)
        x = int((1 - t) * PAD)   # PAD→0（右端から左端へ）
        img = blank()
        img.paste(crab, (x, cy), crab)
        p = FRAMES_DIR / f"walk_{i:02d}.png"
        img.save(p)
        walk.append(str(p))

    # ── バウンス（上下、待ち）────────────────────────────────────────────
    BOUNCE_N = 12
    BOUNCE_H = 8                 # 最大8px上へ
    bounce = []
    for i in range(BOUNCE_N):
        t = math.sin(math.pi * i / BOUNCE_N)   # 0→1→0
        y = cy - int(t * BOUNCE_H)
        img = blank()
        img.paste(crab, (PAD // 2, y), crab)   # 中央固定
        p = FRAMES_DIR / f"bounce_{i:02d}.png"
        img.save(p)
        bounce.append(str(p))

    # ── パルス（stuck、赤みがかったフラッシュ）──────────────────────────
    PULSE_N = 8
    pulse = []
    for i in range(PULSE_N):
        alpha = int(128 + 127 * math.sin(2 * math.pi * i / PULSE_N))
        img = blank()
        # カニを薄くオーバーレイ（点滅）
        frame = crab.copy()
        r, g, b, a = frame.split()
        a = a.point(lambda v: int(v * alpha / 255))
        frame = Image.merge("RGBA", (r, g, b, a))
        img.paste(frame, (PAD // 2, cy), frame)
        p = FRAMES_DIR / f"pulse_{i:02d}.png"
        img.save(p)
        pulse.append(str(p))

    # ── 静止（idle）──────────────────────────────────────────────────────
    static = blank()
    static.paste(crab, (PAD // 2, cy), crab)
    sp = FRAMES_DIR / "static.png"
    static.save(sp)

    return {"walk": walk, "bounce": bounce, "pulse": pulse, "static": str(sp)}


# ── セッション読み込み ────────────────────────────────────────────────────────

def _now_ms() -> float:
    return time.time() * 1000


def fmt_age(secs: float) -> str:
    m = int(secs // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def load_sessions() -> list[dict]:
    result = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            age_s = (_now_ms() - d.get("statusUpdatedAt", _now_ms())) / 1000
            sid   = d.get("sessionId", "")
            d["_age_s"]   = age_s
            d["_stuck"]   = d.get("status") == "busy" and age_s > STUCK_SECS
            d["_waiting"] = (
                d.get("status") == "idle"
                and (SESSIONS_DIR / f"{sid}.waiting").exists()
            )
            d["_dir"] = Path(d.get("cwd", "?")).name or d.get("cwd", "?")
            result.append(d)
        except Exception:
            pass
    return sorted(result, key=lambda x: x.get("updatedAt", 0), reverse=True)


def count_today_tools() -> int:
    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    total = 0
    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if jsonl.stat().st_mtime < today_start:
                continue
            with open(jsonl) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "say":
                            content = entry.get("message", {}).get("content", [])
                            if isinstance(content, list):
                                total += sum(
                                    1 for b in content
                                    if isinstance(b, dict) and b.get("type") == "tool_use"
                                )
                    except Exception:
                        pass
        except Exception:
            pass
    return total


def make_header(text: str) -> rumps.MenuItem:
    item = rumps.MenuItem(text)
    item.set_callback(None)
    return item


# ── 設定ファイル I/O ────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_hooks_config_merged() -> dict:
    """default + local（local が default を上書き）。表示用。"""
    cfg = _read_json(HOOKS_CONFIG_DEFAULT)
    cfg.update(_read_json(HOOKS_CONFIG_LOCAL))
    return cfg


def load_hooks_local() -> dict:
    """書き込み用に local 設定だけを読む。"""
    return _read_json(HOOKS_CONFIG_LOCAL)


def save_hooks_local(cfg: dict) -> None:
    _write_json_atomic(HOOKS_CONFIG_LOCAL, cfg)


def update_hooks_local(**changes) -> None:
    """指定キーだけ local 設定にマージして保存。soundPaths は dict マージ。"""
    cfg = load_hooks_local()
    for key, value in changes.items():
        if key == "soundPaths" and isinstance(value, dict):
            paths = dict(cfg.get("soundPaths") or {})
            paths.update(value)
            cfg["soundPaths"] = paths
        else:
            cfg[key] = value
    save_hooks_local(cfg)


def load_app_settings() -> dict:
    return _read_json(APP_SETTINGS_PATH)


def save_app_settings(cfg: dict) -> None:
    _write_json_atomic(APP_SETTINGS_PATH, cfg)


# ── アプリ ───────────────────────────────────────────────────────────────────

ICON_PT_H = 16   # メニューバーアイコンの表示高さ（ポイント）


class CCApp(rumps.App):
    def _set_frame(self, path: str):
        """NSImage のサイズをポイント単位で明示指定してセットする。"""
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        w_px, h_px = img.size().width, img.size().height
        scale  = ICON_PT_H / h_px
        pt_w   = w_px * scale
        img.setSize_(AppKit.NSSize(pt_w, ICON_PT_H))
        self._icon_nsimage = img
        try:
            self._nsapp.setStatusBarIcon()
        except AttributeError:
            pass

    def __init__(self, frames: dict):
        super().__init__("", icon=frames["static"], template=False, quit_button=None)
        self._frames       = frames
        self._anim_idx     = 0
        self._anim_state   = "idle"   # "walk" | "bounce" | "pulse" | "idle"
        self._known_stuck: set[str] = set()
        self._tool_count   = 0
        self._last_tool_at = 0.0

        # アニメーション速度をユーザー設定から復元（無ければデフォルト）
        app_cfg = load_app_settings()
        self._anim_fps = float(app_cfg.get("animFps", DEFAULT_ANIM_FPS))

        # 動的に interval を変えたいので @rumps.timer ではなく Timer インスタンスを保持
        self._anim_timer = rumps.Timer(self._animate, self._anim_fps)
        self._anim_timer.start()

        self._refresh(None)           # 初回データ取得

    # ── アニメーション（速度はユーザー設定）─────────────────────────────
    def _animate(self, _):
        state = self._anim_state
        if state == "walk":
            seq = self._frames["walk"]
        elif state == "bounce":
            seq = self._frames["bounce"]
        elif state == "pulse":
            seq = self._frames["pulse"]
        else:
            return   # idle は静止なので何もしない

        self._anim_idx = (self._anim_idx + 1) % len(seq)
        self._set_frame(seq[self._anim_idx])

    # ── データ更新（10秒）────────────────────────────────────────────────
    @rumps.timer(REFRESH_SECS)
    def _refresh(self, _):
        ss      = load_sessions()
        stuck   = [s for s in ss if s["_stuck"]]
        busy    = [s for s in ss if s.get("status") == "busy" and not s["_stuck"]]
        waiting = [s for s in ss if s["_waiting"]]
        idle    = [s for s in ss if s.get("status") == "idle" and not s["_waiting"]]

        # ── アニメーション状態を決定 ──────────────────────────────────
        if stuck:
            new_state = "pulse"
            self.title = f"⚠ {len(stuck)}"
        elif busy:
            new_state = "walk"
            self.title = ""
        elif waiting:
            new_state = "bounce"
            self.title = ""
        else:
            new_state = "idle"
            self.title = ""
            self.icon  = self._frames["static"]

        if new_state != self._anim_state:
            self._anim_state = new_state
            self._anim_idx   = 0

        if new_state == "idle":
            self._set_frame(self._frames["static"])

        # ── stuck 通知 ──────────────────────────────────────────────
        for s in stuck:
            sid = s.get("sessionId", "")
            if sid not in self._known_stuck:
                rumps.notification(
                    title="Claude Code — スタック検出",
                    subtitle=s["_dir"],
                    message=f"{fmt_age(s['_age_s'])} busy のまま更新なし",
                )
        self._known_stuck = {s.get("sessionId", "") for s in stuck}

        # ── ツール数（60秒おき）──────────────────────────────────────
        now = time.time()
        if now - self._last_tool_at > 60:
            self._tool_count  = count_today_tools()
            self._last_tool_at = now

        # ── メニュー再構築 ────────────────────────────────────────────
        items: list = []

        def add_section(label: str, sessions: list[dict], icon: str):
            if not sessions:
                return
            items.append(make_header(label))
            for s in sessions:
                items.append(rumps.MenuItem(
                    f"      {icon}  {s['_dir']}   {fmt_age(s['_age_s'])}"
                ))
            items.append(None)

        if not ss:
            items.append(rumps.MenuItem("セッションなし"))
            items.append(None)
        else:
            add_section(f"⚠   STUCK  ·  {len(stuck)}",   stuck,   "⚠")
            add_section(f"↻   ACTIVE  ·  {len(busy)}",   busy,    "↻")
            add_section(f"💬  入力待ち  ·  {len(waiting)}", waiting, "💬")
            add_section(f"·   IDLE  ·  {len(idle)}",     idle,    "·")

        summary = f"今日  {len(ss)} セッション · {self._tool_count} ツール呼び出し"
        items.append(make_header(summary))
        items.append(None)
        items.append(rumps.MenuItem("今すぐ更新", callback=self._refresh))
        items.append(None)
        items.append(self._build_sound_menu())
        items.append(self._build_speed_menu())
        items.append(None)
        items.append(rumps.MenuItem("終了", callback=rumps.quit_application))

        self.menu.clear()
        for item in items:
            self.menu.add(item)

    # ── 通知音メニュー ──────────────────────────────────────────────────
    def _build_sound_menu(self) -> rumps.MenuItem:
        cfg = load_hooks_config_merged()
        muted = bool(cfg.get("muteAll", False))
        sound_paths = cfg.get("soundPaths") or {}

        root = rumps.MenuItem("通知音")

        mute_item = rumps.MenuItem("すべてミュート", callback=self._toggle_mute_all)
        mute_item.state = 1 if muted else 0
        root.add(mute_item)
        root.add(None)

        for event, flag_key, label in CONTROLLED_HOOK_EVENTS:
            disabled = bool(cfg.get(flag_key, False))
            item = rumps.MenuItem(label, callback=self._make_toggle_event_callback(flag_key))
            item.state = 0 if disabled else 1
            if muted:
                item.set_callback(None)  # ミュート中はグレーアウト
            root.add(item)
        root.add(None)

        for event, _flag_key, label in CONTROLLED_HOOK_EVENTS:
            current = sound_paths.get(event)
            suffix = f"  ({Path(current).name})" if current else "  (デフォルト)"
            choose = rumps.MenuItem(
                f"{event} の音を選択…{suffix}",
                callback=self._make_choose_sound_callback(event),
            )
            root.add(choose)
        root.add(None)
        root.add(rumps.MenuItem("カスタム音をすべて解除", callback=self._reset_all_sounds))

        return root

    # ── 速度メニュー ────────────────────────────────────────────────────
    def _build_speed_menu(self) -> rumps.MenuItem:
        root = rumps.MenuItem("カニの速度")
        current = self._anim_fps
        # 最も近いプリセットにチェック
        closest = min(SPEED_PRESETS, key=lambda p: abs(p[1] - current))[1]
        for label, fps in SPEED_PRESETS:
            item = rumps.MenuItem(label, callback=self._make_set_speed_callback(fps))
            item.state = 1 if abs(fps - closest) < 1e-6 else 0
            root.add(item)
        return root

    # ── 通知音コールバック ──────────────────────────────────────────────
    def _toggle_mute_all(self, sender):
        new_value = not bool(sender.state)
        update_hooks_local(muteAll=new_value)
        self._refresh(None)

    def _make_toggle_event_callback(self, flag_key: str):
        def _cb(sender):
            # state == 1 (チェック中 = enabled) → disable に切り替え
            new_disabled = bool(sender.state)
            update_hooks_local(**{flag_key: new_disabled})
            self._refresh(None)
        return _cb

    def _make_choose_sound_callback(self, event_name: str):
        def _cb(_sender):
            path = self._prompt_sound_file(event_name)
            if path:
                update_hooks_local(soundPaths={event_name: path})
                self._refresh(None)
        return _cb

    def _reset_all_sounds(self, _sender):
        cfg = load_hooks_local()
        cfg["soundPaths"] = {event: None for event, _, _ in CONTROLLED_HOOK_EVENTS}
        save_hooks_local(cfg)
        self._refresh(None)

    def _prompt_sound_file(self, event_name: str) -> str | None:
        """NSOpenPanel で音ファイルを選択。キャンセル時は None。"""
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_(f"{event_name} に使う音ファイルを選択")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["mp3", "wav", "m4a", "aiff", "aac", "caf"])
        if panel.runModal() != 1:
            return None
        urls = panel.URLs()
        if not urls:
            return None
        return str(urls[0].path())

    # ── 速度コールバック ────────────────────────────────────────────────
    def _make_set_speed_callback(self, fps: float):
        def _cb(_sender):
            self._anim_fps = fps
            self._anim_timer.stop()
            self._anim_timer.interval = fps
            self._anim_timer.start()
            settings = load_app_settings()
            settings["animFps"] = fps
            save_app_settings(settings)
            self._refresh(None)
        return _cb


# ── エントリポイント ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    crab   = Image.open(str(CRAB_SRC)).convert("RGBA")
    frames = build_frames(crab)
    CCApp(frames).run()
