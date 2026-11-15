import os

from mechus.agent import MechusAgent
from mechus.config import MechusConfig
from mechus.envfile import load_env_file


def main() -> None:
    load_env_file(os.getenv("MECHUS_ENV_FILE", ".env"))
    config = MechusConfig.from_env()
    agent = MechusAgent(config)
    agent.run_forever()


if __name__ == "__main__":
    main()
