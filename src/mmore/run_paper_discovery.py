# mmore/run_paper_discovery.py
"""Entrypoint for `mmore paper-discovery --config-file <yaml>`.

No `load_dotenv()` here, unlike the other entrypoints. Every source this
pipeline talks to is anonymous, so there are no secrets to load.
"""

import argparse
import time

from mmore.profiler import enable_profiling_from_env, profile_function

from .paper_discovery.config import PaperDiscoveryConfig
from .paper_discovery.pipeline import PaperDiscoveryPipeline
from .utils import load_config
from .ux import card, quiet_noisy_libs, setup_logging, step_intro, step_summary

PAPER_DISCOVERY_NAME = "Paper Discovery"
PAPER_DISCOVERY_EMOJI = "📄"
logger = setup_logging(PAPER_DISCOVERY_NAME, PAPER_DISCOVERY_EMOJI)


@profile_function()
def run_paper_discovery(config_file: str) -> None:
    quiet_noisy_libs()
    cfg = load_config(config_file, PaperDiscoveryConfig)
    step_intro(
        PAPER_DISCOVERY_NAME,
        PAPER_DISCOVERY_EMOJI,
        "Find papers for your keywords",
        [
            f"sources: {', '.join(cfg.sources)}",
            f"PDFs: {'on' if cfg.download_pdfs else 'off'}",
        ],
    )
    pipeline = PaperDiscoveryPipeline(config=cfg)
    start = time.time()
    pipeline.run()
    step_summary(
        PAPER_DISCOVERY_NAME,
        PAPER_DISCOVERY_EMOJI,
        time.time() - start,
        pipeline.summary(),
    )
    steps = pipeline.next_steps()
    if steps:
        card("Next steps", steps)


if __name__ == "__main__":
    enable_profiling_from_env()
    parser = argparse.ArgumentParser(description="Run the Paper Discovery pipeline.")
    parser.add_argument(
        "--config-file",
        type=str,
        required=True,
        help="Path to the Paper Discovery configuration file (YAML).",
    )
    args = parser.parse_args()
    run_paper_discovery(args.config_file)
