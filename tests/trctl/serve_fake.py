"""Run the fake daemon standalone for trying the TUI without touching a real daemon.

    python3 tests/serve_fake.py            # prints the URL to use
    trctl --url <URL> watch
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_daemon import FakeDaemon, make_torrent  # noqa: E402


def demo_torrents():
    return [
        make_torrent(1, "ubuntu-24.04.1-desktop-amd64.iso", [("ubuntu.iso", 6_100_000_000)], done=1.0,
                     status=6, rate_up=180_000),
        make_torrent(2, "Big.Buck.Bunny.2008.1080p",
                     [("Big.Buck.Bunny.mkv", 2_400_000_000), ("Extras/Making.Of.mkv", 700_000_000),
                      ("Subs/English.srt", 60_000), ("Subs/German.srt", 58_000), ("sample.mkv", 40_000_000),
                      ("poster.jpg", 900_000), ("info.nfo", 2_000)],
                     done=0.35, status=4, rate_down=4_200_000, rate_up=60_000, labels=["movies"]),
        make_torrent(3, "debian-12.7.0-amd64-netinst.iso", [("debian.iso", 660_000_000)], done=0.12, status=0),
        make_torrent(4, "Sintel (2010) [Open Movie] 4K", [("Sintel.4k.mkv", 12_000_000_000),
                                                          ("Sintel.srt", 80_000)],
                     done=0.02, status=3),
        make_torrent(5, "archlinux-2024.11.01-x86_64.iso", [("arch.iso", 1_200_000_000)], done=0.81,
                     status=4, rate_down=900_000, rate_up=20_000, labels=["linux"]),
        make_torrent(6, "broken torrent", [("x.bin", 1_000_000)], done=0.5, status=0, error=3,
                     error_string="No data found! Ensure your drives are connected"),
    ]


if __name__ == "__main__":
    with FakeDaemon(demo_torrents()) as d:
        print(d.url, flush=True)
        try:
            while True:
                time.sleep(1)
                d.tick(1)
        except KeyboardInterrupt:
            pass
