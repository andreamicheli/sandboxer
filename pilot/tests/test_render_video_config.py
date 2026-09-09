"""Tests verifying the Remotion render script configuration and assets."""

from pathlib import Path


VIDEO_DIR = Path(__file__).resolve().parents[2] / "video"
RENDER_SCRIPT = VIDEO_DIR / "render-video.js"


def test_render_script_contains_software_h264_and_swiftshader_flags():
    """Verify Chromium headless flags prevent H.264 NAL corruption on long renders."""
    assert RENDER_SCRIPT.is_file(), f"Missing {RENDER_SCRIPT}"
    content = RENDER_SCRIPT.read_text(encoding="utf-8")

    # Hardware acceleration disabled to force software encoder
    assert "hardwareAcceleration: 'disable'" in content

    # SwiftShader deterministic software GL rendering (replaces faulty angle + disable-gpu)
    assert "gl: 'swiftshader'" in content

    # Stabilization flags
    assert "'--disable-features=Vulkan'" in content
    assert "'--disable-gpu-compositing'" in content

    # Bundler must pass publicDir so static assets are bundled
    assert "publicDir:" in content

    # Subsets supported via SUBSET_FRAMES / frameRange
    assert "SUBSET_FRAMES" in content
    assert "frameRange" in content


def test_custom_intro_asset_is_present_and_accessible():
    """Verify custom intro video is present in video/public/assets and video/assets."""
    public_intro = VIDEO_DIR / "public" / "assets" / "custom-intro.mp4"
    assert public_intro.is_file(), f"Missing public intro: {public_intro}"
    assert public_intro.stat().st_size > 0

    assets_intro = VIDEO_DIR / "assets" / "custom-intro.mp4"
    assert assets_intro.is_file(), f"Missing assets intro: {assets_intro}"
    assert assets_intro.stat().st_size > 0
