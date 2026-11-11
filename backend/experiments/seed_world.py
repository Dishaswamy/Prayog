"""Labs, an admin and users with tokens for failure_experiment.py, which runs outside the image."""

import argparse
import json

from experiments.common import World


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    world = World()
    lab = world.lab()
    _, admin = world.user(lab, role="ADMIN")
    users = [world.user(lab)[1] for _ in range(8)]
    world.close()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"lab": lab, "admin": admin, "users": users}, f)


if __name__ == "__main__":
    main()
