import git
import yaml
import os
from dotenv import dotenv_values
from deepmerge import always_merger


from typing import Optional

FROM_TO = False
CLEAN_NONE = True
BASEDIR = "./ndc/"
CUSTOM_VALUE_FILE_NAME = "docker-compose.custom-values.yml"
REMOVED_VALUE_FILE_NAME = "docker-compose.removed-values.yml"


def diff(dict1: dict, dict2: dict, differences: Optional[dict] = None) -> dict:
    if differences is None:
        differences = {}
    keys = set(dict1.keys()).union(set(dict2.keys()))
    for key in keys:
        if dict1.get(key) != dict2.get(key):
            if isinstance(dict1.get(key), dict) and isinstance(dict2.get(key), dict):
                differences[key] = {}
                diff(dict1[key], dict2[key], differences[key])

                if CLEAN_NONE:
                    differences[key] = {
                        k: v
                        for k, v in differences[key].items()
                        if not (v is None or (isinstance(v, dict) and not v))
                    }
            else:
                if FROM_TO:
                    differences[key] = [dict1.get(key), dict2.get(key)]
                else:
                    differences[key] = dict2.get(key)

    if CLEAN_NONE:
        differences = {
            k: v
            for k, v in differences.items()
            if not (v is None or (isinstance(v, dict) and not v))
        }

    return differences


def generate_custom_values_yaml(dir: str, repo: git.Repo) -> None:

    missing = {}
    modified = {}

    if not os.path.isfile(f"{BASEDIR}{dir}/.env"):
        return

    env_values = dotenv_values(f"{BASEDIR}{dir}/.env")
    seperator = env_values.get("COMPOSE_PATH_SEPARATOR", ":")
    files = env_values.get("COMPOSE_FILE", "").split(seperator)

    for file in files:
        if file == REMOVED_VALUE_FILE_NAME or file == CUSTOM_VALUE_FILE_NAME:
            continue

        print(f"processing {dir}/{file}")
        try:
            original = yaml.full_load(repo.git.show(f"origin/main:{dir}/{file}"))
        except Exception as e:
            print(f"unable to open git file {str(e)}")
            original = {}

        try:
            with open(f"{BASEDIR}{dir}/{file}", "r") as ymlfile:
                current = yaml.full_load(ymlfile)
        except Exception as e:
            print(f"unable to open local file {str(e)}")
            current = {}

        result = diff(original, current)
        modified = always_merger.merge(modified, result)
        result = diff(current, original)
        missing = always_merger.merge(missing, result)

    if modified:
        with open(f"{BASEDIR}{dir}/{CUSTOM_VALUE_FILE_NAME}", "w") as ymlfile:
            yaml.dump(modified, ymlfile)

    if missing:
        with open(f"{BASEDIR}{dir}/{REMOVED_VALUE_FILE_NAME}", "w") as ymlfile:
            yaml.dump(missing, ymlfile)


if __name__ == "__main__":
    r = git.Repo(BASEDIR)
    for dir in [f.path for f in os.scandir(BASEDIR) if f.is_dir()]:
        dir = dir.replace(BASEDIR, "")
        print(f"{dir}")
        generate_custom_values_yaml(dir, r)
