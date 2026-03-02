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
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs

import requests
import yaml
from bs4 import BeautifulSoup
from plexapi.server import PlexServer


# --- Configuration ---

def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logging.error(f"Config file not found: {config_path}")
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
    return session


# --- Scraping ---

def get_latest_episode_number(session, base_url, series_slug):
    """Scrape the series page to find the latest episode number."""
    url = f"{base_url}/series/{quote(series_slug, safe='')}/"
    logging.info(f"Fetching series page: {unquote(url)}")

    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Episodes are listed as links containing الحلقة (episode) and a number
    episode_numbers = []
    for link in soup.find_all("a", href=True):
        href = unquote(link["href"])
        match = re.search(r'الحلقة-(\d+)', href)
        if match:
            episode_numbers.append(int(match.group(1)))

    if not episode_numbers:
        logging.warning(f"No episodes found for {series_slug}")
        return 0

    latest = max(episode_numbers)
    logging.info(f"Latest episode: {latest} (found {len(set(episode_numbers))} unique episodes)")
    return latest


def extract_servers_from_element(element_url):
    """Extract server list from a qesen.net URL with base64 post parameter."""
    parsed = urlparse(element_url)
    params = parse_qs(parsed.query)
    if "post" not in params:
        return None

    decoded = base64.b64decode(params["post"][0]).decode("utf-8")
    payload = json.loads(decoded)
    return payload.get("servers", [])


def get_episode_download_url(session, base_url, series_slug, episode_num):
    """Scrape an episode page to get the cloud.mail.ru download URL."""
    url = f"{base_url}/episode/{quote(series_slug, safe='')}-{quote('الحلقة', safe='')}-{episode_num}/"
    logging.info(f"Fetching episode page: {unquote(url)}")

    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

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
                for server in servers:
                    if server.get("name", "").lower() == "express":
                        mailru_url = server["id"]
                        if "cloud.mail.ru" in mailru_url:
                            logging.info(f"Found mail.ru URL: {mailru_url}")
                            return mailru_url
        except Exception as e:
            logging.warning(f"Failed to decode payload from {candidate_url}: {e}")

    # Also check inline scripts for embedded base64 data
    for script in soup.find_all("script"):
        if not script.string:
            continue
        # Look for base64-encoded JSON in script content
        for match in re.finditer(r'post=([A-Za-z0-9+/=]+)', script.string):
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                payload = json.loads(decoded)
                for server in payload.get("servers", []):
                    if server.get("name", "").lower() == "express":
                        mailru_url = server["id"]
                        if "cloud.mail.ru" in mailru_url:
                            logging.info(f"Found mail.ru URL in script: {mailru_url}")
                            return mailru_url
            except Exception:
                continue

    logging.warning(f"No express server found for episode {episode_num}")
    return None


# --- Mail.ru Download ---

def download_from_mailru(mailru_url, output_path):
    """Download a file from cloud.mail.ru public link via their API."""
    parsed = urlparse(mailru_url)
    public_hash = parsed.path.replace("/public/", "").strip("/")

    logging.info(f"Downloading from mail.ru, hash: {public_hash}")

    mailru_session = requests.Session()
    mailru_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })

    # Step 1: Visit the public page to establish cookies
    page_resp = mailru_session.get(mailru_url, timeout=30)
    page_resp.raise_for_status()

    # Step 2: Extract weblink_get URL from page or dispatcher API
    weblink_get_url = None

    # Try embedded JSON in page source
    match = re.search(r'"weblink_get"\s*:\s*\[\s*\{\s*"url"\s*:\s*"([^"]+)"', page_resp.text)
    if match:
        weblink_get_url = match.group(1)

    # Fallback: dispatcher API
    if not weblink_get_url:
        try:
            disp_resp = mailru_session.get(
                "https://cloud.mail.ru/api/v2/dispatcher",
                timeout=15,
            )
            if disp_resp.ok:
                disp_data = disp_resp.json()
                weblink_get_url = disp_data["body"]["weblink_get"][0]["url"]
        except Exception as e:
            logging.warning(f"Dispatcher API failed: {e}")

    if not weblink_get_url:
        raise Exception("Could not find weblink_get URL from mail.ru")

    logging.info(f"Got weblink_get URL: {weblink_get_url}")

    # Step 3: Get download token
    token_resp = mailru_session.get(
        "https://cloud.mail.ru/api/v2/tokens/download",
        timeout=15,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["body"]["token"]
    logging.info("Got download token")

    # Step 4: Download the file
    download_url = f"{weblink_get_url}/{public_hash}?key={token}"
    logging.info(f"Starting download...")

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
                # Log progress every ~10MB
                if total > 0 and downloaded % (10 * 1024 * 1024) < 128 * 1024:
                    pct = (downloaded / total) * 100
                    logging.info(
                        f"  Progress: {pct:.1f}% "
                        f"({downloaded // (1024 * 1024)}MB / {total // (1024 * 1024)}MB)"
                    )

    # Rename .part to final path (atomic on same filesystem)
    part_path.rename(output_path)
    logging.info(f"Download complete: {output_path.name} ({downloaded // (1024 * 1024)}MB)")
    return True


# --- Plex Integration ---

def connect_plex(config):
    """Connect to Plex server."""
    try:
        plex = PlexServer(config["plex"]["url"], config["plex"]["token"])
        logging.info(f"Connected to Plex: {plex.friendlyName}")
        return plex
    except Exception as e:
        logging.error(f"Failed to connect to Plex: {e}")
        return None


def cleanup_watched(plex, config):
    """Delete episodes that have been watched in Plex."""
    library_name = config["plex"]["library_name"]
    media_root = Path(config["storage"]["media_root"])
    deleted_count = 0

    try:
        library = plex.library.section(library_name)
    except Exception:
        logging.info(f"Plex library '{library_name}' not found, skipping cleanup")
        return 0

    for show in library.all():
        for episode in show.episodes():
            if not episode.isPlayed:
                continue
            for media in episode.media:
                for part in media.parts:
                    file_path = Path(part.file)
                    if not file_path.exists():
                        continue
                    if not str(file_path).startswith(str(media_root)):
                        continue

                    logging.info(f"Deleting watched: {file_path.name}")
                    file_path.unlink()
                    deleted_count += 1

                    # Clean up empty directories
                    season_dir = file_path.parent
                    if season_dir.exists() and not any(season_dir.iterdir()):
                        season_dir.rmdir()
                        logging.info(f"Removed empty dir: {season_dir.name}")

                    series_dir = season_dir.parent
                    if series_dir.exists() and not any(series_dir.iterdir()):
                        series_dir.rmdir()
                        logging.info(f"Removed empty dir: {series_dir.name}")

    if deleted_count > 0:
        logging.info(f"Cleaned up {deleted_count} watched episode(s)")
        library.update()

    return deleted_count


def trigger_plex_scan(plex, config):
    """Trigger a Plex library scan."""
    try:
        library = plex.library.section(config["plex"]["library_name"])
        library.update()
        logging.info("Triggered Plex library scan")
    except Exception as e:
        logging.warning(f"Could not trigger Plex scan: {e}")


# --- Disk Space ---

def check_disk_space(media_root, min_free_gb):
    """Check if there's enough free disk space."""
    try:
        usage = shutil.disk_usage(media_root)
        free_gb = usage.free / (1024 ** 3)
        logging.info(f"Disk space: {free_gb:.1f}GB free")
        return free_gb >= min_free_gb
    except Exception as e:
        logging.warning(f"Could not check disk space: {e}")
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


# --- Main ---

def main():
    setup_logging()
    logging.info("=" * 60)
    logging.info("TurkishSeriesScrapper starting")
    logging.info("=" * 60)

    config = load_config()
    session = create_session()
    media_root = Path(config["storage"]["media_root"])
    min_free_gb = config["storage"].get("min_free_space_gb", 10)
    base_url = config["scraper"]["base_url"]

    # Ensure media root exists
    media_root.mkdir(parents=True, exist_ok=True)

    # Connect to Plex
    plex = connect_plex(config)

    # Cleanup watched episodes first (free space before downloading)
    if plex:
        cleanup_watched(plex, config)

    # Check disk space
    if not check_disk_space(str(media_root), min_free_gb):
        logging.error(f"Not enough disk space (need {min_free_gb}GB free). Aborting.")
        return

    downloaded_any = False

    for series in config.get("series", []):
        if not series.get("enabled", True):
            continue

        series_name = series["name"]
        series_slug = series["slug"]
        logging.info(f"\n--- Processing: {series_name} ---")

        try:
            latest_ep = get_latest_episode_number(session, base_url, series_slug)
            if latest_ep == 0:
                continue

            # Download all missing episodes from 1 to latest
            for ep_num in range(1, latest_ep + 1):
                ep_path = get_episode_path(media_root, series_name, ep_num)

                if ep_path.exists():
                    continue

                logging.info(f"Missing episode {ep_num}, attempting download...")

                if not check_disk_space(str(media_root), min_free_gb):
                    logging.error("Disk space low, stopping downloads")
                    break

                try:
                    mailru_url = get_episode_download_url(
                        session, base_url, series_slug, ep_num
                    )
                    if not mailru_url:
                        logging.warning(f"Skipping episode {ep_num} - no express server")
                        continue

                    if download_from_mailru(mailru_url, ep_path):
                        downloaded_any = True

                    time.sleep(5)  # Rate limit between downloads

                except Exception as e:
                    logging.error(f"Failed to download episode {ep_num}: {e}")
                    part_path = Path(str(ep_path) + ".part")
                    if part_path.exists():
                        part_path.unlink()
                    continue

        except Exception as e:
            logging.error(f"Error processing {series_name}: {e}")

        time.sleep(3)  # Rate limit between series

    # Trigger Plex scan if anything was downloaded
    if downloaded_any and plex:
        trigger_plex_scan(plex, config)

    logging.info("TurkishSeriesScrapper finished")


if __name__ == "__main__":
    main()
