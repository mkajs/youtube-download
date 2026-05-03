#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
MAX_PART_BYTES = 1_900 * 1024 * 1024  # 1.9 GB


def run(cmd, **kwargs):
    """Run a shell command and return the result"""
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    """Check if URL is a YouTube playlist"""
    return "playlist?list=" in url or "/playlist/" in url


def yt_dlp_cmd(url, output_template, playlist):
    """Generate yt-dlp command with Deno runtime and remote EJS components"""
    # Use Deno with remote EJS components (auto-downloads challenge solver)
    js_flags = "--js-runtimes deno --remote-components ejs:npm"

    # Format: prioritize 720p, then any video with audio
    fmt = "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best"

    no_playlist = "" if playlist else "--no-playlist"

    # Cookies file handling
    cookies = f'--cookies "{COOKIES_FILE}"' if COOKIES_FILE and Path(COOKIES_FILE).exists() else ""

    # Build the command
    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--retries 5 --fragment-retries 5 --sleep-requests 2 '
        f'--sleep-interval 3 --max-sleep-interval 10 '
        f'--no-check-certificates '
        f'{no_playlist} {cookies} {js_flags} '
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


def zip_files(files, zip_path):
    """Create a zip file containing the given files"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return zip_path


def split_and_zip(mp4, tmpdir):
    """Split large MP4 files into parts and zip each part"""
    prefix = str(mp4) + ".part"
    run(f'split -b {MAX_PART_BYTES} "{mp4}" "{prefix}"')
    parts = sorted(Path(tmpdir).glob(mp4.name + ".part*"))
    zips = []
    for i, part in enumerate(parts, 1):
        zip_path = Path(tmpdir) / f"{mp4.stem}.part{i}.zip"
        zip_files([part], zip_path)
        zips.append(zip_path)
    return zips


def release_exists(tag):
    """Check if a GitHub release already exists"""
    return run(f'gh release view "{tag}" --repo "{REPO}"').returncode == 0


def create_or_upload_release(tag, title, notes, files):
    """Create a new release or upload files to existing one"""
    files_str = " ".join(f'"{f}"' for f in files)
    notes_escaped = notes.replace('"', '\\"').replace('\n', '\\n')

    if release_exists(tag):
        print(f"  Uploading to existing release: {tag}")
        return run(f'gh release upload "{tag}" {files_str} --repo "{REPO}" --clobber')

    print(f"  Creating new release: {tag}")
    return run(
        f'gh release create "{tag}" {files_str} '
        f'--repo "{REPO}" --title "{title}" --notes "{notes_escaped}"'
    )


def get_release_url(tag):
    """Get the URL for a GitHub release"""
    result = run(f'gh release view "{tag}" --repo "{REPO}" --json url -q .url')
    return result.stdout.strip() if result.returncode == 0 else ""


def process_entry(entry, tmpdir):
    """Process a single video/playlist entry"""
    url = entry["url"]
    playlist = is_playlist(url)
    output_template = (
        "%(playlist_id)s/%(playlist_index)03d-%(id)s.%(ext)s"
        if playlist
        else "%(id)s.%(ext)s"
    )

    print(f"\n📥 Downloading: {url}")
    result = run(yt_dlp_cmd(url, str(Path(tmpdir) / output_template), playlist))

    if result.returncode != 0:
        error_msg = result.stderr[-500:].strip()
        print(f"  ❌ ERROR: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Find downloaded MP4 files
    mp4_files = sorted(Path(tmpdir).rglob("*.mp4"))
    if not mp4_files:
        # Check for other video formats
        other_formats = list(Path(tmpdir).rglob("*.mkv")) + list(Path(tmpdir).rglob("*.webm"))
        if other_formats:
            print(f"  Found {len(other_formats)} non-MP4 files, converting...")
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
    title = info.get("title") or info.get("playlist_title") or Path(mp4_files[0]).stem
    print(f"  📝 Title: {title}")
    print(f"  📁 Files: {len(mp4_files)} video file(s)")

    # Handle playlist vs single video
    if playlist:
        pl_id = info.get("playlist_id") or info.get("playlist") or Path(tmpdir).name
        tag = f"yt-playlist-{pl_id}"[:100]

        upload_files = []
        for mp4 in mp4_files:
            size_mb = mp4.stat().st_size / (1024 * 1024)
            print(f"  📦 File size: {size_mb:.2f} MB")

            if mp4.stat().st_size > MAX_PART_BYTES:
                print(f"  ✂ File exceeds 1.9GB, splitting into parts...")
                upload_files.extend(split_and_zip(mp4, tmpdir))
            else:
                zip_path = Path(tmpdir) / f"{mp4.stem}.zip"
                upload_files.append(zip_files([mp4], zip_path))

        notes = (
            f"Source: {url}\n"
            f"Total videos: {len(mp4_files)}\n\n"
            "**Split parts instructions:**\n"
            "1. Extract each zip file\n"
            "2. Concatenate parts:\n"
            "```bash\ncat *.part*.mp4 > combined.mp4\n```"
        )
    else:
        # Single video
        video_id = info.get("id") or mp4_files[0].stem
        tag = f"yt-{video_id}"[:100]
        mp4 = mp4_files[0]
        size_mb = mp4.stat().st_size / (1024 * 1024)
        print(f"  📦 File size: {size_mb:.2f} MB")

        if mp4.stat().st_size > MAX_PART_BYTES:
            print(f"  ✂ File exceeds 1.9GB, splitting into parts...")
            upload_files = split_and_zip(mp4, tmpdir)
            notes = (
                f"Source: {url}\n\n"
                "**Split parts instructions:**\n"
                "1. Extract each zip file\n"
                "2. Concatenate parts:\n"
                f"```bash\ncat {mp4.stem}.part*.mp4 > {mp4.name}\n```"
            )
        else:
            zip_path = Path(tmpdir) / f"{video_id}.zip"
            upload_files = [zip_files([mp4], zip_path)]
            notes = f"Source: {url}"

    # Create release and upload files
    result = create_or_upload_release(tag, title, notes, upload_files)
    if result.returncode != 0:
        error_msg = result.stderr[-500:].strip()
        print(f"  ❌ ERROR uploading to GitHub: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Update entry with success data
    entry["status"] = "done"
    entry["title"] = title
    entry["release_tag"] = tag
    entry["release_url"] = get_release_url(tag)
    entry["downloaded_at"] = date.today().isoformat()
    print(f"  ✅ Done: {entry['release_url']}")
    return entry


def main():
    """Main entry point"""
    print("=" * 60)
    print("🎬 YouTube Download Script")
    print("=" * 60)

    # Check if videos.json exists
    if not VIDEOS_JSON.exists():
        print(f"❌ Error: {VIDEOS_JSON} not found")
        exit(1)

    # Load and filter pending videos
    try:
        videos = json.loads(VIDEOS_JSON.read_text())
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing videos.json: {e}")
        exit(1)

    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("✅ No pending videos.")
        return

    print(f"\n📋 Processing {len(pending)} pending video(s)...")

    # Process each pending entry
    for i, entry in enumerate(pending, 1):
        print(f"\n{'─' * 60}")
        print(f"📌 [{i}/{len(pending)}]")
        print(f"{'─' * 60}")

        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)

        # Save progress after each entry
        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")
        print(f"💾 Progress saved to {VIDEOS_JSON}")

    # Show summary
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    successful = [v for v in videos if v.get("status") == "done"]
    failed = [v for v in videos if v.get("status") == "failed"]
    print(f"✅ Successful: {len(successful)}")
    print(f"❌ Failed: {len(failed)}")

    for f in failed:
        print(f"  - {f.get('url')}: {f.get('error', 'Unknown error')[:100]}")

    if failed:
        print("\n⚠️ Some downloads failed. Check the errors above.")
        exit(1)

    print("\n🎉 All done! All videos downloaded successfully.")


if __name__ == "__main__":
    main()