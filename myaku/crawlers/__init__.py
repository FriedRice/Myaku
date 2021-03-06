"""Sub-package for all Myaku crawlers."""

# Import crawler classes to make them importable directly from package
from .asahi import AsahiCrawler  # noqa: F401
from .kakuyomu import KakuyomuCrawler  # noqa: F401
from .nhk_news_web import NhkNewsWebCrawler  # noqa: F401
