#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
# Store videos in a 'videos' directory in the repo
VIDEOS_DIR = Path(__file__).parent.parent / "videos"
MAX_FILE_SIZE_MB = 100  # GitHub's soft limit for regular files (use LFS for larger)


def run(cmd, **kwargs):
    """Run a shell command and return the result"""
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    """Check if URL is a YouTube playlist"""
    return "playlist?list=" in url or "/playlist/" in url


def sanitize_filename(title):
    """Convert title to a valid filename"""
    # Replace invalid characters with underscores
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', title)
    # Replace spaces with underscores
    sanitized = sanitized.replace(' ', '_')
    # Remove consecutive underscores
    sanitized = re.sub(r'_+', '_', sanitized)
    # Limit length
    sanitized = sanitized[:100]
    return sanitized


def yt_dlp_cmd(url, output_template, playlist):
    """Generate yt-dlp command with proper cookie handling"""

    js_flags = "--js-runtimes deno --remote-components ejs:npm"
    fmt = "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best"
    no_playlist = "" if playlist else "--no-playlist"

    cookies = ""
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        with open(COOKIES_FILE, 'r') as f:
            if '# Netscape HTTP Cookie File' in f.readline():
                cookies = f'--cookies "{COOKIES_FILE}"'

    extractor_args = '--extractor-args "youtube:player_client=web,android,ios"'
    user_agent = '--user-agent "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'

    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--retries 10 --fragment-retries 10 --sleep-requests 5 '
        f'--sleep-interval 5 --max-sleep-interval 15 '
        f'--no-check-certificates --geo-bypass '
        f'--ignore-errors '
        f'{no_playlist} {cookies} {js_flags} {extractor_args} {user_agent} '
        f'-o "{output_template}" "{url}"'
    )


def read_info_json(tmpdir):
    """Read the .info.json file created by yt-dlp"""
    info_jsons = sorted(Path(tmpdir).rglob("*.info.json"))
    if not info_jsons:
        return {}
    try:
        return json.loads(info_jsons[0].read_text())
    except Exception:
        return {}


def process_entry(entry, tmpdir):
    """Process a single video/playlist entry and save to repo"""
    url = entry["url"]
    playlist = is_playlist(url)

    # Create videos directory if it doesn't exist
    VIDEOS_DIR.mkdir(exist_ok=True)

    # Use title in filename for better readability
    output_template = str(VIDEOS_DIR / "%(title)s.%(ext)s")

    print(f"\n📥 Downloading: {url}")
    result = run(yt_dlp_cmd(url, output_template, playlist))

    if result.returncode != 0:
        error_msg = result.stderr[-1000:].strip()
        print(f"  ❌ ERROR: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Find downloaded MP4 files
    mp4_files = sorted(VIDEOS_DIR.rglob("*.mp4"))
    if not mp4_files:
        # Check for other video formats
        other_formats = list(VIDEOS_DIR.rglob("*.mkv")) + list(VIDEOS_DIR.rglob("*.webm"))
        if other_formats:
            print(f"  Converting {len(other_formats)} files to MP4...")
            for vf in other_formats:
                new_name = vf.with_suffix('.mp4')
                vf.rename(new_name)
                mp4_files.append(new_name)
        else:
            print("  ❌ ERROR: No video files produced")
            entry["status"] = "failed"
            entry["error"] = "No video files produced by yt-dlp"
            return entry

    # Read metadata
    info = read_info_json(tmpdir)
    title = info.get("title") or Path(mp4_files[0]).stem
    safe_filename = sanitize_filename(title)

    # Rename file to safe name
    for mp4 in mp4_files:
        new_name = VIDEOS_DIR / f"{safe_filename}.mp4"
        if mp4 != new_name:
            mp4.rename(new_name)
            print(f"  📝 Renamed to: {safe_filename}.mp4")

    file_path = VIDEOS_DIR / f"{safe_filename}.mp4"
    file_size_mb = file_path.stat().st_size / (1024 * 1024)

    print(f"  📝 Title: {title}")
    print(f"  📁 File: {safe_filename}.mp4")
    print(f"  📦 Size: {file_size_mb:.2f} MB")

    # Create a metadata file
    metadata = {
        "original_url": url,
        "title": title,
        "filename": f"{safe_filename}.mp4",
        "file_size_mb": round(file_size_mb, 2),
        "downloaded_at": date.today().isoformat(),
        "video_id": info.get("id"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count")
    }

    metadata_path = VIDEOS_DIR / f"{safe_filename}.json"
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"  📄 Metadata saved: {safe_filename}.json")

    # For very large files, provide split instructions instead of storing
    if file_size_mb > MAX_FILE_SIZE_MB:
        print(f"  ⚠️ File exceeds {MAX_FILE_SIZE_MB}MB. Consider using Git LFS or external storage.")

        # Create a README for large files
        readme_path = VIDEOS_DIR / f"{safe_filename}_README.txt"
        with open(readme_path, 'w') as f:
            f.write(f"File: {safe_filename}.mp4\n")
            f.write(f"Size: {file_size_mb:.2f} MB\n")
            f.write(f"Source: {url}\n")
            f.write(f"Downloaded: {date.today().isoformat()}\n\n")
            f.write("This file is too large for GitHub. Use git-lfs or download manually.\n")
            f.write(f"yt-dlp command to download: yt-dlp -f best[height<=720] \"{url}\"\n")

    entry["status"] = "done"
    entry["title"] = title
    entry["filename"] = f"{safe_filename}.mp4"
    entry["file_size_mb"] = round(file_size_mb, 2)
    entry["downloaded_at"] = date.today().isoformat()
    entry["storage_path"] = str(file_path)

    print(f"  ✅ Done: Saved to {file_path}")
    return entry


def update_readme(videos):
    """Generate a README file listing all downloaded videos"""
    readme_path = Path(__file__).parent.parent / "VIDEOS.md"

    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write("# Downloaded Videos\n\n")
        f.write("This file lists all videos downloaded by the automated workflow.\n\n")
        f.write("## Videos\n\n")

        successful = [v for v in videos if v.get("status") == "done"]

        for video in successful:
            f.write(f"### {video.get('title', 'Unknown')}\n")
            f.write(f"- **URL**: {video.get('url', 'N/A')}\n")
            f.write(f"- **Filename**: `{video.get('filename', 'N/A')}`\n")
            f.write(f"- **Size**: {video.get('file_size_mb', 0):.2f} MB\n")
            f.write(f"- **Downloaded**: {video.get('downloaded_at', 'N/A')}\n")
            f.write("\n")

    print(f"📄 Updated VIDEOS.md with {len(successful)} videos")


def main():
    """Main entry point"""
    print("=" * 60)
    print("🎬 YouTube Download Script (Direct Repository Storage)")
    print("=" * 60)

    if not VIDEOS_JSON.exists():
        print(f"❌ Error: {VIDEOS_JSON} not found")
        exit(1)

    try:
        videos = json.loads(VIDEOS_JSON.read_text())
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing videos.json: {e}")
        exit(1)

    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("✅ No pending videos.")
        return

    print(f"\n📋 Processing {len(pending)} video(s)...")

    for i, entry in enumerate(pending, 1):
        print(f"\n{'─' * 60}")
        print(f"📌 [{i}/{len(pending)}]")
        print(f"{'─' * 60}")

        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)

        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")
        print(f"💾 Progress saved")

    # Update README with downloaded videos
    update_readme(videos)

    # Create .gitignore for videos directory if not exists
    gitignore_path = VIDEOS_DIR / ".gitignore"
    if not gitignore_path.exists():
        with open(gitignore_path, 'w') as f:
            f.write("# Video files are tracked via Git LFS\n")
            f.write("*.mp4 filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.zip filter=lfs diff=lfs merge=lfs -text\n")

    # Summary
    successful = [v for v in videos if v.get("status") == "done"]
    failed = [v for v in videos if v.get("status") == "failed"]

    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    print(f"✅ Downloaded: {len(successful)} video(s)")
    print(f"❌ Failed: {len(failed)}")

    for f in failed:
        print(f"  - {f.get('url')}: {f.get('error', 'Unknown')[:100]}")

    if successful:
        print(f"\n📁 Videos saved in the 'videos/' directory of this repository")
        print(f"📄 See VIDEOS.md for the complete list")

    if failed:
        exit(1)

    print("\n🎉 All done!")


if __name__ == "__main__":
    main()