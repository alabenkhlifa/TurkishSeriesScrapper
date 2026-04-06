#!/usr/bin/env python3
"""
TurkishSeriesScrapper - Automated Turkish series downloader and Plex manager.
Scrapes episodes from krmzi.org, downloads from cloud.mail.ru, integrates with Plex.
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml
from bs4 import BeautifulSoup
from plexapi.server import PlexServer


# --- Configuration ---

def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logging.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# --- Logging ---

def setup_logging():
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_dir / "scrapper.log",
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# --- HTTP Session ---

def _mount_retries(session):
    """Mount retry adapter on a session for transient network errors."""
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.9",
    })
    return _mount_retries(session)


# --- Scraping ---

def get_latest_episodes(session, base_url, series_slug, count=2):
    """Scrape the series page to find the latest episodes and their URLs.

    Returns list of (episode_number, episode_url) tuples, newest first.
    Returns empty list if no episodes found.
    """
    url = f"{base_url}/series/{quote(series_slug, safe='')}/"
    logging.info("Fetching series page: %s", unquote(url))

    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Episodes are listed as links containing الحلقة (episode) and a number
    episodes = {}  # ep_num -> url
    for link in soup.find_all("a", href=True):
        href = unquote(link["href"])
        match = re.search(r'الحلقة-(\d+)', href)
        if match:
            ep_num = int(match.group(1))
            if ep_num not in episodes:
                episodes[ep_num] = link["href"]

    if not episodes:
        logging.warning("No episodes found for %s", series_slug)
        return []

    latest_nums = sorted(episodes.keys(), reverse=True)[:count]
    logging.info("Latest episodes: %s (found %d total)",
                 ", ".join(str(n) for n in latest_nums), len(episodes))
    return [(num, episodes[num]) for num in latest_nums]


def extract_servers_from_element(element_url):
    """Extract server list from a qesen.net URL with base64 post parameter."""
    parsed = urlparse(element_url)
    params = parse_qs(parsed.query)
    if "post" not in params:
        return None

    decoded = base64.b64decode(params["post"][0]).decode("utf-8")
    payload = json.loads(decoded)
    return payload.get("servers", [])


def get_episode_servers(session, episode_url):
    """Scrape an episode page to get all available download servers.

    Returns a list of dicts: [{"type": "express", "url": "..."}, {"type": "arabhd", "id": "..."}]
    """
    logging.info("Fetching episode page: %s", unquote(episode_url))

    resp = session.get(episode_url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    available_servers = []
    seen_types = set()

    def collect_servers(servers_list):
        for server in servers_list:
            name = server.get("name", "").lower().strip()
            server_id = server.get("id", "")
            if name == "express" and "cloud.mail.ru" in server_id and "express" not in seen_types:
                available_servers.append({"type": "express", "url": server_id})
                seen_types.add("express")
            elif name == "arab hd" and server_id and "arabhd" not in seen_types:
                available_servers.append({"type": "arabhd", "id": server_id})
                seen_types.add("arabhd")

    # Search all links and iframes for qesen.net redirect with base64 payload
    candidates = []
    for tag in soup.find_all(["a", "iframe"], href=True):
        candidates.append(tag.get("href") or tag.get("src"))
    for tag in soup.find_all("iframe", src=True):
        candidates.append(tag["src"])

    for candidate_url in candidates:
        if not candidate_url or ("qesen.net" not in candidate_url and "post=" not in candidate_url):
            continue
        try:
            servers = extract_servers_from_element(candidate_url)
            if servers:
                collect_servers(servers)
        except Exception as e:
            logging.warning("Failed to decode payload from %s: %s", candidate_url, e)

    # Also check inline scripts for embedded base64 data
    for script in soup.find_all("script"):
        if not script.string:
            continue
        for match in re.finditer(r'post=([A-Za-z0-9+/=]+)', script.string):
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                payload = json.loads(decoded)
                collect_servers(payload.get("servers", []))
            except Exception:
                continue

    logging.info("Found %d server(s): %s", len(available_servers),
                 ", ".join(s["type"] for s in available_servers) or "none")
    if not available_servers:
        logging.warning("No download servers found for %s", unquote(episode_url))

    return available_servers


# --- Arab HD Download ---

def _unpack_js(packed_html):
    """Unpack JavaScript packed with Dean Edwards' packer (eval(function(p,a,c,k,e,d){...}))."""
    match = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.+?)',(\d+),(\d+),'(.+?)'\.\s*split\('\|'\)\)\)",
        packed_html,
        re.DOTALL,
    )
    if not match:
        return None

    p, a, c, k = match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).split("|")

    def base_n(num, base):
        """Convert number to base-N string."""
        chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if num < base:
            return chars[num]
        return base_n(num // base, base) + chars[num % base]

    for i in range(c - 1, -1, -1):
        if k[i]:
            token = base_n(i, a)
            p = re.sub(r'\b' + token + r'\b', k[i], p)

    return p


def get_arabhd_stream_url(server_id):
    """Fetch the Arab HD embed page and extract the m3u8 stream URL."""
    embed_url = f"https://v.turkvearab.com/embed-{server_id}.html"
    logging.info("Fetching Arab HD embed: %s", embed_url)

    # Use a fresh session - the embed page returns bad tokens if krmzi.org cookies are present
    embed_session = _mount_retries(requests.Session())
    embed_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    resp = embed_session.get(embed_url, timeout=30)
    resp.raise_for_status()

    # Try direct m3u8 match first
    page_text = resp.text
    m3u8_match = re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', page_text)

    # If not found, unpack JS packer and search there
    if not m3u8_match:
        unpacked = _unpack_js(page_text)
        if unpacked:
            m3u8_match = re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', unpacked)

    if m3u8_match:
        stream_url = m3u8_match.group(1)
        stream_url = stream_url.replace("&amp;", "&")
        logging.debug("m3u8 URL: %s", stream_url[:80])
        return stream_url

    logging.warning("Could not find m3u8 URL in Arab HD embed page")
    return None


def download_from_hls(stream_url, output_path):
    """Download an HLS stream using ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a temp file with ASCII name to avoid ffmpeg issues with Arabic paths
    tmp_path = output_path.parent / f".download_{os.getpid()}.mp4"

    logging.info("Downloading HLS stream via ffmpeg")

    cmd = [
        "ffmpeg",
        "-y",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-i", stream_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(tmp_path),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last_logged_mb = 0
    stderr_output = []

    for line in proc.stderr:
        stderr_output.append(line)
        # ffmpeg progress lines contain "size=" with current size in kB
        size_match = re.search(r'size=\s*(\d+)kB', line)
        if size_match:
            current_mb = int(size_match.group(1)) // 1024
            if current_mb >= last_logged_mb + 100:
                last_logged_mb = (current_mb // 100) * 100
                logging.info("  Progress: %dMB downloaded", current_mb)

    proc.wait()

    if proc.returncode != 0:
        err_tail = "".join(stderr_output[-20:])
        logging.error("ffmpeg failed (exit %d): %s", proc.returncode, err_tail[-500:])
        if tmp_path.exists():
            tmp_path.unlink()
        return False

    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        logging.error("ffmpeg produced no output")
        if tmp_path.exists():
            tmp_path.unlink()
        return False

    tmp_path.rename(output_path)
    size_mb = output_path.stat().st_size // (1024 * 1024)
    logging.info("HLS download complete: %s (%dMB)", output_path.name, size_mb)
    return True


# --- Mail.ru Download ---

def download_from_mailru(mailru_url, output_path):
    """Download a file from cloud.mail.ru public link."""
    parsed = urlparse(mailru_url)
    public_hash = parsed.path.replace("/public/", "").strip("/")

    logging.info("Downloading from mail.ru, hash: %s", public_hash)

    mailru_session = _mount_retries(requests.Session())
    mailru_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })

    # Step 1: Visit the public page to establish cookies and extract weblink_get URL
    page_resp = mailru_session.get(mailru_url, timeout=30)
    page_resp.raise_for_status()

    # Step 2: Extract weblink_get URL from embedded JSON in page
    weblink_get_url = None
    match = re.search(r'"weblink_get":\{"count":"\d+","url":"([^"]+)"', page_resp.text)
    if match:
        weblink_get_url = match.group(1)

    if not weblink_get_url:
        raise Exception("Could not find weblink_get URL from mail.ru page")

    logging.debug("weblink_get URL: %s", weblink_get_url)

    # Step 3: Download - weblink_get URL + public hash redirects to the actual file
    download_url = f"{weblink_get_url}/{public_hash}"
    logging.info("Starting mail.ru download")

    # Atomic write via .part temp file
    part_path = Path(str(output_path) + ".part")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with mailru_session.get(download_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0

        with open(part_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=128 * 1024):  # 128KB chunks
                f.write(chunk)
                downloaded += len(chunk)
                # Log progress every ~100MB
                if total > 0 and downloaded % (100 * 1024 * 1024) < 128 * 1024:
                    pct = (downloaded / total) * 100
                    logging.info("  Progress: %.1f%% (%dMB / %dMB)",
                                 pct, downloaded // (1024 * 1024), total // (1024 * 1024))

    # Rename .part to final path (atomic on same filesystem)
    part_path.rename(output_path)
    logging.info("Download complete: %s (%dMB)", output_path.name, downloaded // (1024 * 1024))
    return True


# --- Plex Integration ---

def connect_plex(config):
    """Connect to Plex server."""
    try:
        plex = PlexServer(config["plex"]["url"], config["plex"]["token"])
        logging.info("Connected to Plex: %s", plex.friendlyName)
        return plex
    except Exception as e:
        logging.error("Failed to connect to Plex: %s", e)
        return None


def cleanup_watched(plex, config):
    """Delete episodes that have been watched in Plex (scans all libraries)."""
    media_root = Path(config["storage"]["media_root"])
    deleted_count = 0

    for library in plex.library.sections():
        for show in library.all():
            try:
                episodes = show.episodes()
            except Exception:
                continue
            for episode in episodes:
                if not episode.isPlayed:
                    continue
                for media in episode.media:
                    for part in media.parts:
                        file_path = Path(part.file)
                        if not file_path.exists():
                            continue
                        if not str(file_path).startswith(str(media_root)):
                            continue

                        logging.info("Deleting watched: %s", file_path.name)
                        file_path.unlink()
                        deleted_count += 1

                        # Clean up empty directories
                        season_dir = file_path.parent
                        if season_dir.exists() and not any(season_dir.iterdir()):
                            season_dir.rmdir()
                            logging.debug("Removed empty dir: %s", season_dir.name)

                        series_dir = season_dir.parent
                        if series_dir.exists() and not any(series_dir.iterdir()):
                            series_dir.rmdir()
                            logging.debug("Removed empty dir: %s", series_dir.name)

    if deleted_count > 0:
        logging.info("Cleaned up %d watched episode(s)", deleted_count)

    return deleted_count


# --- Disk Space ---

def check_disk_space(media_root, min_free_gb):
    """Check if there's enough free disk space."""
    try:
        usage = shutil.disk_usage(media_root)
        free_gb = usage.free / (1024 ** 3)
        logging.info("Disk space: %.1fGB free", free_gb)
        return free_gb >= min_free_gb
    except Exception as e:
        logging.warning("Could not check disk space: %s", e)
        return True  # Assume OK if we can't check


# --- Episode Path ---

def get_episode_path(media_root, series_name, episode_num):
    """Get the Plex-compatible file path for an episode."""
    return (
        Path(media_root)
        / series_name
        / "Season 01"
        / f"{series_name} - S01E{episode_num:02d}.mp4"
    )


# --- Temp file cleanup ---

def cleanup_stale_temp_files(media_root):
    """Remove leftover .part and .download_* temp files from interrupted downloads."""
    count = 0
    for pattern in ("**/*.part", "**/.download_*.mp4"):
        for tmp in Path(media_root).glob(pattern):
            logging.info("Removing stale temp file: %s", tmp.name)
            tmp.unlink()
            count += 1
    if count > 0:
        logging.info("Cleaned up %d stale temp file(s)", count)


# --- Main ---

def main():
    setup_logging()
    logging.info("TurkishSeriesScrapper starting")

    config = load_config()
    session = create_session()
    media_root = Path(config["storage"]["media_root"])
    min_free_gb = config["storage"].get("min_free_space_gb", 10)
    base_url = config["scraper"]["base_url"]

    # Ensure media root exists
    media_root.mkdir(parents=True, exist_ok=True)

    # Clean up temp files from previous interrupted runs
    cleanup_stale_temp_files(media_root)

    # Connect to Plex
    plex = connect_plex(config)

    # Cleanup watched episodes first (free space before downloading)
    if plex:
        cleanup_watched(plex, config)

    # Check disk space
    if not check_disk_space(str(media_root), min_free_gb):
        logging.error("Not enough disk space (need %dGB free). Aborting.", min_free_gb)
        return

    for series in config.get("series", []):
        if not series.get("enabled", True):
            continue

        series_name = series["name"]
        series_slug = series["slug"]
        logging.info("Processing: %s", series_name)

        try:
            max_eps = config.get("download", {}).get("max_episodes_per_series", 2)
            latest_episodes = get_latest_episodes(session, base_url, series_slug, count=max_eps)
            if not latest_episodes:
                continue

            for ep_num, ep_url in latest_episodes:
                ep_path = get_episode_path(media_root, series_name, ep_num)

                if ep_path.exists():
                    logging.info("S01E%02d already exists, skipping", ep_num)
                    continue

                logging.info("S01E%02d missing, downloading...", ep_num)

                if not check_disk_space(str(media_root), min_free_gb):
                    logging.error("Disk space low, stopping downloads")
                    break

                try:
                    servers = get_episode_servers(session, ep_url)

                    if not servers:
                        logging.warning("No download servers found for episode %d", ep_num)
                        continue

                    # Try servers in order: express first, then arabhd
                    server_order = ["express", "arabhd"]
                    servers_by_type = {s["type"]: s for s in servers}
                    success = False

                    for server_type in server_order:
                        if server_type not in servers_by_type:
                            continue
                        server = servers_by_type[server_type]

                        try:
                            if server_type == "express":
                                logging.info("Trying express (mail.ru)")
                                success = download_from_mailru(server["url"], ep_path)
                            elif server_type == "arabhd":
                                logging.info("Trying Arab HD")
                                stream_url = get_arabhd_stream_url(server["id"])
                                if stream_url:
                                    success = download_from_hls(stream_url, ep_path)
                                else:
                                    logging.warning("Could not get Arab HD stream URL")

                            if success:
                                break
                        except Exception as e:
                            logging.warning("Server %s failed: %s", server_type, e)
                            part_path = Path(str(ep_path) + ".part")
                            if part_path.exists():
                                part_path.unlink()
                            continue

                    if not success:
                        logging.error("All servers failed for episode %d", ep_num)

                    time.sleep(5)

                except Exception as e:
                    logging.error("Failed to download episode %d: %s", ep_num, e)
                    part_path = Path(str(ep_path) + ".part")
                    if part_path.exists():
                        part_path.unlink()

        except Exception as e:
            logging.error("Error processing %s: %s", series_name, e)

        time.sleep(3)  # Rate limit between series

    logging.info("TurkishSeriesScrapper finished")


if __name__ == "__main__":
    main()
