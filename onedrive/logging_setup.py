import logging

from onedrive import constants


def setup_logging(level: int = logging.INFO) -> None:
    constants.ensure_dirs()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(constants.LOG_FILE),
            logging.StreamHandler(),
        ],
    )
