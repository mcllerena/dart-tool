"""Build the compact transmission-line GeoJSON cache from the HIFLD archive."""

if __package__:
    from .map import TRANSMISSION_LINES_CACHE_PATH, TRANSMISSION_LINES_ZIP_PATH
    from .map import write_transmission_line_cache
else:
    from map import TRANSMISSION_LINES_CACHE_PATH, TRANSMISSION_LINES_ZIP_PATH
    from map import write_transmission_line_cache


def main() -> None:
    feature_count, compressed_size = write_transmission_line_cache(
        TRANSMISSION_LINES_ZIP_PATH,
        TRANSMISSION_LINES_CACHE_PATH,
    )
    print(
        f"Built {feature_count:,} transmission lines "
        f"({compressed_size / 1_000_000:.1f} MB): "
        f"{TRANSMISSION_LINES_CACHE_PATH}"
    )


if __name__ == "__main__":
    main()
