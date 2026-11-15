import os

from koov.agent import KoovAgent
from koov.config import KoovConfig
from koov.envfile import load_env_file


def main() -> None:
    load_env_file(os.getenv("KOOV_ENV_FILE", ".env"))
    config = KoovConfig.from_env()
    agent = KoovAgent(config)
    agent.run_forever()


if __name__ == "__main__":
    main()
